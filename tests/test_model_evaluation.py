"""T15 boundaries; synthetic fixtures test contracts, never acceptance metrics."""

import numpy as np
import pandas as pd
import pytest

from zucai.predict import backtest
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory


@pytest.fixture
def seasons_history():
    rng = np.random.default_rng(15)
    records = []
    for day in range(0, 365 * 4, 4):
        kickoff = pd.Timestamp("2022-07-01T12:00Z") + pd.Timedelta(days=day)
        home = (day // 4) % 4
        away = (home + 1 + (day // 16) % 3) % 4
        records.append(
            dict(
                match_id=f"fd:{day}",
                source="fd",
                league="英超",
                home="ABCD"[home],
                away="ABCD"[away],
                kickoff_utc=kickoff,
                available_utc=kickoff + pd.Timedelta(hours=3),
                available_source="assumed",
                home_goals=rng.poisson(1.6),
                away_goals=rng.poisson(1.1),
                counterpart_id=None,
            )
        )
    return pd.DataFrame(records)


def test_season_boundaries_and_latest_complete_seasons():
    from zucai.predict.seasons import latest_complete_seasons, season_bounds, season_for

    assert season_for("2025-06-30T23:59:59Z", "英超") == "2425"
    assert season_for("2025-07-01T00:00:00Z", "英超") == "2526"
    assert season_for("2025-12-31T23:59:59Z", "日职") == "2025"
    assert season_for("2026-01-01T00:00:00Z", "日职") == "2026"
    assert season_bounds("2425", "英超") == (
        pd.Timestamp("2024-07-01T00:00Z"),
        pd.Timestamp("2025-07-01T00:00Z"),
    )
    assert latest_complete_seasons("2026-11-01T00:00Z", "英超") == ["2425", "2526"]
    assert latest_complete_seasons("2026-11-01T00:00Z", "日职") == ["2024", "2025"]
    with pytest.raises(ValueError):
        season_bounds("2426", "英超")


def test_closing_preference_complete_triplets_and_missing():
    assert hasattr(backtest, "closing_probabilities")
    prices = backtest.closing_probabilities(
        [
            ("a", "PS", 3.0, 3.0, 3.0),
            ("a", "B365", 2.0, 3.0, 6.0),
            ("b", "B365", 2.0, None, 6.0),
            ("b", "Avg", 2.0, 4.0, 4.0),
            ("c", "Max", 2.0, 3.0, 6.0),
            ("d", "PS", 1.0, 2.0, 3.0),
            ("e", "B365", float("nan"), 3.0, 6.0),
        ]
    )
    assert prices["a"] == pytest.approx((0.5, 1 / 3, 1 / 6))
    assert prices["b"] == pytest.approx((0.5, 0.25, 0.25))
    assert set(prices) == {"a", "b"}


def test_monthly_folds_keep_history_and_closing_separate(seasons_history):
    quotes = {row.match_id: (0.2, 0.3, 0.5) for row in seasons_history.itertuples()}
    kwargs = dict(
        since=pd.Timestamp("2024-07-01T00:00Z"),
        as_of=pd.Timestamp("2026-07-01T00:00Z"),
        seasons=["2425", "2526"],
        fold="monthly",
    )
    first = backtest.evaluate(seasons_history, {}, closing=quotes, **kwargs)
    second = backtest.evaluate(seasons_history, {}, closing={}, **kwargs)
    assert first["model"] == second["model"]
    assert first["closing"]["n"] == first["closing_paired_model"]["n"] == first["model"]["n"]
    assert second["closing"]["log_loss"] is None
    assert set(first["seasons"]) == {"2425", "2526"}
    assert sum(s["model"]["n"] for s in first["seasons"].values()) == first["model"]["n"]
    for item in first["folds"]:
        cutoff = pd.Timestamp(item["cutoff_utc"])
        assert (cutoff + pd.Timedelta(microseconds=1)).day == 1
        assert pd.Timestamp(item["last_available_utc"]) < cutoff
    assert first["folds"][0]["training_games"] > 100
    assert first["model"]["draw_mean"] >= 0
    assert first["model"]["draw_rate"] >= 0
    assert sum(b["n"] for b in first["model"]["calibration"]) == 3 * first["model"]["n"]


def test_unknown_prior_and_before_after_use_identical_known_predictions(seasons_history):
    cutoff = pd.Timestamp("2024-07-01T00:00Z")
    model = DixonColes().fit(seasons_history, as_of=cutoff)
    row = dict(
        home="NEW", away="A", source="fd", league="英超", kickoff_utc=cutoff + pd.Timedelta(days=1)
    )
    matrix, meta = model.predict_with_metadata(row)
    assert matrix.shape == (10, 10) and matrix.min() >= 0
    assert matrix.sum() == pytest.approx(1)
    assert meta["unknown_team_prior"] is True
    assert set(model.teams) == set("ABCD")
    other, _ = model.predict_with_metadata(
        {**row, "market_home": 0.999, "closing": (0.99, 0.005, 0.005)}
    )
    np.testing.assert_array_equal(matrix, other)
    # Disabling the policy reproduces the old skip route, not a different known-team model.
    old = DixonColes(unknown_team_prior=False).fit(seasons_history, as_of=cutoff)
    with pytest.raises(InsufficientHistory):
        old.predict_with_metadata(row)
    known = {**row, "home": "B"}
    np.testing.assert_array_equal(
        old.predict_score_matrix(known), model.predict_score_matrix(known)
    )
    changed = seasons_history.copy()
    first_test = changed.index[changed.kickoff_utc >= cutoff][0]
    changed.loc[first_test, "home"] = "NEW"
    report = backtest.evaluate(changed, {}, since=cutoff, as_of=cutoff + pd.Timedelta(days=30))
    assert report["unknown_team"]["n"] == 1
    assert report["without_prior"]["n"] + 1 == report["model"]["n"]
    assert report["unknown_team_share"] == pytest.approx(1 / report["model"]["n"])


def test_late_labels_at_season_cutoff_cannot_enter_selection(seasons_history):
    from zucai.predict.tuning import rolling_origin

    cutoff = pd.Timestamp("2026-07-01T00:00Z")
    kwargs = dict(grid=[90, 365], seasons=["2425"], as_of=cutoff, fold="monthly")
    first = rolling_origin(seasons_history, {}, **kwargs)
    changed = seasons_history.copy()
    future = changed.kickoff_utc >= pd.Timestamp("2025-07-01T00:00Z")
    changed.loc[future, ["home_goals", "away_goals"]] = [9, 0]
    second = rolling_origin(changed, {}, **kwargs)
    a, b = first["windows"][0], second["windows"][0]
    assert a["selection_season"] == "2425" and a["validation_season"] == "2526"
    assert a["half_life_days"] == b["half_life_days"]
    assert a["candidates"] == b["candidates"]
    assert a["validation"]["model"] != b["validation"]["model"]


def test_gate_requires_recent_complete_weekly_paired_evidence():
    from zucai.predict.parameters import p2_gate

    def metrics(n, ll):
        return dict(n=n, log_loss=ll)

    report = dict(
        fold="weekly",
        seasons={
            season: dict(
                complete=True,
                model=metrics(380, 1.0),
                uniform=metrics(380, np.log(3)),
                closing=metrics(380, 0.96),
                closing_paired_model=metrics(380, 1.0),
            )
            for season in ("2425", "2526")
        },
    )
    kwargs = dict(league="英超", as_of="2026-11-01T00:00Z")
    assert p2_gate(report, **kwargs)["p2_ready"] is True
    report["seasons"]["2526"]["closing_paired_model"]["log_loss"] = 1.09
    assert p2_gate(report, **kwargs)["p2_ready"] is False
    report["seasons"]["2526"]["closing_paired_model"]["log_loss"] = 1.0
    report["seasons"]["2526"]["complete"] = False
    assert p2_gate(report, **kwargs)["p2_ready"] is False
    report["seasons"]["2526"]["complete"] = True
    report["fold"] = "monthly"
    assert p2_gate(report, **kwargs)["p2_ready"] is False


def test_config_override_provenance_and_no_retroactive_use(tmp_path):
    import json

    from zucai.predict.parameters import resolve_parameters

    config = tmp_path / "params.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    "英超": dict(
                        half_life_days=365,
                        selection_as_of_utc="2026-07-01T00:00Z",
                        selection_season="2526",
                        p2_ready=False,
                    )
                },
            }
        )
    )
    result = resolve_parameters("英超", as_of="2026-11-01T00:00Z", params_file=config)
    assert result["half_life_days"] == 365 and result["source"] == "params.json"
    override = resolve_parameters(
        "英超", as_of="2026-11-01T00:00Z", params_file=config, half_life_days=180
    )
    assert override["half_life_days"] == 180 and override["source"] == "cli"
    old = resolve_parameters("英超", as_of="2025-01-01T00:00Z", params_file=config)
    assert old["half_life_days"] == 90 and old["source"] == "default"
    with pytest.raises(ValueError):
        resolve_parameters("英超", as_of="2026-11-01T00:00Z", params_file=config, half_life_days=-1)


def test_train_reads_config_keeps_static_db_unchanged_and_records_override(
    seasons_history, tmp_path
):
    import hashlib
    import json
    import sqlite3

    from zucai.cli import main
    from zucai.db import get_engine, init_db
    from zucai.predict.command import train

    database = tmp_path / "static.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(database) as db:
        db.executemany(
            "INSERT INTO teams(id,name,aliases) VALUES (?,?,?)",
            [(i + 1, name, "[]") for i, name in enumerate("ABCD")],
        )
        for r in seasons_history.itertuples():
            db.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES (?,?,?,?,?,?,?)",
                (
                    r.match_id,
                    r.source,
                    r.league,
                    "ABCD".index(r.home) + 1,
                    "ABCD".index(r.away) + 1,
                    r.kickoff_utc.isoformat(),
                    "finished",
                ),
            )
            db.execute(
                "INSERT INTO results(match_id,home_goals,away_goals,available_utc,"
                "available_source) VALUES (?,?,?,?,?)",
                (r.match_id, r.home_goals, r.away_goals, r.available_utc.isoformat(), "assumed"),
            )
    config = tmp_path / "params.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    "英超": dict(
                        half_life_days=365,
                        selection_as_of_utc="2025-07-01T00:00Z",
                        draw_calibration=True,
                        p2_ready=False,
                        evaluation_windows=[
                            dict(
                                validation_season="2526",
                                half_life_days=365,
                                selection_as_of_utc="2025-07-01T00:00Z",
                                draw_calibration=True,
                            )
                        ],
                    )
                },
            }
        )
    )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = train(
        db=database,
        league="英超",
        source="fd",
        seasons=["2526"],
        fold="monthly",
        params_file=config,
        output=tmp_path / "dc.pkl",
        as_of=pd.Timestamp("2026-07-01T00:00Z"),
    )
    assert result["half_life_days"] == 365 and result["parameter_source"] == "params.json"
    assert result["database_sha256"] == before == hashlib.sha256(database.read_bytes()).hexdigest()
    assert result["backtest"]["seasons"]["2526"]["model"]["n"] > 0
    calibrated_override = train(
        db=database,
        league="英超",
        source="fd",
        seasons=["2526"],
        fold="monthly",
        params_file=config,
        output=tmp_path / "calibrated-override.pkl",
        as_of="2026-07-01T00:00Z",
        half_life_days=180,
    )
    assert calibrated_override["draw_calibration"] is True
    assert all(f["draw_calibration"] for f in calibrated_override["backtest"]["folds"])
    assert (
        main(
            [
                "train",
                "--db",
                str(database),
                "--league",
                "英超",
                "--source",
                "fd",
                "--seasons",
                "2526",
                "--fold",
                "monthly",
                "--as-of",
                "2026-07-01",
                "--params-file",
                str(config),
                "--half-life-days",
                "180",
                "--no-draw-calibration",
                "--output",
                str(tmp_path / "override.pkl"),
            ]
        )
        == 0
    )
    assert DixonColes.load(tmp_path / "override.pkl").parameter_source == "cli"
    overridden = DixonColes.load(tmp_path / "override.pkl")
    assert overridden.draw_calibration is False
    assert overridden.parameter_provenance["draw_calibration"] is False
    assert overridden.parameter_provenance["draw_calibration_source"] == "cli"
    from zucai.predict.tuning import tune

    tuned = tune(
        db=database,
        league="英超",
        source="fd",
        grid=[90],
        seasons=["2425"],
        as_of="2026-07-01T00:00Z",
        params_file=tmp_path / "tuned.json",
    )
    assert tuned["weekly"]["fold"] == "weekly"
    assert tuned["weekly"]["seasons"]["2526"]["model"]["n"] > 0
    assert tuned["parameters"]["p2_ready"] is False  # Only one complete validation season.
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_readiness_cannot_relabel_old_or_incompatible_model(seasons_history, tmp_path):
    import json

    from zucai.predict.parameters import model_readiness

    cutoff = pd.Timestamp("2026-07-02T00:00Z")
    model = DixonColes(365).fit(seasons_history, as_of=cutoff)
    policy = dict(history_scope="cumulative", strict_availability=False, replay_known_after=None)
    model.database_sha256 = "a" * 64
    model.training_policy = policy
    config = tmp_path / "params.json"
    entry = dict(
        half_life_days=365,
        selection_as_of_utc="2026-07-01T00:00Z",
        generated_utc="2026-07-01T00:00Z",
        sources=["fd"],
        unknown_team_prior="weakest_quartile_half_shrinkage",
        draw_calibration=False,
        p2_ready=True,
        database_sha256="a" * 64,
        training_policy=policy,
        p2_evidence={"as_of_utc": "2026-07-01T00:00Z", "seasons": ["2425", "2526"]},
    )
    config.write_text(json.dumps({"schema_version": 1, "leagues": {"英超": entry}}))
    assert model_readiness(model, as_of=cutoff, params_file=config)["p2_ready"] is True
    assert (
        model_readiness(model, as_of="2025-01-01T00:00Z", params_file=config)["p2_ready"] is False
    )
    model.half_life_days = 90
    assert model_readiness(model, as_of=cutoff, params_file=config)["p2_ready"] is False
    assert model.half_life_days == 90  # A config never changes fitted weights.


def test_gate_rejects_empty_season_and_failed_folds_even_with_large_total():
    from zucai.predict.parameters import p2_gate

    rows = {}
    for season, n in [("2425", 600), ("2526", 0)]:
        rows[season] = dict(
            complete=True,
            skipped={},
            **{
                key: dict(n=n, log_loss=ll if n else None)
                for key, ll in [
                    ("model", 1.0),
                    ("uniform", np.log(3)),
                    ("closing", 0.96),
                    ("closing_paired_model", 1.0),
                ]
            },
        )
    report = dict(fold="weekly", seasons=rows)
    assert not p2_gate(report, league="英超", as_of="2026-11-01T00:00Z")["p2_ready"]
    for value in rows["2526"].values():
        if isinstance(value, dict) and "n" in value:
            value["n"] = 600
            value["log_loss"] = 1.0
    rows["2526"]["uniform"]["log_loss"] = np.log(3)
    rows["2526"]["closing"]["log_loss"] = 0.96
    rows["2526"]["skipped"] = {"fit_failed": 20}
    assert not p2_gate(report, league="英超", as_of="2026-11-01T00:00Z")["p2_ready"]


def test_week_crossing_season_boundary_and_jc_subset(seasons_history):
    history = seasons_history.copy()
    first = history.index[history.kickoff_utc >= pd.Timestamp("2025-07-01T00:00Z")][0]
    history.loc[first, "counterpart_id"] = "jc:linked"
    # The importer preserves an older API counterpart when FD links to the jc row.
    history.loc[first + 1, "source"] = "jc"
    history.loc[first + 1, "counterpart_id"] = "fdorg:existing-link"
    report = backtest.evaluate(
        history, {}, seasons=["2526"], as_of="2025-08-01T00:00Z", parameters_by_season={"2526": 365}
    )
    assert report["folds"][0]["cutoff_utc"] == "2025-06-30T23:59:59.999999+00:00"
    assert report["folds"][0]["half_life_days"] == 365
    assert report["jc_linked"]["n"] == 2
    assert report["seasons"]["2526"]["complete"] is False


def test_gate_rejects_unavailable_or_unreconciled_outcomes():
    from zucai.predict.parameters import p2_gate

    row = dict(
        complete=True,
        skipped={},
        in_period=380,
        unavailable_labels=100,
        model=dict(n=280, log_loss=1.0),
        uniform=dict(n=280, log_loss=np.log(3)),
        closing=dict(n=280, log_loss=0.96),
        closing_paired_model=dict(n=280, log_loss=1.0),
    )
    report = dict(fold="weekly", seasons={"2425": row, "2526": row})
    assert not p2_gate(report, league="英超", as_of="2026-11-01T00:00Z")["p2_ready"]
    row["unavailable_labels"] = 0
    assert not p2_gate(report, league="英超", as_of="2026-11-01T00:00Z")["p2_ready"]


def test_readiness_rejects_replay_other_corpus_and_expired_seasons(seasons_history, tmp_path):
    import json

    from zucai.predict.parameters import model_readiness

    model = DixonColes(365).fit(seasons_history, as_of=pd.Timestamp("2026-07-02T00:00Z"))
    policy = dict(history_scope="cumulative", strict_availability=False, replay_known_after=None)
    model.database_sha256 = "a" * 64
    model.training_policy = policy
    entry = dict(
        half_life_days=365,
        selection_as_of_utc="2026-07-01T00:00Z",
        generated_utc="2026-07-01T00:00Z",
        sources=["fd"],
        unknown_team_prior="weakest_quartile_half_shrinkage",
        draw_calibration=False,
        database_sha256="a" * 64,
        training_policy=policy,
        p2_ready=True,
        p2_evidence={"as_of_utc": "2026-07-01T00:00Z", "seasons": ["2425", "2526"]},
    )
    config = tmp_path / "params.json"
    config.write_text(json.dumps({"schema_version": 1, "leagues": {"英超": entry}}))
    assert model_readiness(model, as_of="2026-11-01T00:00Z", params_file=config)["p2_ready"]
    assert not model_readiness(model, as_of="2027-08-01T00:00Z", params_file=config)["p2_ready"]
    model.database_sha256 = "b" * 64
    assert not model_readiness(model, as_of="2026-11-01T00:00Z", params_file=config)["p2_ready"]
    model.database_sha256 = "a" * 64
    model.replayed_training_share = 1.0
    model.availability_basis = "replayed"
    assert not model_readiness(model, as_of="2026-11-01T00:00Z", params_file=config)["p2_ready"]


def test_missing_counterparts_survive_pandas_string_nulls(seasons_history):
    history = seasons_history.copy()
    # pandas 3 converts SQL NULL to NaN in a mixed string column.
    history["counterpart_id"] = pd.Series([None] * len(history), dtype="str")
    history.loc[0, "counterpart_id"] = "jc:older-link"
    report = backtest.evaluate(history, {}, seasons=["2526"], as_of="2025-08-01T00:00Z")
    assert report["model"]["n"] > 0
    assert report["jc_linked"]["n"] == 0


def test_draw_intercept_matches_training_frequency_and_stays_finite():
    from zucai.predict.calibration import fit_draw_offset

    assert fit_draw_offset([0.2] * 4, [0, 1, 1, 0], [1] * 4) == pytest.approx(np.log(4))
    assert np.isfinite(fit_draw_offset([0.2] * 4, [0] * 4, [1] * 4))
    assert np.isfinite(fit_draw_offset([0.2] * 4, [1] * 4, [1] * 4))


def test_draw_calibration_uses_only_eligible_results_and_no_prices(seasons_history):
    cutoff = pd.Timestamp("2024-07-01T00:00Z")
    first = DixonColes(365, draw_calibration=True).fit(seasons_history, as_of=cutoff)
    changed = seasons_history.copy()
    future = changed.available_utc >= cutoff
    changed.loc[future, ["home_goals", "away_goals"]] = [8, 8]
    changed["closing"] = 0.999
    changed["market_home"] = 0.001
    second = DixonColes(365, draw_calibration=True).fit(changed, as_of=cutoff)
    assert first.draw_offset == second.draw_offset
    assert first.calibration_training_rows == first.training_rows
    row = dict(
        home="A", away="B", source="fd", league="英超", kickoff_utc=cutoff + pd.Timedelta(days=1)
    )
    matrix, meta = first.predict_with_metadata(row)
    np.testing.assert_array_equal(matrix, second.predict_score_matrix(row))
    assert matrix.min() >= 0 and matrix.sum() == pytest.approx(1)
    assert meta["draw_calibration"] is True
    assert meta["draw_offset"] == first.draw_offset
    baseline = DixonColes(365).fit(seasons_history, as_of=cutoff).predict_score_matrix(row)
    off_diagonal = ~np.eye(10, dtype=bool)
    np.testing.assert_allclose(
        matrix[off_diagonal] / baseline[off_diagonal], np.full(90, matrix[0, 1] / baseline[0, 1])
    )


def test_draw_calibration_discloses_invalid_training_predictions(seasons_history, monkeypatch):
    from zucai.predict.dixon_coles import DixonColesGoalModel

    original = DixonColesGoalModel.predict

    def some_invalid(engine, home, away, **kwargs):
        if (home, away) == ("A", "B"):
            raise ValueError("goal_matrix contains negative probabilities")
        return original(engine, home, away, **kwargs)

    monkeypatch.setattr(DixonColesGoalModel, "predict", some_invalid)
    model = DixonColes(365, draw_calibration=True).fit(seasons_history, as_of="2024-07-01T00:00Z")
    assert 0 < model.calibration_training_rows < model.training_rows
    assert (
        model.calibration_skipped_training_rows
        == model.training_rows - model.calibration_training_rows
    )


def test_auto_calibration_switch_comes_only_from_selection_season(seasons_history, monkeypatch):
    from zucai.predict import tuning

    calls = []

    def measured(history, market, **kwargs):
        calls.append(kwargs)
        return dict(
            model=dict(n=100, log_loss=1.0, draw_bias=-0.04),
            in_period=100,
            unavailable_labels=0,
            skipped={},
            folds=[],
        )

    monkeypatch.setattr(tuning, "evaluate", measured)
    result = tuning.rolling_origin(
        seasons_history, {}, grid=[365], seasons=["2425"], as_of="2026-07-01T00:00Z"
    )
    assert calls[0]["draw_calibration"] is False
    assert calls[1]["draw_calibration"] is True
    assert result["windows"][0]["draw_calibration"] is True
    assert result["windows"][0]["selection_draw_bias"] == -0.04
