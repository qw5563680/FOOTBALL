"""Frozen parameter replay, not a re-fit on the four historical results."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from penaltyblog.models import DixonColesGoalModel

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.predict.dixon_coles import DixonColes
from zucai.report.batch import predict_batch
from zucai.report.render import load_batch, write_report

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t37"
REAL = json.loads((FIXTURES / "parameters.json").read_text())
SYNTHETIC = json.loads((FIXTURES / "synthetic.json").read_text())


def replay_model(fold):
    """Initialize team lookup only; no historical outcomes are present or fitted."""
    params = fold["parameters"]
    teams = sorted(k.removeprefix("attack_") for k in params if k.startswith("attack_"))
    engine = DixonColesGoalModel([0] * len(teams), [0] * len(teams), teams, teams[::-1])
    engine._params = np.array([params[k] for k in engine._get_param_names()])
    engine.fitted = True
    model = DixonColes(draw_calibration=True)
    model.engine, model.teams = engine, set(teams)
    model.as_of = pd.Timestamp(fold["as_of"])
    model.latest_available = model.as_of - pd.Timedelta(hours=1)
    model.league, model.sources = fold["league"], ["fd"]
    model.training_rows = fold["training_rows"]
    model.draw_offset = fold["draw_offset"]
    attack = np.sort([params[f"attack_{t}"] for t in teams])
    defence = np.sort([params[f"defence_{t}"] for t in teams])
    n = int(np.ceil(len(teams) / 4))
    model.prior_parameters = dict(
        attack=float((attack[:n].mean() + attack.mean()) / 2),
        defence=float((defence[-n:].mean() + defence.mean()) / 2),
        method="weakest_quartile_half_shrinkage",
    )
    return model


def assert_valid_guard(model, target):
    before = model.engine._params.copy()
    offset = model.draw_offset
    grid, metadata = model.predict_with_metadata(target)
    assert grid.shape == (10, 10) and np.isfinite(grid).all() and grid.min() >= 0
    assert grid.sum() == pytest.approx(1, abs=1e-12)
    assert metadata["rho_clipped"] is True
    assert metadata["rho_before"] == before[-1]
    low, high = metadata["rho_legal_interval"]
    assert low <= metadata["rho_after"] <= high
    assert metadata["rho_after"] != metadata["rho_before"]
    np.testing.assert_array_equal(model.engine._params, before)
    assert model.draw_offset == offset
    return grid, metadata


@pytest.mark.parametrize("target", REAL["targets"], ids=lambda t: t["match_id"])
def test_four_saved_parameter_failures_are_guarded(target):
    model = replay_model(REAL["folds"][target["fit_cutoff"]])
    assert target["home"] in model.teams and target["away"] in model.teams
    # The upstream constructor tolerates the tiny F2 negative value, while our
    # wrapper rejects every negative cell. The other three fail upstream already.
    if target["league"] == "法乙":
        old = model.engine.predict(target["home"], target["away"], normalize=False).grid
        assert old[1, 0] < 0
    else:
        with pytest.raises(ValueError, match="negative probabilities"):
            model.engine.predict(target["home"], target["away"], normalize=False)
    _, metadata = assert_valid_guard(model, target)
    assert metadata["unknown_team_prior"] is False
    assert metadata["prior_rho_clipped"] is False
    assert metadata["rho_clip_reason"] == "outside_legal_interval"


def test_synthetic_history_reproduces_negative_rho_and_is_guarded():
    history = pd.DataFrame(SYNTHETIC["history"])
    for column in ("kickoff_utc", "available_utc"):
        history[column] = pd.to_datetime(history[column])
    model = DixonColes().fit(history, as_of=pd.Timestamp(SYNTHETIC["as_of"]))
    assert model.engine.get_params()["rho"] < 0
    target = SYNTHETIC["target"]
    with pytest.raises(ValueError, match="negative probabilities"):
        model.engine.predict(target["home"], target["away"], normalize=False)
    assert_valid_guard(model, target)


@pytest.mark.parametrize("target", REAL["normal_targets"])
@pytest.mark.parametrize("calibrated", [False, True])
def test_five_normal_pairs_are_exactly_unchanged(target, calibrated):
    model = replay_model(next(iter(REAL["folds"].values())))
    model.draw_calibration = calibrated
    old = model.engine.predict(target["home"], target["away"], normalize=False).grid[:10, :10]
    old = old / old.sum()
    if calibrated:
        old[np.diag_indices(10)] *= np.exp(model.draw_offset)
        old /= old.sum()
    new, metadata = model.predict_with_metadata(target)
    np.testing.assert_array_equal(new, old)
    assert metadata["rho_clipped"] is False
    assert metadata["rho_before"] == metadata["rho_after"]
    assert metadata["rho_clip_reason"] is None
    assert metadata["rho_roundoff_steps"] == 0


def test_boundary_roundoff_is_also_audited():
    fixture = json.loads((FIXTURES / "boundary.json").read_text())
    model = replay_model(fixture)
    old = model.engine.predict("A", "B", normalize=False).grid
    assert old.min() < 0
    _, metadata = assert_valid_guard(model, fixture["target"])
    assert metadata["rho_clip_reason"] == "boundary_roundoff"
    assert 0 < metadata["rho_roundoff_steps"] <= 8


def test_unknown_team_uses_unchanged_prior_and_records_both_flags():
    fold = {**SYNTHETIC, "league": "L", "training_rows": 60, "draw_offset": 0.0}
    model = replay_model(fold)
    target = {**SYNTHETIC["target"], "home": "unknown"}
    prior = model.prior_parameters.copy()
    _, metadata = assert_valid_guard(model, target)
    assert model.prior_parameters == prior
    assert metadata["unknown_team_prior"] is True
    assert metadata["prior_rho_clipped"] is True
    assert metadata["prior_method"] == "weakest_quartile_half_shrinkage"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_parameters_are_not_repaired(bad):
    model = replay_model(next(iter(REAL["folds"].values())))
    model.engine._params[-1] = bad
    with pytest.raises(ValueError, match="未产生有效"):
        model.predict_with_metadata(REAL["normal_targets"][0])


@pytest.mark.parametrize("boundary", [False, True])
def test_clipping_is_frozen_and_visible_in_match_row(tmp_path, monkeypatch, boundary):
    if boundary:
        fold = json.loads((FIXTURES / "boundary.json").read_text())
        target = {**fold["target"], "match_id": "fd:synthetic-boundary", "league": fold["league"]}
    else:
        target = REAL["targets"][0]
        fold = REAL["folds"][target["fit_cutoff"]]
    model = replay_model(fold)
    path = tmp_path / "model.pkl"
    model.save(path)
    engine = get_engine(tmp_path / "matches.db")
    init_db(engine)
    cutoff = model.as_of + pd.Timedelta(hours=1)
    with session_scope(engine) as session:
        home, away = Team(name=target["home"]), Team(name=target["away"])
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id=target["match_id"],
                source="fd",
                league=target["league"],
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=pd.Timestamp(target["kickoff_utc"]),
                status="scheduled",
            )
        )
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            match_ids=[target["match_id"]],
            as_of=cutoff,
            generated=cutoff,
            persist=False,
        )
    output = tmp_path / "frozen"
    write_report(batch, output)
    frozen = load_batch(output / "forecast.json")
    row = frozen["predictions"][0]
    assert row["status"] == "predicted"
    assert row["metadata"]["rho_clipped"] is True
    assert row["metadata"]["rho_before"] > row["metadata"]["rho_after"]
    assert sum(row["model_probabilities"].values()) == pytest.approx(1)
    assert frozen["bet_allowed"] is False
    report = (output / "report.md").read_text()
    match_row = next(
        line for line in report.splitlines() if line.startswith("| " + target["match_id"])
    )
    assert "已预测" in match_row and "低比分修正受限" in match_row
    before, after = (f"{row['metadata'][key]:.17g}" for key in ("rho_before", "rho_after"))
    assert before != after
    assert f"{before} → {after}" in match_row
    from zucai.cloud_predict import _summary

    summary = tmp_path / "actions-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    day = pd.Timestamp(target["kickoff_utc"]).tz_convert("Asia/Shanghai").date().isoformat()
    _summary(
        tmp_path,
        {
            "run_id": "fixture-t37",
            "status": "ok",
            "sporttery_stale": False,
            "leagues": {},
            "forecasts": {day: {"path": "frozen", "predicted": 1, "excluded": 0, "reasons": {}}},
        },
    )
    assert match_row in summary.read_text()
    # Loading/predicting did not persist the per-pair clamp into the model artifact.
    np.testing.assert_array_equal(DixonColes.load(path).engine._params, model.engine._params)
