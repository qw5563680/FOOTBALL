from copy import deepcopy

from test_stats_experiment_evaluation import PROTOCOL, metric_records
from zucai.predict.experiments.evaluate import load_protocol
from zucai.predict.experiments.metrics import VARIANTS


def test_prespecified_group_intervals_keep_pairing_and_empty_groups():
    from zucai.predict.experiments.groups import subgroup_comparisons

    rows = metric_records()
    for row in rows:
        for name in VARIANTS[:6]:
            row["variants"].setdefault(name, deepcopy(row["variants"]["candidate_raw"]))
    rows[0]["cold_start"] = True
    rows[0]["variants"]["baseline_raw"] = None
    rows[1]["statistics_missing"] = True
    rows[1]["experimental"] = False
    protocol = load_protocol(PROTOCOL)
    report = subgroup_comparisons(rows, protocol)
    cold = report["cold_start"]["true"]
    assert cold["targets"] == 1
    assert len(cold["paired"]) == 6
    pair = cold["paired"]["candidate_raw-ablation_raw"]
    assert pair["all"]["n"] == 1
    assert pair["all"]["log_loss"]["interval"] is None
    assert pair["common_experiment"]["n"] == 0
    missing = report["statistics_missing"]["true"]["paired"]
    assert missing["candidate_raw-baseline_raw"]["common_experiment"]["n"] == 0
    assert report == subgroup_comparisons(rows, protocol)
    empty = subgroup_comparisons([], protocol)
    assert empty["cold_start"]["false"]["targets"] == 0
    assert (
        empty["cold_start"]["false"]["paired"]["candidate_raw-baseline_raw"]["all"]["reason"]
        == "no_paired_observations"
    )
