"""Capture-time strict targets and explicitly replayed training stay separate."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from test_parallel_experiments import strict_database  # noqa: F401
from test_stats_features import builder, technical_database  # noqa: F401
from zucai.db import session_scope
from zucai.models import MatchStat, Result
from zucai.predict.base import utc
from zucai.predict.experiments.parallel import compute, raw_json

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t34a/capture-time.json").read_bytes()
)


def test_offline_early_step_has_no_sports_api_key():
    workflow = (Path(__file__).parents[1] / ".github/workflows/football-feeds.yml").read_text()
    step = workflow.split(
        "- name: Preserve captured statistics from already downloaded fd files", 1
    )[1].split("\n      - name:", 1)[0]
    assert "API_FOOTBALL_KEY" not in step
    assert "GITHUB_TOKEN" in step and "--endpoints fd" in step


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_backfilled_values_use_actual_capture_for_strict(technical_database, case):  # noqa: F811
    with session_scope(technical_database) as session:
        for row in session.scalars(select(MatchStat)):
            row.captured_utc = case["captured_utc"]
    features = builder(technical_database)
    target = features.matches[FIXTURE["target"]]
    key = "home_all_5_shots_on_target_for"
    strict = features.build(target, as_of=FIXTURE["cutoff"], mode="strict")["cells"][key]
    replay = features.build(target, as_of=FIXTURE["cutoff"], mode="backfill_replay")["cells"][key]
    assert replay["value"] == FIXTURE["expected_replay_mean"]
    assert replay["match_ids"] == FIXTURE["expected_ids"]
    if case["usable"]:
        assert strict["n"] == strict["N"] == 5
        assert strict["value"] == replay["value"]
        for evidence in strict["evidence"]:
            assert evidence["availability_basis"] == "backfilled"
            assert evidence["available_utc"] is None
            assert evidence["captured_utc"] == case["captured_utc"]
            assert evidence["effective_available_utc"] == case["captured_utc"]
    else:
        assert strict["value"] is None and strict["n"] == 0
        assert strict["missing_reasons"] == ["backfilled_not_known"]
    for evidence in replay["evidence"]:
        game = features.matches[evidence["match_id"]]
        assert utc(evidence["effective_available_utc"]) == game["kickoff_utc"] + timedelta(hours=3)


def test_t33_late_capture_still_excluded_and_replay_unchanged(technical_database):  # noqa: F811
    features = builder(technical_database)
    target = features.matches[FIXTURE["target"]]
    strict = features.build(target, as_of=FIXTURE["cutoff"], mode="strict")
    replay = features.build(target, as_of=FIXTURE["cutoff"], mode="backfill_replay")
    key = "home_all_5_shots_on_target_for"
    assert strict["cells"][key]["value"] is None
    assert strict["cells"][key]["missing_reasons"] == ["backfilled_not_known"]
    assert replay["cells"][key]["value"] == FIXTURE["expected_replay_mean"]
    assert replay["cells"][key]["match_ids"] == FIXTURE["expected_ids"]
    assert replay["validation_status"] == "historical_replay_unvalidated"


@pytest.mark.parametrize("late_target_capture", [False, True])
def test_training_replay_is_separate_from_target_strict(strict_database, late_target_capture):  # noqa: F811
    engine, _, doc, target = strict_database
    capture = utc(doc["cutoff"]) + timedelta(hours=1 if late_target_capture else -1)
    with session_scope(engine) as session:
        for row in session.scalars(select(MatchStat)):
            row.availability_basis, row.available_utc = "backfilled", None
            row.captured_utc = capture
        # A known technical window cannot authorize a label arriving at the fit cutoff.
        rejected_id = doc["rows"][-1]["match_id"]
        session.get(Result, rejected_id).available_utc = utc(doc["cutoff"])
        session.flush()
        rows, fits = compute(session, [target], cutoff=doc["cutoff"])
        frozen_rows, frozen_fits = raw_json(rows), raw_json(fits)
        again, again_fits = compute(session, [target], cutoff=doc["cutoff"])
    assert raw_json(again) == frozen_rows and raw_json(again_fits) == frozen_fits
    sot, fit = rows[0], fits[0]
    assert fit["training_rows"] >= 11
    assert rejected_id not in {r["match_id"] for r in fit["training"]}
    assert fit["training_rejected"]["label_not_known_before_fit"] == 1
    for evidence in [fit, sot, sot["features"], *[r["features"] for r in fit["training"]]]:
        assert evidence["training_availability_mode"] == "backfill_replay"
        assert evidence["target_availability_mode"] == "strict_capture_time"
    for observation in fit["training"]:
        assert (
            utc(observation["kickoff_utc"]) < utc(observation["available_utc"]) < utc(doc["cutoff"])
        )
        assert observation["features"]["availability_mode"] == "backfill_replay"
        for cell in observation["features"]["cells"].values():
            for evidence in cell["evidence"]:
                assert utc(evidence["effective_available_utc"]) < utc(observation["feature_cutoff"])
                assert utc(evidence["captured_utc"]) > utc(observation["feature_cutoff"])
    assert sot["features"]["availability_mode"] == "strict"
    assert sot["p2_ready"] is False and sot["bet_allowed"] is False
    if late_target_capture:
        assert sot["status"] == "missing" and sot["model_probabilities"] is None
        assert sot["fallback_reason"] == "strict_features_missing"
    else:
        assert sot["status"] == "predicted"
        assert sum(sot["model_probabilities"].values()) == pytest.approx(1)
        for cell in sot["features"]["cells"].values():
            for evidence in cell["evidence"]:
                assert utc(evidence["effective_available_utc"]) == capture
