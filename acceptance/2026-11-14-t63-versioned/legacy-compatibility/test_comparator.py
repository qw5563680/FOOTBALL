"""Invented comparator contracts; no forecasts are fitted or scored here."""

import json
from copy import deepcopy

import pytest

from compare_compatibility import compare


def summary():
    return {
        "database_sha256": "same-database",
        "targets_sha256": "same-targets",
        "fixed_input": "fixed-input",
        "code_commit": "test-code",
        "database_unchanged": True,
        "fits": {
            "key": {
                "league": "英超",
                "cutoff_utc": "2026-11-01T00:00:00Z",
                "status": "fitted",
                "numeric_sha256": "numbers",
                "training_id_sha256": "ids",
            }
        },
        "targets": [
            {
                "match_id": "jc:synthetic",
                "group": "original_seven",
                "league": "英超",
                "model_league": "英超",
                "cutoff_utc": "2026-11-01T00:00:00Z",
                "status": "predicted",
                "probabilities": [0.4, 0.3, 0.3],
                "score_matrix": [[0.3, 0.3], [0.4, 0.0]],
            }
        ],
    }


def write_pair(tmp_path, current):
    for name, document in (("old", summary()), ("new", current)):
        path = tmp_path / name
        path.mkdir()
        (path / "summary.json").write_text(json.dumps(document))
    return compare(tmp_path / "old", tmp_path / "new", tmp_path / "comparison")


def test_exact_equal_compatibility_control_passes(tmp_path):
    result = write_pair(tmp_path, summary())
    assert result["passed"]
    assert result["exact_equal_fits"] == 1
    assert result["exact_equal_targets"] == 1


@pytest.mark.parametrize(
    "change",
    ["one_bit_probability", "training_ids", "parameters", "missing_target", "cutoff"],
)
def test_no_tolerance_or_silent_denominator_change(tmp_path, change):
    current = deepcopy(summary())
    if change == "one_bit_probability":
        current["targets"][0]["probabilities"][0] = 0.4000000000000001
    elif change == "training_ids":
        current["fits"]["key"]["training_id_sha256"] = "different-ids"
    elif change == "parameters":
        current["fits"]["key"]["complete_parameter_entry_sha256"] = "different-policy"
    elif change == "missing_target":
        current["targets"] = []
    else:
        current["targets"][0]["cutoff_utc"] = "2026-11-02T00:00:00Z"
    result = write_pair(tmp_path, current)
    assert not result["passed"]
    assert result["issues"]
