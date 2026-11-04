import json
from pathlib import Path

import numpy as np
import pytest

from test_stats_intensity import observations
from zucai.predict.base import utc

PROTOCOL = Path(__file__).parents[1] / "docs/experiments/T33-evaluation-protocol.json"


def test_folds_and_selection_are_time_separated():
    from zucai.predict.experiments.evaluate import fold_cutoff, load_protocol

    protocol = load_protocol(PROTOCOL)
    assert utc(protocol["selection_end"]) == utc(protocol["evaluation_start"])
    assert fold_cutoff("2025-07-01T18:00:00Z", "weekly") == utc(
        "2025-07-01T00:00Z"
    ) - np.timedelta64(1, "us")
    assert fold_cutoff("2025-07-09T18:00:00Z", "weekly") == utc(
        "2025-07-07T00:00Z"
    ) - np.timedelta64(1, "us")
    assert fold_cutoff("2025-07-09T18:00:00Z", "monthly") == utc(
        "2025-07-01T00:00Z"
    ) - np.timedelta64(1, "us")
    invalid = json.loads(PROTOCOL.read_bytes())
    invalid["selection_end"] = "2026-01-01T00:00Z"
    with pytest.raises(ValueError, match="切分"):
        load_protocol(invalid)


def metric_records():
    return [
        dict(
            match_id=row["match_id"],
            kickoff_utc=row["kickoff_utc"].isoformat(),
            actual=0,
            league="英超",
            season="2526",
            cold_start=False,
            statistics_missing=False,
            variants={
                "baseline_raw": {"probabilities": [0.5, 0.3, 0.2], "score_brier": 0.7},
                "candidate_raw": {"probabilities": [0.6, 0.2, 0.2], "score_brier": 0.6},
            },
            fallback_reason=None,
            experimental=True,
        )
        for row in observations()[:30]
    ]


def test_metrics_and_block_pairing_include_all_targets():
    from zucai.predict.experiments.metrics import paired, summarize

    rows = metric_records()
    summary = summarize(rows, "candidate_raw")
    assert summary["n"] == 30 and summary["coverage"] == 1
    assert summary["log_loss"] == pytest.approx(-np.log(0.6))
    assert summary["brier"] == pytest.approx(0.24)
    assert summary["top_accuracy"] == 1
    diff = paired(rows, "candidate_raw", "baseline_raw", replicates=2000, seed=3301)
    assert diff["n"] == 30
    assert diff["log_loss"]["difference"] == pytest.approx(np.log(0.5 / 0.6))
    assert diff["log_loss"]["interval"] == pytest.approx([np.log(0.5 / 0.6)] * 2)
    assert diff == paired(rows, "candidate_raw", "baseline_raw", replicates=2000, seed=3301)
    rows[0]["variants"]["candidate_raw"] = None
    assert summarize(rows, "candidate_raw")["coverage"] == 29 / 30
    assert paired(rows, "candidate_raw", "baseline_raw")["n"] == 29


def test_missing_experiment_is_not_reported_as_equality():
    from zucai.predict.experiments.metrics import paired, summarize

    rows = metric_records()
    for row in rows:
        row["experimental"] = False
        row["fallback_reason"] = "statistics_missing"
        row["variants"]["candidate_raw"] = row["variants"]["baseline_raw"]
    assert paired(rows, "candidate_raw", "baseline_raw", common_experiment=True)["n"] == 0
    assert summarize(rows, "candidate_raw")["experimental_n"] == 0


def test_zero_probability_is_infinite_loss_and_ties_are_explicit():
    from zucai.predict.experiments.metrics import summarize

    rows = metric_records()[:1]
    rows[0]["variants"]["candidate_raw"]["probabilities"] = [0, 0.5, 0.5]
    assert np.isinf(summarize(rows, "candidate_raw")["log_loss"])
    rows[0]["variants"]["candidate_raw"]["probabilities"] = [1 / 3] * 3
    assert summarize(rows, "candidate_raw")["top_accuracy"] == 1 / 3


def test_common_experiment_is_intersection_of_all_six_variants():
    from zucai.predict.experiments.command import group_summary
    from zucai.predict.experiments.evaluate import load_protocol
    from zucai.predict.experiments.metrics import VARIANTS, paired

    rows = metric_records()[:2]
    for row in rows:
        for name in VARIANTS[:6]:
            row["variants"][name] = dict(probabilities=[0.6, 0.2, 0.2])
    rows[0]["variants"]["baseline_raw"] = None
    summary = group_summary(rows, load_protocol(PROTOCOL))
    assert summary["models"]["candidate_raw"]["n"] == 2
    assert {
        value["n"] for name, value in summary["common_experiment"].items() if name in VARIANTS[:6]
    } == {1}
    assert paired(rows, "candidate_raw", "ablation_raw", common_experiment=True)["n"] == 1
