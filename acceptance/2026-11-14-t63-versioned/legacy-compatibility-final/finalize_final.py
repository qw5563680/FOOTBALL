"""Summarize the final frozen run while reusing immutable baseline artifacts."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path


def encoded(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.write_bytes(encoded(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    output = root / "final"
    assert not output.exists()
    preparation = load(root / "preparation-manifest.json")
    reuse = load(root / "baseline-reuse-sha256.json")
    previous = Path(preparation["prior_evidence_directory"])
    baseline_path = Path(reuse["baseline_directory"])
    baseline = load(baseline_path / "summary.json")
    current = load(root / "current/summary.json")
    comparison = load(root / "comparison/summary.json")
    snapshot = load(root / "final-current-source-manifest.json")
    fixture = load(root / "future-import-fixture-evidence.json")
    old_schema = load(previous / "baseline-schema.json")
    new_schema = load(root / "current-schema.json")
    assert comparison["passed"] and not comparison["issues"]
    assert comparison["fit_count"] == comparison["exact_equal_fits"] == 39
    assert comparison["exact_equal_targets"] == 119
    assert baseline["database_unchanged"] and current["database_unchanged"]
    assert current["network_requests"] == 0
    assert all(snapshot["old7_and_pool_complete_entries_equal_b83"].values())
    assert all(
        digest(Path(path)) == expected
        for path, expected in reuse["files_sha256"].items()
    )
    prior_manifest = load(previous / "final/evidence-sha256.json")
    assert all(
        digest(previous / path) == expected for path, expected in prior_manifest.items()
    )
    assert fixture["passed"] and fixture["phases"]["green"]["tests"] == 8
    for phase in fixture["phases"].values():
        for kind in ("log", "xml"):
            assert digest(Path(phase[kind]["path"])) == phase[kind]["sha256"]
    assert digest(Path(fixture["fixture"]["path"])) == fixture["fixture"]["sha256"]

    fits = []
    fit_csv = []
    for key, row in current["fits"].items():
        old = baseline["fits"][key]
        assert row["status"] == old["status"] == "fitted"
        for kind, field in (
            ("numeric", "numeric_sha256"),
            ("training-ids", "training_id_sha256"),
        ):
            left = baseline_path / f"{key}-{kind}.json"
            right = root / "current" / f"{key}-{kind}.json"
            assert left.read_bytes() == right.read_bytes()
            assert digest(left) == old[field] == row[field]
        fits.append(
            {
                "fit_key": key,
                **row,
                "exact_equal": True,
                "baseline_numeric_path": str(baseline_path / f"{key}-numeric.json"),
                "current_numeric_path": str(root / "current" / f"{key}-numeric.json"),
                "baseline_training_ids_path": str(
                    baseline_path / f"{key}-training-ids.json"
                ),
                "current_training_ids_path": str(
                    root / "current" / f"{key}-training-ids.json"
                ),
            }
        )
        fit_csv.append(
            {
                "fit_key": key,
                **{
                    name: row[name]
                    for name in (
                        "league",
                        "cutoff_utc",
                        "read_history_rows",
                        "selected_rows",
                        "model_training_rows",
                        "training_id_sha256",
                        "numeric_sha256",
                        "complete_parameter_entry_sha256",
                    )
                },
                "selected_source_counts": json.dumps(
                    row["selected_source_counts"], sort_keys=True
                ),
                "exact_equal": True,
            }
        )

    old_targets = {row["match_id"]: row for row in baseline["targets"]}
    assert len(old_targets) == len(current["targets"]) == 119
    assert len({row["match_id"] for row in current["targets"]}) == 119
    targets, target_csv = [], []
    for row in current["targets"]:
        old = old_targets[row["match_id"]]
        for key in (
            "cutoff_utc",
            "status",
            "reason",
            "probabilities",
            "score_matrix",
            "model_version",
            "prediction_metadata",
        ):
            assert row.get(key) == old.get(key)
        cutoff = row["cutoff_utc"]
        bjt = (
            datetime.fromisoformat(cutoff)
            .astimezone(timezone(timedelta(hours=8)))
            .isoformat()
            if cutoff
            else None
        )
        grid_sha = (
            hashlib.sha256(encoded(row["score_matrix"])).hexdigest()
            if row["status"] == "predicted"
            else None
        )
        if row["status"] == "predicted":
            assert len(row["score_matrix"]) == 10 and all(
                len(line) == 10 for line in row["score_matrix"]
            )
        else:
            assert row["reason"] == "no_verified_pre_kickoff_cutoff"
        targets.append(
            {
                **row,
                "cutoff_bjt": bjt,
                "validation_status": "实验未验证；仅兼容性比较，不评分",
                "baseline_probabilities": old.get("probabilities"),
                "score_matrix_sha256": grid_sha,
                "exact_equal": True,
            }
        )
        compact = {
            name: row.get(name)
            for name in (
                "match_id",
                "group",
                "league",
                "home",
                "away",
                "source",
                "kickoff_utc",
                "cutoff_utc",
                "status",
                "reason",
                "model_name",
                "model_version",
                "fit_key",
            )
        }
        compact.update(
            cutoff_bjt=bjt,
            validation_status="实验未验证；仅兼容性比较，不评分",
            exact_equal=True,
            score_matrix_sha256=grid_sha,
        )
        for index, label in enumerate(("home", "draw", "away")):
            compact["baseline_p_" + label] = old.get("probabilities", [None] * 3)[index]
            compact["current_p_" + label] = row.get("probabilities", [None] * 3)[index]
        target_csv.append(compact)

    schema = []
    for old, new in zip(old_schema["leagues"], new_schema["leagues"], strict=True):
        assert old["league"] == new["league"]
        assert old["read_history_core_sha256"] == new["read_history_core_sha256"]
        schema.append(
            {
                "league": old["league"],
                "old_columns": old["columns"],
                "added_columns": sorted(set(new["columns"]) - set(old["columns"])),
                "removed_columns": sorted(set(old["columns"]) - set(new["columns"])),
                "original_columns_values_and_order_equal": True,
                "attrs_equal": old["attrs"] == new["attrs"],
            }
        )
    final_source = Path(snapshot["source_root"])
    source_changes = [
        name
        for name, expected in snapshot["files_sha256"].items()
        if not (final_source / name).is_file()
        or digest(final_source / name) != expected
    ]
    result = {
        **comparison,
        "created_utc": datetime.now(UTC).isoformat(),
        "purpose": "Final fixed-input legacy numerical compatibility acceptance; no scores, LL, tuning or P2",
        "archive_commit": current["fixed_input"],
        "baseline_reused_not_refitted": True,
        "baseline_reuse_manifest_sha256": digest(root / "baseline-reuse-sha256.json"),
        "baseline_reused_artifact_count": len(reuse["files_sha256"]),
        "previous_evidence_files_verified_unchanged": len(prior_manifest),
        "current_source_snapshot_sha256": snapshot["snapshot_sha256"],
        "current_source_manifest_sha256": digest(
            root / "final-current-source-manifest.json"
        ),
        "parent_head_commit": snapshot["source_head_commit"],
        "current_snapshot_is_working_tree_not_committed_revision": True,
        "source_files_stable_during_snapshot": snapshot[
            "source_files_stable_during_snapshot"
        ],
        "source_tree_changes_since_snapshot": source_changes,
        "source_selection_override_used": False,
        "old7_and_pool_complete_parameter_entries_equal_b83": snapshot[
            "old7_and_pool_complete_entries_equal_b83"
        ],
        "exact_equal_predictions": sum(row["status"] == "predicted" for row in targets),
        "not_generated_no_cutoff": sum(
            row["status"] == "not_generated" for row in targets
        ),
        "fit_numeric_and_training_id_files_verified_byte_identical": len(fits),
        "league_status_counts": {
            league: dict(
                Counter(row["status"] for row in targets if row["league"] == league)
            )
            for league in sorted({row["league"] for row in targets})
        },
        "worker": preparation["pointers"]["worker"],
        "comparator": preparation["pointers"]["comparator"],
        "finalizer_sha256": digest(Path(__file__)),
        "new_network_requests": 0,
        "separate_future_import_fixture": {
            "path": str(root / "future-import-fixture-evidence.json"),
            "sha256": digest(root / "future-import-fixture-evidence.json"),
            "red_failed": 7,
            "red_passed": 1,
            "green_passed": 8,
            "fixed_archive_comparison_is_not_a_substitute": True,
        },
        "limitation": "This fixed archive verifies unchanged legacy inputs and all numeric outputs. Future-import link behavior is supported by separate eight-case fixture evidence. Current code is the recorded working-tree snapshot, not merely the parent HEAD commit.",
    }
    assert (
        result["exact_equal_predictions"] == 105
        and result["not_generated_no_cutoff"] == 14
    )
    output.mkdir()
    for name, value in (
        ("acceptance.json", result),
        ("targets.json", targets),
        ("fits.json", fits),
        ("schema-difference.json", schema),
    ):
        write(output / name, value)
    for name, rows in (("targets.csv", target_csv), ("fits.csv", fit_csv)):
        with (output / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    write(
        output / "evidence-sha256.json",
        {
            str(path.relative_to(root)): digest(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and path != output / "evidence-sha256.json"
        },
    )
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "fits": len(fits),
                "predictions": result["exact_equal_predictions"],
                "no_cutoff": result["not_generated_no_cutoff"],
                "source_changes": source_changes,
            }
        )
    )


if __name__ == "__main__":
    main()
