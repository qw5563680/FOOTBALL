"""Offline synthetic implementation checks, never model accuracy evidence."""

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from zucai.predict.base import utc

ROOT = Path(__file__).parents[1]
DESIGN = json.loads((ROOT / "data/fixtures/t38/experiment-design.json").read_bytes())
PROTOCOL = ROOT / "docs/experiments/T38-evaluation-protocol.json"


def observations():
    samples = json.loads((ROOT / DESIGN["source_rows"]).read_bytes())["rows"]
    rows = []
    for i, sample in enumerate(samples):
        stamp = utc(DESIGN["training_start"]) + np.timedelta64(i, "D")
        differences = DESIGN["injury_differences"][i % len(DESIGN["injury_differences"])]
        rows.append(
            dict(
                match_id=f"fd:injury-fixture-{i}",
                kickoff_utc=stamp,
                available_utc=stamp + np.timedelta64(3, "h"),
                feature_cutoff=stamp,
                inputs={
                    **{
                        k: sample[k]
                        for k in ("home_for", "away_for", "home_against", "away_against")
                    },
                    "missing_diff": differences[0],
                },
                optional_inputs={"missing_last_starters_diff": differences[1]},
                home_goals=sample["home_goals"],
                away_goals=sample["away_goals"],
            )
        )
    return rows


def test_fitted_injury_coefficients_and_signed_design():
    from zucai.predict.experiments.injury_intensity import InjuryIntensity

    rows = observations()
    fitted = InjuryIntensity(window=5).fit(rows, as_of="2024-09-01T00:00:00Z")
    assert "missing_diff" in fitted.coefficients
    assert "missing_last_starters_diff" not in fitted.coefficients
    home, away = fitted._design(rows[0]["inputs"])
    assert len(home) == len(away) == 5
    assert home[-1] == 2 and away[-1] == -2
    matrix, metadata = fitted.predict(rows[0]["inputs"], calibrated=True)
    assert np.isfinite(matrix).all() and matrix.min() >= 0
    assert matrix.sum() == pytest.approx(1)
    assert metadata["model_name"] == "dc-sot-injuries-v2"
    assert metadata["model_type"] == "TEAM" and not metadata["bet_allowed"]
    assert metadata["training_availability_mode"] == "backfill_replay"
    assert metadata["target_availability_mode"] == "strict_capture_time"
    changed = {**rows[0]["inputs"], "missing_diff": 8}
    assert not np.allclose(matrix, fitted.predict(changed, calibrated=True)[0])


def test_ablation_uses_identical_full_mask_and_exact_unchanged_host_math():
    from zucai.predict.experiments.injury_intensity import InjuryIntensity
    from zucai.predict.experiments.intensity import INPUTS, StatsIntensity

    rows = observations()
    rows[0]["inputs"]["missing_diff"] = None
    full = InjuryIntensity(window=5).fit(rows, as_of="2024-09-01T00:00:00Z")
    ablated = InjuryIntensity(window=5, ablation=True).fit(rows, as_of="2024-09-01T00:00:00Z")
    assert full.training_ids == ablated.training_ids
    assert rows[0]["match_id"] not in full.training_ids
    host = StatsIntensity(stat_key="shots_on_target", window=5).fit(
        [{**r, "inputs": {k: r["inputs"][k] for k in INPUTS}} for r in rows[1:]],
        as_of="2024-09-01T00:00:00Z",
    )
    assert host.coefficients == ablated.coefficients
    left = rows[1]["inputs"]
    right = {**left, "missing_diff": 20}
    assert np.array_equal(ablated.predict(left)[0], ablated.predict(right)[0])
    assert np.array_equal(ablated.predict(left)[0], host.predict({k: left[k] for k in INPUTS})[0])
    with pytest.raises(ValueError, match="missing"):
        ablated.predict(rows[0]["inputs"])


def test_training_label_boundaries_and_market_whitelist():
    from zucai.predict.experiments.injury_intensity import InjuryIntensity

    rows = observations()
    cutoff = utc("2024-09-01T00:00:00Z")
    invalid = [
        {**rows[0], "match_id": "fd:before-kick", "available_utc": rows[0]["kickoff_utc"]},
        {**rows[0], "match_id": "fd:at-cutoff", "available_utc": cutoff},
        {**rows[0], "match_id": "fd:future", "available_utc": cutoff + np.timedelta64(1, "D")},
        {**rows[0], "match_id": "fd:unknown", "available_utc": None},
    ]
    base = InjuryIntensity(window=5).fit(rows, as_of=cutoff)
    check = InjuryIntensity(window=5).fit([*rows, *invalid], as_of=cutoff)
    assert check.coefficients == base.coefficients
    assert check.training_ids == base.training_ids
    with pytest.raises(ValueError, match="白名单"):
        InjuryIntensity(window=5).fit(
            [{**rows[0], "inputs": {**rows[0]["inputs"], "market_h": 0.9}}], as_of=cutoff
        )
    with pytest.raises(ValueError, match="白名单"):
        InjuryIntensity(window=5).fit(
            [{**rows[0], "inputs": {**rows[0]["inputs"], **rows[0]["optional_inputs"]}}],
            as_of=cutoff,
        )
    with pytest.raises(ValueError, match="截点"):
        InjuryIntensity(window=5).fit(
            [{**rows[0], "feature_cutoff": cutoff}, *rows[1:]], as_of=cutoff
        )


def test_protocol_is_frozen_and_splits_disjoint():
    from zucai.predict.experiments.injury_evaluate import load_protocol

    protocol = load_protocol(PROTOCOL)
    assert protocol["selection_end"] == protocol["evaluation_start"]
    for key, value in (
        ("training_availability_mode", "strict"),
        ("target_availability_mode", "backfill_replay"),
        ("selection_end", "2025-08-01T00:00:00Z"),
        ("injury_inputs", ["missing_diff", "missing_last_starters_diff"]),
        ("target_availability_modes", ["strict_capture_time"]),
        ("lineup_subset", {**protocol["lineup_subset"], "fill_missing": True}),
        ("host", "dixon-coles"),
    ):
        with pytest.raises(ValueError, match="冻结"):
            load_protocol({**protocol, key: value})


def metric_row(key, p=0.6, *, complete=True):
    from zucai.predict.experiments.injury_evaluate import COLUMNS

    return dict(
        match_id=key,
        kickoff_utc="2025-07-02T15:00:00+00:00",
        actual=0,
        league="英超",
        season="2526",
        cold_start=False,
        statistics_missing=False,
        injuries_missing=not complete,
        experimental=complete,
        lineup_complete=complete,
        target_availability_mode="strict_capture_time",
        missing_reasons=[] if complete else ["injuries_missing"],
        variants={
            name: dict(probabilities=[p, (1 - p) / 2, (1 - p) / 2], score_brier=0.5)
            if complete or name.startswith("baseline")
            else None
            for name in COLUMNS
        },
    )


def test_window_selection_compares_only_shared_true_candidates(monkeypatch):
    from zucai.predict.experiments.injury_evaluate import InjuryWalkForwardExperiment, load_protocol

    experiment = InjuryWalkForwardExperiment(
        pd.DataFrame(), None, None, protocol=load_protocol(PROTOCOL)
    )
    trials = {
        5: [metric_row("shared", 0.5), metric_row("only-five", 0.99)],
        10: [metric_row("shared", 0.6), metric_row("missing", complete=False)],
    }
    monkeypatch.setattr(
        experiment, "run", lambda *, window, **kwargs: (deepcopy(trials[window]), [])
    )
    result = experiment.select()
    assert result["window"] == 10 and result["common_candidate_n"] == 1
    assert result["common_candidate_ids"] == ["shared"]
    assert {trial["metrics"]["n"] for trial in result["trials"].values()} == {1}
    trials[10] = [metric_row("only-ten", 0.6)]
    empty = experiment.select()
    assert empty["window"] == 5 and not empty["evidence_sufficient"]
    assert empty["reason"] == "no_common_candidate_selection_observations"


def test_zero_common_evidence_is_null_and_groups_retain_denominators():
    from zucai.predict.experiments.injury_evaluate import COLUMNS, group_summary, load_protocol

    row = metric_row("fd:missing", complete=False)
    summary = group_summary([row], load_protocol(PROTOCOL))
    assert summary["status"] == "insufficient_evidence"
    assert summary["targets"] == 1 and summary["common_targets"] == 0
    assert summary["models"]["baseline_raw"]["n"] == 1
    assert {summary["common_experiment"][name]["log_loss"] for name in COLUMNS} == {None}
    assert summary["models"]["candidate_raw"]["log_loss"] is None
    assert len(summary["paired"]) == 6
    for comparison in summary["paired"].values():
        assert comparison["common_experiment"]["n"] == 0
        assert comparison["common_experiment"]["log_loss"] is None
    missing = summary["groups"]["injuries_missing"]["true"]
    assert missing["targets"] == 1 and missing["common_targets"] == 0
    assert len(missing["paired"]) == 6
    assert summary["groups"]["cold_start"]["unknown"]["targets"] == 0


def test_paired_block_bootstrap_is_reproducible():
    from zucai.predict.experiments.injury_evaluate import group_summary, load_protocol

    rows = [metric_row("a"), metric_row("b"), metric_row("c")]
    rows[1]["kickoff_utc"] = "2025-07-12T15:00:00Z"
    rows[2]["kickoff_utc"] = "2025-07-13T15:00:00Z"
    for row in rows:
        row["variants"]["baseline_raw"]["probabilities"] = [0.5, 0.3, 0.2]
    protocol = load_protocol(PROTOCOL)
    left, right = group_summary(rows, protocol), group_summary(rows, protocol)
    assert left == right
    paired = left["paired"]["candidate_raw-baseline_raw"]["common_experiment"]
    assert paired["n"] == 3 and paired["blocks"] == 2
    assert paired["replicates"] == 2000 and paired["seed"] == 3301
    assert paired["log_loss"]["interval"] == pytest.approx([np.log(0.5 / 0.6)] * 2)


def timeline():
    rows, index = [], 0
    samples = observations()
    for phase in ("training", "selection", "evaluation"):
        for offset in range(DESIGN[f"{phase}_rows"]):
            sample = deepcopy(samples[index])
            stamp = utc(DESIGN[f"{phase}_start"]) + np.timedelta64(offset, "D")
            sample.update(
                kickoff_utc=stamp,
                available_utc=stamp + np.timedelta64(3, "h"),
                feature_cutoff=stamp,
                home=DESIGN["teams"][index % 3],
                away=DESIGN["teams"][(index + 1) % 3],
                home_team_id=index % 3 + 1,
                away_team_id=(index + 1) % 3 + 1,
                source="fd",
                league="英超",
            )
            rows.append(sample)
            index += 1
    return rows


class FixtureStatistics:
    provider = "fd"

    def __init__(self, rows, *, strict_missing=False):
        self.rows = {row["match_id"]: row for row in rows}
        self.calls = []
        self.strict_missing = strict_missing

    def build(self, row, *, as_of, mode, windows, **kwargs):
        self.calls.append((row["match_id"], mode, utc(as_of)))
        values = self.rows[row["match_id"]]["inputs"]
        cells = {}
        for side in ("home", "away"):
            for direction in ("for", "against"):
                value = (
                    None
                    if mode == "strict" and self.strict_missing
                    else values[f"{side}_{direction}"]
                )
                cells[f"{side}_all_{windows[0]}_shots_on_target_{direction}"] = dict(
                    decay=value, missing_reasons=["fixture_after_cutoff"] if value is None else []
                )
        return dict(feature_cutoff=utc(as_of).isoformat(), availability_mode=mode, cells=cells)


class FixtureInjuries:
    def __init__(self, rows, *, strict_missing=False, lineup_missing=False):
        self.rows = {row["match_id"]: row for row in rows}
        self.calls = []
        self.strict_missing = strict_missing
        self.lineup_missing = lineup_missing

    def build(self, row, *, as_of, mode):
        self.calls.append((row["match_id"], mode, utc(as_of)))
        sample = self.rows[row["match_id"]]
        values = {**sample["inputs"], **sample["optional_inputs"]}
        result = dict(feature_cutoff=utc(as_of).isoformat(), availability_mode=mode)
        for side, sign in (("home", 1), ("away", -1)):
            result[side] = {}
            for metric, key in (
                ("missing_n", "missing_diff"),
                ("missing_last_starters_n", "missing_last_starters_diff"),
            ):
                missing = (mode == "strict" and self.strict_missing) or (
                    self.lineup_missing and key == "missing_last_starters_diff"
                )
                result[side][metric] = dict(
                    value=None if missing else max(0, values[key] * sign),
                    n=0 if missing else 1,
                    missing_reason="fixture_after_cutoff" if missing else None,
                    evidence=[],
                )
        return result


def test_walk_forward_trains_replay_and_predicts_strict_without_fallback():
    from zucai.predict.experiments.injury_evaluate import (
        COLUMNS,
        InjuryWalkForwardExperiment,
        load_protocol,
    )

    rows = timeline()
    statistics, injuries = FixtureStatistics(rows), FixtureInjuries(rows)
    protocol = load_protocol(PROTOCOL)
    experiment = InjuryWalkForwardExperiment(
        pd.DataFrame(rows), statistics, injuries, protocol=protocol
    )
    predictions, folds = experiment.run(
        window=5, start=protocol["evaluation_start"], end=protocol["evaluation_end"], fold="weekly"
    )
    assert len(predictions) == DESIGN["evaluation_rows"]
    assert all(row["variants"][name] for row in predictions for name in COLUMNS)
    for fit in folds:
        assert (
            fit["models"]["candidate"]["training_ids"] == fit["models"]["ablation"]["training_ids"]
        )
        for key in fit["models"]["candidate"]["training_ids"]:
            sample = experiment.sample_cache[key, 5]
            assert (
                utc(sample["kickoff_utc"]) < utc(sample["available_utc"]) < utc(fit["fit_cutoff"])
            )
            assert sample["features"]["availability_mode"] == "backfill_replay"
    for row in predictions:
        assert utc(row["fit_cutoff"]) < utc(row["feature_cutoff"]) == utc(row["kickoff_utc"])
        assert row["features"]["availability_mode"] == "strict_capture_time"
    assert {mode for _, mode, _ in statistics.calls} == {"strict", "backfill_replay"}
    assert {mode for _, mode, _ in injuries.calls} == {"strict", "backfill_replay"}

    missing = InjuryWalkForwardExperiment(
        pd.DataFrame(rows),
        FixtureStatistics(rows),
        FixtureInjuries(rows, strict_missing=True),
        protocol=protocol,
    )
    predictions, _ = missing.run(
        window=5, start=protocol["evaluation_start"], end=protocol["evaluation_end"], fold="weekly"
    )
    assert all(row["variants"]["baseline_raw"] for row in predictions)
    assert all(row["variants"]["candidate_raw"] is None for row in predictions)
    assert all(row["variants"]["ablation_raw"] is None for row in predictions)
    assert all(not row["experimental"] and row["injuries_missing"] for row in predictions)


def test_target_tables_select_independently_and_keep_strict_empty():
    from zucai.predict.experiments.injury_evaluate import (
        COLUMNS,
        evaluate_group,
        group_summary,
        load_protocol,
    )

    rows = timeline()
    protocol = load_protocol(PROTOCOL)
    statistics = FixtureStatistics(rows)
    injuries = FixtureInjuries(rows, strict_missing=True)
    tables = {
        mode: evaluate_group(
            pd.DataFrame(rows),
            statistics,
            injuries,
            protocol=protocol,
            target_availability_mode=mode,
        )
        for mode in DESIGN["target_availability_modes"]
    }
    strict, replay = (tables[mode] for mode in DESIGN["target_availability_modes"])
    assert not strict["selection"]["evidence_sufficient"]
    assert strict["selection"]["common_candidate_n"] == 0
    assert replay["selection"]["evidence_sufficient"]
    assert replay["selection"]["common_candidate_n"] == DESIGN["selection_rows"]
    assert all(row["variants"]["candidate_raw"] is None for row in strict["predictions"])
    assert all(row["variants"]["candidate_raw"] for row in replay["predictions"])
    for mode, table in tables.items():
        assert table["selection"]["target_availability_mode"] == mode
        assert table["training_features"]
        for sample in table["training_features"]:
            assert sample["target_availability_mode"] == mode
            assert sample["features"]["availability_mode"] == "backfill_replay"
        for row in table["predictions"]:
            assert row["target_availability_mode"] == row["availability_mode"] == mode
            assert row["features"]["availability_mode"] == mode
            assert row["injury_replay_assumption"] == protocol["injury_replay_assumption"]
            for name in COLUMNS:
                if row["variants"][name]:
                    assert row["variants"][name]["target_availability_mode"] == mode
        for fold in table["folds"]:
            assert fold["target_availability_mode"] == mode
            for model in fold["models"].values():
                assert model["target_availability_mode"] == mode
        summary = group_summary(table["predictions"], protocol, target_availability_mode=mode)
        assert summary["targets"] == DESIGN["evaluation_rows"]
        expected = 0 if mode == "strict_capture_time" else DESIGN["evaluation_rows"]
        assert summary["common_targets"] == expected
    with pytest.raises(ValueError, match="混合"):
        group_summary(strict["predictions"] + replay["predictions"], protocol)


@pytest.mark.parametrize("target_mode", DESIGN["target_availability_modes"])
def test_missing_lineups_do_not_gate_main_candidate_or_change_fits(target_mode):
    from zucai.predict.experiments.injury_evaluate import (
        InjuryWalkForwardExperiment,
        group_summary,
        load_protocol,
    )

    rows = timeline()
    protocol = load_protocol(PROTOCOL)
    results = []
    for lineup_missing in (False, True):
        experiment = InjuryWalkForwardExperiment(
            pd.DataFrame(rows),
            FixtureStatistics(rows),
            FixtureInjuries(rows, lineup_missing=lineup_missing),
            protocol=protocol,
            target_availability_mode=target_mode,
        )
        predictions, folds = experiment.run(
            window=5,
            start=protocol["evaluation_start"],
            end=protocol["evaluation_end"],
            fold="weekly",
        )
        for row in predictions:
            assert row["variants"]["candidate_raw"] is not None
            assert not row["injuries_missing"]
            assert row["lineup_complete"] is not lineup_missing
            assert (row["optional_inputs"]["missing_last_starters_diff"] is None) is lineup_missing
            assert "missing_last_starters_diff" not in row["features"]["inputs"]
        summary = group_summary(predictions, protocol, target_availability_mode=target_mode)
        assert summary["common_targets"] == DESIGN["evaluation_rows"]
        subset = summary["lineup_subset"]
        assert subset["all_targets"] == DESIGN["evaluation_rows"]
        assert subset["targets"] == (0 if lineup_missing else DESIGN["evaluation_rows"])
        assert subset["fill_missing"] is False
        if lineup_missing:
            assert subset["models"]["candidate_raw"]["log_loss"] is None
            assert subset["paired"]["candidate_raw-ablation_raw"]["all"]["n"] == 0
        results.append((predictions, folds))
    assert results[0][1] == results[1][1]
    assert [row["variants"] for row in results[0][0]] == [row["variants"] for row in results[1][0]]


def test_selection_fits_cannot_see_evaluation_labels():
    from zucai.predict.experiments.injury_evaluate import InjuryWalkForwardExperiment, load_protocol

    rows = timeline()
    history = pd.DataFrame(rows)
    protocol = load_protocol(PROTOCOL)
    original = InjuryWalkForwardExperiment(
        history, FixtureStatistics(rows), FixtureInjuries(rows), protocol=protocol
    ).select()
    altered = history.copy()
    altered.loc[
        altered.kickoff_utc >= utc(protocol["evaluation_start"]), ["home_goals", "away_goals"]
    ] = 9
    changed = InjuryWalkForwardExperiment(
        altered, FixtureStatistics(rows), FixtureInjuries(rows), protocol=protocol
    ).select()
    assert original == changed
    future = set(history.loc[history.kickoff_utc >= utc(protocol["evaluation_start"]), "match_id"])
    assert not future.intersection(
        key
        for trial in original["trials"].values()
        for fold in trial["folds"]
        for model in fold["models"].values()
        for key in model["training_ids"]
    )


def test_all_missing_strict_targets_skip_fitting_and_training_features(monkeypatch):
    from zucai.predict.experiments.injury_evaluate import (
        COLUMNS,
        InjuryWalkForwardExperiment,
        load_protocol,
    )
    from zucai.predict.experiments.injury_intensity import InjuryIntensity
    from zucai.predict.experiments.intensity import StatsIntensity

    rows = timeline()
    protocol = load_protocol(PROTOCOL)
    statistics = FixtureStatistics(rows, strict_missing=True)
    injuries = FixtureInjuries(rows, strict_missing=True)
    experiment = InjuryWalkForwardExperiment(
        pd.DataFrame(rows), statistics, injuries, protocol=protocol
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "No fitter or replay sample builder should run for absent target inputs"
        )

    monkeypatch.setattr(StatsIntensity, "fit", forbidden)
    monkeypatch.setattr(InjuryIntensity, "fit", forbidden)
    monkeypatch.setattr(experiment, "samples", forbidden)
    selection = experiment.select()
    assert selection["window"] == 5 and not selection["evidence_sufficient"]
    predictions, folds = experiment.run(
        window=5, start=protocol["evaluation_start"], end=protocol["evaluation_end"], fold="weekly"
    )
    assert len(predictions) == DESIGN["evaluation_rows"]
    assert all(row["variants"][name] is None for row in predictions for name in COLUMNS)
    assert experiment.sample_cache == {}
    for fold in folds:
        assert fold["models"] == {}
        assert set(fold["errors"].values()) == {"fit_not_attempted:no_eligible_target_inputs"}
        for info in fold["model_status"].values():
            assert info["status"] == "fit_not_attempted" and info["eligible_target_inputs"] == 0
    assert {mode for _, mode, _ in statistics.calls} == {"strict"}


@pytest.fixture
def injury_database(tmp_path):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, MatchStat, Result, Team

    database = tmp_path / "injury-experiment.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(id=index + 1, name=name) for index, name in enumerate(DESIGN["teams"]))
        session.flush()
        for row in timeline():
            session.add(
                Match(
                    match_id=row["match_id"],
                    source="fd",
                    league="英超",
                    status="finished",
                    home_team_id=row["home_team_id"],
                    away_team_id=row["away_team_id"],
                    kickoff_utc=row["kickoff_utc"],
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=row["match_id"],
                    home_goals=row["home_goals"],
                    away_goals=row["away_goals"],
                    available_utc=row["available_utc"],
                    available_source="assumed",
                )
            )
            for side in ("home", "away"):
                value = row["inputs"][f"{side}_for"]
                session.add(
                    MatchStat(
                        provider="fd",
                        provider_fixture_id=row["match_id"],
                        endpoint="csv",
                        record_key=f"{side}:shots_on_target",
                        match_id=row["match_id"],
                        team_id=row[f"{side}_team_id"],
                        side=side,
                        period_basis="90",
                        extra_time_included=False,
                        eligible_90=True,
                        captured_utc=row["available_utc"],
                        available_utc=None,
                        availability_basis="backfilled",
                        raw_sha256="a" * 64,
                        raw_location="synthetic_fixture",
                        parser_version="fixture",
                        missing_reason=None,
                        version=1,
                        stat_key="shots_on_target",
                        raw_value=value,
                        value=value,
                        unit="count",
                    )
                )
    yield database, engine
    engine.dispose()


def test_read_only_command_archives_null_evidence_and_all_fits_precede_market(
    injury_database, tmp_path, monkeypatch
):
    import zipfile

    from zucai.features import injuries as features_module
    from zucai.predict.experiments import injury_command as command

    database, _ = injury_database
    before = command.sha256(database)
    phases = []
    original_evaluate, original_attach = command.evaluate_group, command.attach_market

    def evaluate(*args, **kwargs):
        phases.append("fit")
        return original_evaluate(*args, **kwargs)

    def market(*args, **kwargs):
        phases.append("market")
        assert phases.count("fit") == 4
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(command, "evaluate_group", evaluate)
    monkeypatch.setattr(command, "attach_market", market)
    monkeypatch.setattr(
        features_module,
        "InjuryFeatures",
        lambda session: FixtureInjuries(timeline(), strict_missing=True, lineup_missing=True),
    )
    output = tmp_path / "output"
    result = command.run(
        database,
        output,
        source_commit="synthetic-fixture",
        code_commit="fixture-code",
        leagues=["英超", "德甲"],
        run_id="fixture",
    )
    assert command.sha256(database) == before
    assert phases == ["fit"] * 4 + ["market"] * 4
    assert result["status"] == "insufficient_evidence" and result["common_targets"] == 0
    summary = json.loads((output / "evidence/summary.json").read_bytes())
    assert summary["pooled"]["targets"] == DESIGN["evaluation_rows"]
    assert summary["pooled"]["models"]["baseline_raw"]["n"] == DESIGN["evaluation_rows"]
    assert summary["pooled"]["models"]["candidate_raw"]["n"] == 0
    assert summary["scopes"]["fd-德甲"]["targets"] == 0
    assert summary["default_target_availability_mode"] == "strict_capture_time"
    assert summary["pooled"] == summary["target_tables"]["strict_capture_time"]["pooled"]
    replay = summary["target_tables"]["backfill_replay"]
    assert replay["pooled"]["targets"] == DESIGN["evaluation_rows"]
    assert replay["pooled"]["common_targets"] == DESIGN["evaluation_rows"]
    assert replay["pooled"]["lineup_subset"]["targets"] == 0
    assert replay["scopes"]["fd-英超"]["selection_evidence_sufficient"]
    assert not summary["scopes"]["fd-英超"]["selection_evidence_sufficient"]
    assert result["target_tables"]["backfill_replay"]["common_targets"] == DESIGN["evaluation_rows"]
    manifest = json.loads((output / "evidence/input_manifest.json").read_bytes())
    assert manifest["database_sha256"] == before and manifest["read_only"] is True
    assert manifest["protocol_sha256"] == command.sha256(PROTOCOL)
    assert manifest["training_availability_mode"] == "backfill_replay"
    assert manifest["target_availability_mode"] == "strict_capture_time"
    assert manifest["target_availability_modes"] == DESIGN["target_availability_modes"]
    assert manifest["candidate"] == "dc-sot-injuries-v2"
    assert manifest["injury_inputs"] == ["missing_diff"]
    with zipfile.ZipFile(result["archive"]) as archive:
        assert "summary.json" in archive.namelist()
        for mode in ("strict_capture_time", "backfill_replay"):
            assert f"{mode}/fd-英超/training-features.jsonl.zst" in archive.namelist()
        assert archive.testzip() is None
    readme = (output / "README.md").read_text()
    assert "## strict_capture_time" in readme and "## backfill_replay" in readme
    assert "假设最新归档名单在开球前已知" in readme
    for line in (output / "evidence/SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert command.sha256(output / "evidence" / name) == digest
    assert (output / "README.md").stat().st_size + (output / "SHA256SUMS").stat().st_size < 200_000
    with pytest.raises(FileExistsError):
        command.run(database, output, source_commit="fixture", leagues=["英超"])
    assert command.sha256(database) == before


def test_release_zip_upload_and_checksum_receipts_are_verified(
    injury_database, tmp_path, monkeypatch
):
    from zucai import stats_release
    from zucai.predict.experiments import injury_command as command

    database, _ = injury_database
    uploads = []

    class FixtureRelease:
        def __init__(self, repository):
            assert repository == "fixture/repository"

        def upload(self, tag, path):
            uploads.append((tag, Path(path).name))
            return stats_release.descriptor(tag, path)

    monkeypatch.setattr(stats_release, "ReleaseStore", FixtureRelease)
    result = command.run(
        database,
        tmp_path / "published",
        source_commit="fixture",
        code_commit="fixture",
        leagues=["英超"],
        publish=True,
        repository="fixture/repository",
        release="experiments-202611",
        run_id="fixture-release",
    )
    assert uploads == [
        ("experiments-202611", "t38-injuries-fixture-release.zip"),
        ("experiments-202611", "SHA256SUMS-fixture-release"),
    ]
    assert result["publication"]["asset"]["sha256"] == result["archive_sha256"]
    assert result["publication"]["checksum_asset"]["sha256"]
    assert (tmp_path / "published/publication.json").exists()
    assert (tmp_path / "published/SHA256SUMS").read_bytes() == (
        tmp_path / "published/SHA256SUMS-fixture-release"
    ).read_bytes()


def test_market_uses_canonical_jc_target_and_strict_complete_quote(injury_database):
    from sqlalchemy.orm import Session

    from zucai.db import session_scope
    from zucai.models import Match, Odds, Result
    from zucai.predict.experiments.history import unified_history
    from zucai.predict.experiments.injury_command import attach_market, read_aligned_history

    _, engine = injury_database
    sample = timeline()[-1]
    with session_scope(engine) as session:
        session.add(
            Match(
                match_id="jc:injury-fixture",
                source="jc",
                league="英超",
                status="finished",
                home_team_id=sample["home_team_id"],
                away_team_id=sample["away_team_id"],
                kickoff_utc=sample["kickoff_utc"],
                counterpart_id=sample["match_id"],
            )
        )
        session.flush()
        session.add(
            Result(
                match_id="jc:injury-fixture",
                home_goals=sample["home_goals"],
                away_goals=sample["away_goals"],
                available_utc=sample["available_utc"],
                available_source="captured",
            )
        )
        for key, price in zip("hda", [2, 3, 4], strict=True):
            session.add(
                Odds(
                    match_id="jc:injury-fixture",
                    pool="had",
                    line=0,
                    selection=key,
                    price=price,
                    snapshot_utc=sample["kickoff_utc"] - np.timedelta64(1, "h"),
                )
            )
            session.add(
                Odds(
                    match_id="jc:injury-fixture",
                    pool="had",
                    line=0,
                    selection=key,
                    price=10,
                    snapshot_utc=sample["kickoff_utc"],
                )
            )
    with Session(engine) as session:
        statistics, games = unified_history(session, provider="fd")
        history, _ = read_aligned_history(session, games, "英超")
        assert sample["match_id"] not in set(history.match_id)
        row = metric_row("jc:injury-fixture")
        row["feature_cutoff"] = sample["kickoff_utc"].isoformat()
        previous = deepcopy(row["variants"])
        attach_market(session, [row], statistics)
        assert row["variants"]["market"]["probabilities"] == pytest.approx(
            np.array([1 / 2, 1 / 3, 1 / 4]) / (1 / 2 + 1 / 3 + 1 / 4)
        )
        assert all(row["variants"][name] == item for name, item in previous.items())


def test_readonly_url_preserves_real_fixture_receipts_and_blocks_writes(tmp_path, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from test_injuries_storage import KICKOFF
    from test_injuries_storage import injury_database as capture_database
    from test_injury_features import setup_complete
    from zucai.features import injuries as features_module
    from zucai.predict.experiments import injury_command as command

    database = capture_database.__wrapped__(tmp_path)
    setup_complete(database)
    original = features_module.InjuryFeatures
    observations = []

    def checked(session):
        assert session.get_bind().url.database == str(database)
        assert session.execute(text("PRAGMA query_only")).scalar() == 1
        with pytest.raises(OperationalError, match="readonly"):
            session.execute(text("DELETE FROM match_injuries"))
        builder = original(session)
        snapshot = builder.build(builder.matches["fd:target"], as_of=KICKOFF, mode="strict")
        observations.append(snapshot)
        return builder

    monkeypatch.setattr(features_module, "InjuryFeatures", checked)
    before = command._input_hashes(database)
    command.run(
        database,
        tmp_path / "receipt-check",
        source_commit="fixture",
        code_commit="fixture",
        leagues=["英超"],
        run_id="receipt-check",
    )
    assert command._input_hashes(database) == before
    assert observations[0]["home"]["missing_n"]["value"] == 2
    assert observations[0]["home"]["missing_last_starters_n"]["value"] == 1
    assert "state/audit/injuries_ingest_audit.csv" in before
    assert "state/audit/stats_ingest_audit.csv" in before


@pytest.mark.parametrize(
    "relative_path",
    [
        "state/audit/injuries_ingest_audit.csv",
        "state/injuries_captures/fixture.json.gz",
    ],
)
def test_changed_audit_input_rejects_output_before_publish(
    injury_database, tmp_path, monkeypatch, relative_path
):
    from zucai.predict.experiments import injury_command as command

    database, _ = injury_database
    audit = database.parent / relative_path

    def changed(*args, **kwargs):
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text("fixture mutation during read-only run\n")

    monkeypatch.setattr(command, "attach_market", changed)
    output = tmp_path / "must-not-publish"
    with pytest.raises(RuntimeError, match="哈希变化"):
        command.run(
            database, output, source_commit="fixture", code_commit="fixture", leagues=["英超"]
        )
    assert not output.exists()


def test_corrupt_release_checksum_receipt_cannot_claim_verified_publish(
    injury_database, tmp_path, monkeypatch
):
    from zucai import stats_release
    from zucai.predict.experiments import injury_command as command

    database, _ = injury_database

    class CorruptChecksumRelease:
        def __init__(self, repository):
            pass

        def upload(self, tag, path):
            receipt = stats_release.descriptor(tag, path)
            if Path(path).name.startswith("SHA256SUMS"):
                receipt["sha256"] = "0" * 64
            return receipt

    monkeypatch.setattr(stats_release, "ReleaseStore", CorruptChecksumRelease)
    output = tmp_path / "bad-receipt"
    with pytest.raises(ValueError, match="回执"):
        command.run(
            database,
            output,
            source_commit="fixture",
            code_commit="fixture",
            leagues=["英超"],
            publish=True,
            repository="fixture/repository",
            release="experiments-202611",
            run_id="bad-receipt",
        )
    assert not (output / "publication.json").exists()
    assert (output / "t38-injuries-bad-receipt.zip").is_file()
