"""Frozen synthetic batches exercise time ordering, coverage and prospective gates."""

import csv
import json
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t19"
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def samples():
    batches = json.loads((FIXTURES / "forecasts.json").read_bytes())
    rows = []
    for batch in batches:
        for row in batch["predictions"]:
            rows.append(
                {**row, **{k: batch[k] for k in ("batch_id", "generated_utc", "as_of_utc", "mode")}}
            )
    results = {}
    with (FIXTURES / "results.csv").open() as stream:
        for result in csv.DictReader(stream):
            identity = next(r for r in rows if r["match_id"] == result["match_id"])
            results[result["match_id"]] = {
                **{
                    k: identity[k]
                    for k in ("match_id", "home", "away", "source", "league", "kickoff_utc")
                },
                **result,
                "home_goals": int(result["home_goals"]),
                "away_goals": int(result["away_goals"]),
                "status": "finished",
            }
    return rows, results


def test_last_prematch_only_strict_result_time_and_frozen_market():
    from zucai.scoreboard.score import score_forecasts

    forecasts, results = samples()
    scored, audit = score_forecasts(forecasts, results, [], NOW)
    assert {r["match_id"] for r in scored} == {"jc:one", "jc:two", "jc:three", "jc:nosp"}
    assert {r["batch_id"] for r in scored} == {"late"}
    one = next(r for r in scored if r["match_id"] == "jc:one")
    assert one["model"]["log_loss"] == pytest.approx(-math.log(0.48))
    assert one["model"]["brier"] == pytest.approx(0.4064)
    assert one["uniform"]["brier"] == pytest.approx(2 / 3)
    # Frozen odds 2.5,3,3.5 -> inverse sum 107/105; normalized home=42/107.
    assert one["market"]["log_loss"] == pytest.approx(-math.log(42 / 107))
    assert next(r for r in scored if r["match_id"] == "jc:nosp")["market"] is None
    assert audit["excluded"]["result_not_after_forecast"] == 1
    assert audit["excluded"]["result_not_yet_available"] == 1
    assert audit["excluded"]["result_not_observed"] == 1
    assert score_forecasts(forecasts, results, scored, NOW)[0] == scored


@pytest.mark.parametrize("change", ["replay", "identity", "snapshot", "status", "incomplete"])
def test_non_prospective_or_unmatched_inputs_do_not_gain_evidence(change):
    from zucai.scoreboard.score import score_forecasts

    forecasts, results = samples()
    forecasts = [r for r in forecasts if r["match_id"] == "jc:one"]
    if change == "replay":
        for row in forecasts:
            row["mode"] = "historical_replay"
    elif change == "identity":
        results["jc:one"]["home"] = "另一支球队"
    elif change == "snapshot":
        for row in forecasts:
            row["market"]["snapshot_utc"] = "2026-11-02T00:00:00Z"
    elif change == "status":
        results["jc:one"]["status"] = "cancelled"
    else:
        results["jc:one"]["home_goals"] = None
    scored, _ = score_forecasts(forecasts, results, [], NOW)
    if change == "snapshot":
        assert len(scored) == 1 and scored[0]["market"] is None
    else:
        assert scored == []


def test_appended_newer_batch_counts_match_once_and_flags_score_corrections():
    from zucai.scoreboard.score import score_forecasts, summarize_scores

    forecasts, results = samples()
    first, _ = score_forecasts([r for r in forecasts if r["batch_id"] == "early"], results, [], NOW)
    scored, audit = score_forecasts(forecasts, results, first, NOW)
    assert scored[: len(first)] == first
    summary = summarize_scores(scored, NOW, active_keys=audit["active_keys"])
    assert summary["total"]["n"] == 4
    assert summary["leagues"]["测试联赛"]["n"] == 4
    changed = deepcopy(results)
    changed["jc:one"]["home_goals"] = 0
    changed["jc:one"]["away_goals"] = 1
    unchanged, audit = score_forecasts(forecasts, changed, scored, NOW)
    assert unchanged == scored
    assert "jc:one" in audit["result_corrections"]


def test_score_gates_calibration_month_version_and_drift_boundaries():
    from zucai.scoreboard.score import score_forecasts, summarize_scores

    forecasts, results = samples()
    scored, _ = score_forecasts(forecasts, results, [], NOW)
    template = next(r for r in scored if r["match_id"] == "jc:one")
    rows = []
    for index in range(200):
        row = deepcopy(template)
        row["match_id"] = f"jc:gate-{index}"
        stamp = NOW - timedelta(days=1 + index % 28)
        row["kickoff_utc"] = stamp.isoformat()
        rows.append(row)
    assert not summarize_scores(rows[:199], NOW)["leagues"]["测试联赛"]["prospective_verified"]
    summary = summarize_scores(rows, NOW)
    league = summary["leagues"]["测试联赛"]
    assert league["prospective_verified"]
    assert league["drift"]["four_weeks_draw_bias"]
    assert not league["drift"]["last_100_log_loss"]
    assert len(league["model"]["calibration"]) == 10
    assert sum(b["n"] for b in league["model"]["calibration"]) == 600
    assert set(summary["months"]) == {"2026-10", "2026-11"}
    assert len(summary["league_versions"]) == 1
    for row in rows:
        row["model"]["log_loss"] = 2
    poor = summarize_scores(rows, NOW)["leagues"]["测试联赛"]
    assert not poor["prospective_verified"] and poor["drift"]["last_100_log_loss"]


def test_exact_five_percentage_points_is_not_drift():
    from zucai.scoreboard.score import score_forecasts, summarize_scores

    forecasts, results = samples()
    scored, _ = score_forecasts(forecasts, results, [], NOW)
    template = next(r for r in scored if r["match_id"] == "jc:one")
    rows = []
    for index in range(200):
        row = deepcopy(template)
        row["match_id"] = f"jc:boundary-{index}"
        row["kickoff_utc"] = (NOW - timedelta(days=1 + index % 28)).isoformat()
        row["model"]["probabilities"] = {"h": 0.85, "d": 0.05, "a": 0.1}
        rows.append(row)
    assert not summarize_scores(rows, NOW)["leagues"]["测试联赛"]["drift"]["four_weeks_draw_bias"]
