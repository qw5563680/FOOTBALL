"""Compare exact legacy model inputs, fitted numbers and probabilities; never score."""

import argparse
import json
from collections import Counter
from pathlib import Path

from compatibility_worker import digest_bytes, encoded, write


def compare(baseline, current, output):
    if output.exists():
        raise ValueError("Comparison output must be new")
    old = json.loads((baseline / "summary.json").read_bytes())
    new = json.loads((current / "summary.json").read_bytes())
    output.mkdir(parents=True)
    issues, fit_rows, target_rows = [], [], []
    for key in ("database_sha256", "targets_sha256", "fixed_input"):
        if old[key] != new[key]:
            issues.append(
                {"scope": "input", "field": key, "old": old[key], "new": new[key]}
            )
    mandatory = (
        "sources",
        "complete_parameter_entry_sha256",
        "read_history_rows",
        "read_history_core_sha256",
        "selected_rows",
        "selected_source_counts",
        "selected_core_sha256",
        "training_id_sha256",
        "resolved_parameters",
        "numeric_sha256",
        "model_training_rows",
        "status",
    )
    for key in sorted(old["fits"].keys() | new["fits"].keys()):
        left, right = old["fits"].get(key), new["fits"].get(key)
        changed = [
            field
            for field in mandatory
            if (left or {}).get(field) != (right or {}).get(field)
        ]
        if (
            left is None
            or right is None
            or left.get("status") != "fitted"
            or right.get("status") != "fitted"
        ):
            changed.append("missing_or_failed_fit")
        if changed:
            issues.append(
                {
                    "scope": "fit",
                    "key": key,
                    "league": (left or right)["league"],
                    "fields": changed,
                }
            )
        fit_rows.append(
            {
                "key": key,
                "league": (left or right)["league"],
                "cutoff_utc": (left or right)["cutoff_utc"],
                "exact_core_inputs_and_fit_equal": not changed,
                "changed_fields": changed,
                "full_column_schema_equal": (left or {}).get(
                    "read_history_all_columns_sha256"
                )
                == (right or {}).get("read_history_all_columns_sha256"),
                "attrs_equal": (left or {}).get("read_history_attrs_sha256")
                == (right or {}).get("read_history_attrs_sha256"),
            }
        )
    old_targets = {row["match_id"]: row for row in old["targets"]}
    new_targets = {row["match_id"]: row for row in new["targets"]}
    for key in sorted(old_targets.keys() | new_targets.keys()):
        left, right = old_targets.get(key), new_targets.get(key)
        fields = (
            "group",
            "model_league",
            "league",
            "home",
            "away",
            "source",
            "kickoff_utc",
            "cutoff_utc",
            "status",
            "reason",
            "probabilities",
            "score_matrix",
            "model_version",
        )
        changed = [
            field
            for field in fields
            if (left or {}).get(field) != (right or {}).get(field)
        ]
        if left is None or right is None:
            changed.append("missing_target")
        if changed:
            issues.append({"scope": "target", "match_id": key, "fields": changed})
        target_rows.append(
            {
                "match_id": key,
                "group": (left or right)["group"],
                "status": (right or left)["status"],
                "exact_probability_and_grid_equal": not changed,
                "changed_fields": changed,
                "prediction_metadata_equal": (left or {}).get("prediction_metadata")
                == (right or {}).get("prediction_metadata"),
                "score_matrix_sha256": digest_bytes(
                    encoded((left or {}).get("score_matrix"))
                ),
            }
        )
    result = {
        "passed": not issues,
        "no_scoring_or_tuning_or_p2": True,
        "baseline_code_commit": old["code_commit"],
        "current_code_commit": new["code_commit"],
        "database_sha256": old["database_sha256"],
        "target_manifest_sha256": old["targets_sha256"],
        "baseline_database_unchanged": old["database_unchanged"],
        "current_database_unchanged": new["database_unchanged"],
        "fit_count": len(fit_rows),
        "exact_equal_fits": sum(
            row["exact_core_inputs_and_fit_equal"] for row in fit_rows
        ),
        "target_group_counts": {
            group: dict(
                Counter(row["status"] for row in target_rows if row["group"] == group)
            )
            for group in ("original_seven", "european_pool")
        },
        "exact_equal_targets": sum(
            row["exact_probability_and_grid_equal"] for row in target_rows
        ),
        "full_frame_schema_differences": sum(
            not row["full_column_schema_equal"] for row in fit_rows
        ),
        "history_attribute_differences": sum(
            not row["attrs_equal"] for row in fit_rows
        ),
        "prediction_metadata_differences": sum(
            not row["prediction_metadata_equal"] for row in target_rows
        ),
        "issues": issues,
        "baseline_summary_sha256": digest_bytes(
            (baseline / "summary.json").read_bytes()
        ),
        "current_summary_sha256": digest_bytes((current / "summary.json").read_bytes()),
    }
    write(output / "fits.json", fit_rows)
    write(output / "targets.json", target_rows)
    write(output / "summary.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "current", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    compare(args.baseline, args.current, args.output)
