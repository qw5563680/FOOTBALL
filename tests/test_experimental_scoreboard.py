"""Model cohorts, adoption and failure coverage on frozen offline forecast fixtures."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from test_scoreboard_score import NOW, samples
from zucai.scoreboard.command import _json
from zucai.scoreboard.experimental import adoption, select_latest, summarize_experiments
from zucai.scoreboard.paper import advance_ledger
from zucai.scoreboard.score import score_forecasts


def scores():
    rows, results = samples()
    rows = [r for r in rows if r["batch_id"] == "late" and r["match_id"] in {"jc:one", "jc:two"}]
    for row in rows:
        row["league"] = results[row["match_id"]]["league"] = "英超"
    experiments = [
        dict(
            r,
            model_name="dc-sot-v1",
            model_version="offline",
            p2_ready=False,
            validation_status="experimental_unadopted",
            fallback_reason=None,
            market=None,
        )
        for r in deepcopy(rows)
    ]
    original, audit = score_forecasts(rows, results, [], NOW)
    return rows, results, experiments, original, audit


@pytest.mark.parametrize("missing", ["normal", "late", "absent"])
def test_missing_and_late_remain_in_coverage_denominator(missing):
    rows, results, experiments, original, audit = scores()
    experiments[1].update(
        status="missing", model_probabilities=None, fallback_reason="strict_features_missing"
    )
    if missing == "late":
        experiments[1]["generated_utc"] = experiments[1]["kickoff_utc"]
    elif missing == "absent":
        experiments.pop()
    scored, _ = score_forecasts(
        list(select_latest(experiments, NOW).values()), results, [], NOW, experimental=True
    )
    summary = summarize_experiments(
        experiments, scored, original, audit["active_keys"], NOW, activated=rows[0]["as_of_utc"]
    )
    group = next(
        g for g in summary["groups"] if g["model_name"] == "dc-sot-v1" and g["league"] == "英超"
    )
    assert (group["production_n"], group["common_n"], group["relative_coverage"]) == (2, 1, 0.5)
    assert group["adoption"]["automatic_adoption"] is False
    assert _json(summary) == _json(
        summarize_experiments(
            experiments, scored, original, audit["active_keys"], NOW, activated=rows[0]["as_of_utc"]
        )
    )


def test_model_identity_pairing_and_experiment_does_not_enter_paper_ledger():
    rows, results, experiments, original, audit = scores()
    second = [dict(r, model_name="dc-xg-v1") for r in experiments]
    scored, info = score_forecasts(experiments + second, results, [], NOW, experimental=True)
    assert len(scored) == 4 and len({r["record_id"] for r in scored}) == 4
    assert (
        score_forecasts(experiments + second, results, scored, NOW, experimental=True)[0] == scored
    )
    ledger = advance_ledger(experiments, results, [], NOW)
    assert not any(r["record_type"] == "ticket" for r in ledger)
    assert any(r.get("reason") == "model_not_p2_ready" for r in ledger)
    # Common pairing may not silently combine the latest baseline with an earlier experiment.
    for r in experiments:
        r["batch_id"] = "older"
    group = summarize_experiments(experiments, scored, original, audit["active_keys"], NOW)[
        "groups"
    ][0]
    assert group["common_n"] == 0


def test_adoption_is_fixed_and_never_automatic():
    fixture = json.loads(
        (Path(__file__).parents[1] / "data/fixtures/t34a/adoption.json").read_bytes()
    )
    for name, case in fixture.items():
        value = adoption(case, case["experiment_ece"], case["production_ece"], case["coverage"])
        assert (value["unmet"] == []) == (name == "pass_case")
        assert value["automatic_adoption"] is False
    assert adoption(fixture["pass_case"], 0.02, 0.03, 0.9)["status"] == "达到采用门槛，待所有者决策"


def test_infinite_experiment_loss_does_not_stop_production_json():
    _, results, experiments, original, audit = scores()
    experiments[0]["model_probabilities"] = dict(h=0, d=0.5, a=0.5)
    scored, _ = score_forecasts(experiments, results, [], NOW, experimental=True)
    summary = summarize_experiments(experiments, scored, original, audit["active_keys"], NOW)
    english = next(
        g for g in summary["groups"] if g["model_name"] == "dc-sot-v1" and g["league"] == "英超"
    )
    assert english["common_experiment"]["log_loss"] == "Infinity"
    assert "log_loss" in english["adoption"]["unmet"]
    assert b"Infinity" in _json(summary)


def test_later_experiment_does_not_remove_same_batch_production_pair():
    from datetime import timedelta

    from zucai.scoreboard.score import instant

    _, results, experiments, original, audit = scores()
    later = deepcopy(experiments)
    for row in later:
        row["batch_id"] = "experiment-later"
        row["generated_utc"] = (instant(row["generated_utc"]) + timedelta(minutes=1)).isoformat()
    all_rows = experiments + later
    scored, _ = score_forecasts(all_rows, results, [], NOW, experimental=True)
    assert {r["batch_id"] for r in scored} == {"experiment-later"}
    summary = summarize_experiments(all_rows, scored, original, audit["active_keys"], NOW)
    group = next(
        g for g in summary["groups"] if g["model_name"] == "dc-sot-v1" and g["league"] == "英超"
    )
    assert group["own_scored_n"] == 2
    assert (group["production_n"], group["common_n"], group["relative_coverage"]) == (2, 2, 1)
