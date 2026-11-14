"""Publish inspectable summaries of frozen compatibility evidence without refitting."""

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from compatibility_worker import digest_bytes, encoded, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root, args.output
    output.mkdir(parents=True, exist_ok=False)
    baseline = json.loads((root / "baseline/summary.json").read_bytes())
    current = json.loads((root / "current/summary.json").read_bytes())
    comparison = json.loads((root / "comparison/summary.json").read_bytes())
    snapshot = json.loads((root / "current-source-manifest.json").read_bytes())
    old_schema = json.loads((root / "baseline-schema.json").read_bytes())
    new_schema = json.loads((root / "current-schema.json").read_bytes())
    assert comparison["passed"] and not comparison["issues"]
    assert baseline["database_unchanged"] and current["database_unchanged"]
    assert baseline["network_requests"] == current["network_requests"] == 0
    assert all(snapshot["old7_complete_entries_equal_b83"].values())
    fit_rows = []
    for key, old in baseline["fits"].items():
        new = current["fits"][key]
        assert old["status"] == new["status"] == "fitted"
        for kind, field in (("numeric", "numeric_sha256"), ("training-ids", "training_id_sha256")):
            left = (root / "baseline" / f"{key}-{kind}.json").read_bytes()
            right = (root / "current" / f"{key}-{kind}.json").read_bytes()
            assert left == right
            assert digest_bytes(left) == old[field] == new[field]
        fit_rows.append(
            {
                "fit_key": key,
                "league": old["league"],
                "cutoff_utc": old["cutoff_utc"],
                "read_history_rows": old["read_history_rows"],
                "selected_rows": old["selected_rows"],
                "selected_source_counts": json.dumps(old["selected_source_counts"], sort_keys=True),
                "training_id_sha256": old["training_id_sha256"],
                "full_fit_numbers_sha256": old["numeric_sha256"],
                "complete_parameter_entry_sha256": old["complete_parameter_entry_sha256"],
                "exact_equal": True,
            }
        )
    old_targets = {row["match_id"]: row for row in baseline["targets"]}
    target_rows = []
    bjt = timezone(timedelta(hours=8))
    for row in current["targets"]:
        old = old_targets[row["match_id"]]
        assert old.get("probabilities") == row.get("probabilities")
        assert old.get("score_matrix") == row.get("score_matrix")
        assert old.get("prediction_metadata") == row.get("prediction_metadata")
        cutoff = row["cutoff_utc"]
        out = {
            field: row.get(field)
            for field in (
                "match_id", "group", "league", "home", "away", "source", "kickoff_utc",
                "cutoff_utc", "status", "reason", "model_name", "model_version", "fit_key",
            )
        }
        out.update(
            cutoff_bjt=(datetime.fromisoformat(cutoff.replace("Z", "+00:00")).astimezone(bjt).isoformat() if cutoff else None),
            validation_status="实验未验证；仅兼容性比较，不评分",
            exact_equal=True,
        )
        probabilities = row.get("probabilities", [None, None, None])
        for index, name in enumerate(("home", "draw", "away")):
            out[f"baseline_p_{name}"] = old.get("probabilities", [None, None, None])[index]
            out[f"current_p_{name}"] = probabilities[index]
        if row["status"] == "predicted":
            grid = row["score_matrix"]
            assert len(grid) == 10 and all(len(line) == 10 for line in grid)
            out["score_matrix_sha256"] = digest_bytes(encoded(grid))
        else:
            assert row["reason"] == "no_verified_pre_kickoff_cutoff"
            out["score_matrix_sha256"] = None
        target_rows.append(out)
    for filename, rows in (("targets.csv", target_rows), ("fits.csv", fit_rows)):
        with (output / filename).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    schema = []
    for old, new in zip(old_schema["leagues"], new_schema["leagues"], strict=True):
        assert old["league"] == new["league"]
        assert old["read_history_core_sha256"] == new["read_history_core_sha256"]
        assert old["attrs"] == new["attrs"]
        schema.append(
            {
                "league": old["league"],
                "old_columns": old["columns"],
                "added_columns": sorted(set(new["columns"]) - set(old["columns"])),
                "removed_columns": sorted(set(old["columns"]) - set(new["columns"])),
                "original_columns_values_and_order_equal": True,
                "attrs_equal": True,
            }
        )
    write(output / "schema-difference.json", schema)
    result = {
        **comparison,
        "created_utc": datetime.now(UTC).isoformat(),
        "purpose": "Fixed-input legacy compatibility acceptance only; no outcome scoring, tuning or P2 publication",
        "source_selection_override_used": False,
        "new_network_requests": 0,
        "archive_commit": current["fixed_input"],
        "current_source_snapshot_sha256": snapshot["snapshot_sha256"],
        "current_snapshot_is_working_tree_not_committed_revision": True,
        "original_seven_complete_parameter_entries_equal_b83": snapshot["old7_complete_entries_equal_b83"],
        "exact_equal_predictions": sum(row["status"] == "predicted" for row in target_rows),
        "not_generated_no_cutoff": sum(row["status"] == "not_generated" for row in target_rows),
        "league_status_counts": {
            league: dict(Counter(row["status"] for row in target_rows if row["league"] == league))
            for league in sorted({row["league"] for row in target_rows})
        },
        "fit_numeric_and_training_id_files_verified_byte_identical": len(fit_rows),
        "current_worker_sha256": digest_bytes((root / "compatibility_worker.py").read_bytes()),
        "baseline_executed_worker_sha256": digest_bytes((root / "baseline-executed-worker.py").read_bytes()),
        "comparison_script_sha256": digest_bytes((root / "compare_compatibility.py").read_bytes()),
        "schema_audit_script_sha256": digest_bytes((root / "audit_history_schema.py").read_bytes()),
        "comparator_fixture_tests": {"passed": 6, "log_sha256": digest_bytes((root / "comparator-tests.log").read_bytes())},
        "limitation": "Acceptance covers the frozen working-tree snapshot. Audit later source/parameter changes before applying this result to a final commit. Added read-history metadata columns are listed separately; their full-frame hash differs while all original columns and numeric outputs match exactly.",
    }
    write(output / "acceptance.json", result)
    write(
        output / "evidence-sha256.json",
        {
            str(path.relative_to(root)): digest_bytes(path.read_bytes())
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path != output / "evidence-sha256.json"
            and "test-tmp" not in path.parts
        },
    )
    print(json.dumps({"passed": result["passed"], "fits": len(fit_rows), "predictions": result["exact_equal_predictions"], "no_cutoff": result["not_generated_no_cutoff"]}))


if __name__ == "__main__":
    main()
