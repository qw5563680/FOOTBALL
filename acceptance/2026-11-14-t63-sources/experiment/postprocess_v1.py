"""Correct aggregation of frozen v1 outputs; never fit models or change predictions."""

import csv
import json
from pathlib import Path

from paired_runner import summarize_previous
from prepare_census import save, sha

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "paired-v1"
OUTPUT = ROOT / "paired-v1-final"


def write_csv(name, rows):
    with (OUTPUT / name).open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    OUTPUT.mkdir(exist_ok=False)
    original_raw = (SOURCE / "summary.json").read_bytes()
    predictions_raw = (SOURCE / "predictions.json").read_bytes()
    original = json.loads(original_raw)
    predictions = json.loads(predictions_raw)
    executed = ROOT / "paired_runner-executed-v1.py"
    assert sha(executed.read_bytes()) == original["runner_sha256"]
    corrected = summarize_previous(predictions)
    independent = {}
    for arm in ("old", "new"):
        independent[arm] = {
            "at_least_one": sum(
                any(
                    side[arm + "_in_training"] is True
                    for side in row["previous_coverage"].values()
                )
                for row in predictions
            ),
            "both": sum(
                all(
                    side[arm + "_in_training"] is True
                    for side in row["previous_coverage"].values()
                )
                for row in predictions
            ),
            "unknown": sum(
                any(
                    side["status"] == "unknown"
                    for side in row["previous_coverage"].values()
                )
                for row in predictions
            ),
            "identified_team_occurrences": sum(
                side["status"] == "identified"
                for row in predictions
                for side in row["previous_coverage"].values()
            ),
        }
        assert (
            corrected["arms"][arm]["matches_at_least_one_previous_in_training"]
            == independent[arm]["at_least_one"]
        )
        assert (
            corrected["arms"][arm]["matches_both_previous_in_training"]
            == independent[arm]["both"]
        )
        assert (
            corrected["arms"][arm]["identified_team_occurrences"]
            == independent[arm]["identified_team_occurrences"]
        )
    old_links, checked_files, checked_rows = [], 0, 0
    for path in sorted((SOURCE / "training").glob("*-selected.json")):
        selected = json.loads(path.read_bytes())
        indexed = {row["match_id"]: row for row in selected}
        checked_files += 1
        checked_rows += len(selected)
        for row in selected:
            other = indexed.get(row.get("counterpart_id"))
            if (
                other
                and row["source"] in {"fd", "jc"}
                and other["source"] in {"fd", "jc"}
            ):
                old_links.append(
                    {
                        "training_file": path.name,
                        "left": row["match_id"],
                        "right": other["match_id"],
                    }
                )
    assert not old_links, (
        "An explicit legacy link needs representative selection review"
    )
    model_index = {}
    prediction_csv, previous_csv = [], []
    for row in predictions:
        record = {
            key: row.get(key)
            for key in (
                "match_id",
                "league",
                "home",
                "away",
                "kickoff_utc",
                "cutoff_utc",
                "cutoff_beijing",
                "validation_status",
                "label_status",
                "paired_scoring_eligible",
                "iso_week",
            )
        }
        record.update(
            home_goals=(row.get("result") or {}).get("home_goals"),
            away_goals=(row.get("result") or {}).get("away_goals"),
        )
        for arm in ("old", "new"):
            result = row[arm]
            for key in (
                "status",
                "reason",
                "model_name",
                "model_version",
                "training_rows",
                "training_sha256",
            ):
                record[arm + "_" + key] = result.get(key)
            for index, name in enumerate(("home", "draw", "away")):
                record[arm + "_p_" + name] = result.get("probabilities", [None] * 3)[
                    index
                ]
            if result.get("model_path"):
                path = SOURCE / result["model_path"]
                assert sha(path.read_bytes()) == result["model_version"]
                assert (
                    sha((SOURCE / result["training_path"]).read_bytes())
                    == result["training_sha256"]
                )
                model_index[result["model_path"]] = {
                    "arm": arm,
                    "league": row["league"],
                    "cutoff_utc": row["cutoff_utc"],
                    "model_path": result["model_path"],
                    "model_sha256": result["model_version"],
                    "training_path": result["training_path"],
                    "training_sha256": result["training_sha256"],
                    "training_rows": result["training_rows"],
                }
        prediction_csv.append(record)
        for side, previous in row["previous_coverage"].items():
            previous_csv.append(
                {
                    "target_match_id": row["match_id"],
                    "league": row["league"],
                    "cutoff_utc": row["cutoff_utc"],
                    "side": side,
                    **{
                        key: previous.get(key)
                        for key in (
                            "status",
                            "reason",
                            "match_id",
                            "source",
                            "old_in_training",
                            "new_in_training",
                            "stored_available_source",
                            "actual_conservative_archive_bound_before_cutoff",
                            "archive_bound_hours_after_kickoff",
                        )
                    },
                }
            )
    write_csv("predictions.csv", prediction_csv)
    write_csv("previous-team-occurrences.csv", previous_csv)
    write_csv("model-training-index.csv", list(model_index.values()))
    corrected_summary = dict(original, previous_game_coverage=corrected)
    corrected_summary["postprocessing"] = {
        "method": "aggregation_only_from_unchanged_frozen_v1_predictions",
        "source_summary_sha256": sha(original_raw),
        "source_predictions_sha256": sha(predictions_raw),
        "executed_runner_sha256": sha(executed.read_bytes()),
        "corrected_runner_sha256": sha((ROOT / "paired_runner.py").read_bytes()),
        "postprocessor_sha256": sha(Path(__file__).read_bytes()),
        "new_fit_calls": 0,
        "probabilities_changed": False,
        "correction": "matches_at_least_one_previous_in_training counts the 14 known contained sides whose opposite side is unknown: each arm 48 -> 62. Unknown can overlap at-least-one; neither requires both sides known.",
        "generic_runner_hardening": "A failed fit now records unknown actual-training membership; this has no v1 numerical effect because all 112 fits succeeded.",
        "final_summary_path": "paired-v1-final/summary.json",
    }
    save(OUTPUT / "summary.json", corrected_summary)
    checks = {
        "independent_previous_counts": independent,
        "training_files_checked": checked_files,
        "training_rows_checked_with_repetition": checked_rows,
        "legacy_counterpart_dual_rows_in_same_training_set": old_links,
        "fit_failures": sum(
            row[arm]["status"] != "predicted"
            and row[arm].get("reason") != "no_verified_pre_kickoff_cutoff"
            for row in predictions
            for arm in ("old", "new")
        ),
        "verified_model_files": len(model_index),
        "old_and_new_training_digests_equal_for_all_cutoff_targets": all(
            row["old"].get("training_sha256") == row["new"].get("training_sha256")
            for row in predictions
            if row["cutoff_utc"]
        ),
        "original_summary_unchanged": sha((SOURCE / "summary.json").read_bytes())
        == sha(original_raw),
        "original_predictions_unchanged": sha(
            (SOURCE / "predictions.json").read_bytes()
        )
        == sha(predictions_raw),
        "metric_tables_unchanged": all(
            corrected_summary[key] == original[key]
            for key in ("overall", "by_league", "by_iso_week", "by_previous_coverage")
        ),
    }
    save(OUTPUT / "postchecks.json", checks)
    save(
        OUTPUT / "SHA256.json",
        {
            path.name: sha(path.read_bytes())
            for path in sorted(OUTPUT.iterdir())
            if path.is_file()
        },
    )
    return checks


if __name__ == "__main__":
    main()
