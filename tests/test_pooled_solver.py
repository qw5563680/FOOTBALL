"""Numerical and failure contracts for the multi-league-only pooled solver."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from penaltyblog.models import DixonColesGoalModel

from zucai.predict import pooled_fit
from zucai.predict.dixon_coles import DixonColes

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t21/model-history.csv"
CUTOFF = pd.Timestamp("2024-02-15T00:00:00Z")


@pytest.fixture
def history():
    return pd.read_csv(FIXTURE, comment="#", parse_dates=["kickoff_utc", "available_utc"])


def engine_for(history):
    rows = history.loc[history.available_utc < CUTOFF]
    return DixonColesGoalModel(rows.home_goals, rows.away_goals, rows.home, rows.away)


@pytest.mark.parametrize("rho", [-0.1, 0.15, -2.0, 2.0])
@pytest.mark.parametrize("neutral", [False, True])
def test_full_gradient_matches_original_loss_including_floor_and_neutral(history, rho, neutral):
    engine = engine_for(history)
    engine.weights *= np.linspace(0.2, 1.5, len(engine.weights))
    assert {(0, 0), (0, 1), (1, 0), (1, 1)} <= set(
        zip(engine.goals_home, engine.goals_away, strict=True)
    )
    if neutral:
        engine.neutral_venue[::3] = 1
    params = engine._params.copy()
    params[: engine.n_teams] += np.linspace(-0.08, 0.08, engine.n_teams)
    params[engine.n_teams : 2 * engine.n_teams] += np.linspace(0.05, -0.05, engine.n_teams)
    params[-1] = rho
    steps = np.eye(len(params)) * 1e-6
    numerical = np.array(
        [
            (engine._loss_function(params + step) - engine._loss_function(params - step)) / 2e-6
            for step in steps
        ]
    )
    np.testing.assert_allclose(
        pooled_fit.complete_gradient(engine, params), numerical, rtol=1e-5, atol=2e-6
    )


def test_accelerated_and_complete_gradient_cold_fit_agree(history):
    cold, fast = engine_for(history), engine_for(history)
    pooled_fit._full_slsqp(cold)
    diagnostics = pooled_fit.fit_pooled_engine(fast)
    assert diagnostics["success"] is True
    assert diagnostics["optimizer"] == "SLSQP"
    assert diagnostics["gradient"] == "full_dc_with_tau_floor"
    assert abs(diagnostics["attack_sum_residual"]) <= 1e-6
    assert diagnostics["loss"] == pytest.approx(-fast.loglikelihood, abs=1e-8)
    assert abs(fast.loglikelihood - cold.loglikelihood) < 2e-5
    for home in fast.teams:
        for away in fast.teams:
            if home != away:
                np.testing.assert_allclose(
                    fast.predict(home, away).grid, cold.predict(home, away).grid, atol=1e-4, rtol=0
                )


@pytest.mark.parametrize("failure", ["exception", "nan", "bounds"])
def test_invalid_initialization_falls_back_to_complete_gradient_cold_start(
    history, monkeypatch, failure
):
    cold, fast = engine_for(history), engine_for(history)
    pooled_fit._full_slsqp(cold)

    def invalid(engine):
        if failure == "exception":
            raise RuntimeError("synthetic initialization failure")
        params = engine._params.copy()
        params[0] = np.nan if failure == "nan" else 100
        return params, []

    monkeypatch.setattr(pooled_fit, "_augmented_initialization", invalid)
    diagnostics = pooled_fit.fit_pooled_engine(fast)
    assert diagnostics["initialization"] == "cold_start_after_initialization_failure"
    assert diagnostics["initialization_failure"]
    np.testing.assert_array_equal(fast._params, cold._params)


def test_final_solver_failure_is_rejected_and_cannot_save(history, monkeypatch, tmp_path):
    def failed(*args, **kwargs):
        raise ValueError("synthetic final SLSQP failure")

    monkeypatch.setattr(DixonColesGoalModel, "_fit", failed)
    model = DixonColes(pooled=True)
    with pytest.raises(ValueError, match="final SLSQP"):
        model.fit(history, as_of=CUTOFF)
    assert model.engine is None
    with pytest.raises(ValueError, match="训练"):
        model.save(tmp_path / "rejected.pkl")
    assert not (tmp_path / "rejected.pkl").exists()


@pytest.mark.parametrize("invalid", ["nan", "bounds", "constraint", "status"])
def test_final_result_has_independent_validity_checks(history, monkeypatch, invalid):
    engine = engine_for(history)

    def fake_fit(*args, **kwargs):
        if invalid == "nan":
            engine._params[0] = np.nan
        elif invalid == "bounds":
            engine._params[-2] = 3
        elif invalid == "constraint":
            engine._params[0] += 0.01
        engine.fitted = True
        engine.loglikelihood = -1
        engine._res = SimpleNamespace(success=invalid != "status", nit=1)

    monkeypatch.setattr(engine, "_fit", fake_fit)
    with pytest.raises(ValueError, match="SLSQP"):
        pooled_fit._full_slsqp(engine)
    assert engine.fitted is False


@pytest.mark.parametrize("calibrate", [False, True])
def test_domestic_and_single_league_pooled_keep_original_fit(history, monkeypatch, calibrate):
    history["league"] = "英超"

    def forbidden(*args, **kwargs):
        pytest.fail("single-league fitting must not call the pooled solver")

    monkeypatch.setattr("zucai.predict.dixon_coles.fit_pooled_engine", forbidden)
    domestic = DixonColes(draw_calibration=calibrate).fit(history, as_of=CUTOFF)
    pooled = DixonColes(pooled=True, draw_calibration=calibrate).fit(history, as_of=CUTOFF)
    np.testing.assert_array_equal(domestic.engine._params, pooled.engine._params)
    row = dict(home="甲队", away="乙队", league="英超", kickoff_utc=CUTOFF + pd.Timedelta(days=1))
    np.testing.assert_array_equal(
        domestic.predict_score_matrix(row), pooled.predict_score_matrix(row)
    )
    assert pooled.fitting_method == "penaltyblog_slsqp"
    assert "fitting_method" not in domestic.predict_with_metadata(row)[1]


def test_solver_disclosure_roundtrips_and_metadata_cannot_mutate_it(history, tmp_path):
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    assert model.fitting_method == pooled_fit.FITTING_METHOD
    assert model.solver_diagnostics["success"] is True
    path = tmp_path / "pooled.pkl"
    model.save(path)
    restored = DixonColes.load(path)
    assert restored.fitting_method == model.fitting_method
    assert restored.solver_diagnostics == model.solver_diagnostics
    row = dict(home="甲队", away="乙队", league="欧冠", kickoff_utc=CUTOFF + pd.Timedelta(days=1))
    _, metadata = restored.predict_with_metadata(row)
    assert metadata["fitting_method"] == pooled_fit.FITTING_METHOD
    assert metadata["solver_diagnostics"] == model.solver_diagnostics
    metadata["solver_diagnostics"]["success"] = False
    assert restored.solver_diagnostics["success"] is True


def test_declared_scope_checks_only_strictly_eligible_leagues(history):
    history.attrs["training_leagues"] = sorted(history.league.unique())
    changed = history.copy()
    changed.loc[changed.available_utc >= CUTOFF, "league"] = "未来赛事"
    before = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    after = DixonColes(pooled=True).fit(changed, as_of=CUTOFF)
    np.testing.assert_array_equal(before.engine._params, after.engine._params)
    assert before.solver_diagnostics == after.solver_diagnostics
    assert after.training_league_scope == history.attrs["training_leagues"]
    history.attrs["training_leagues"] = ["欧冠"]
    with pytest.raises(ValueError, match="联赛.*范围|范围.*联赛"):
        DixonColes(pooled=True).fit(history, as_of=CUTOFF)
