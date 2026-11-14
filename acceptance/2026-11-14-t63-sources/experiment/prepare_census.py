"""Read-only T63 denominator and evidence inventory. Never fit or score a model."""

import argparse
import hashlib
import json
import logging
import socket
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
LOG = logging.getLogger(__name__)
FIXED = "e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4"
BASELINE = "b83d2cc7d975e41f418f59aad3341a7d1574f95a"
START = "2026-11-01T00:00:00+00:00"
END = "2026-11-14T00:00:00+00:00"
LEAGUES = (
    "英超",
    "英冠",
    "西甲",
    "意甲",
    "德甲",
    "法甲",
    "法乙",
    "荷甲",
    "葡超",
    "日职",
    "瑞超",
    "挪超",
    "芬超",
    "巴甲",
    "美职",
)
FDORG = {"英超", "英冠", "西甲", "意甲", "德甲", "法甲", "荷甲", "葡超", "巴甲"}


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Missing timestamp timezone")
    return result.astimezone(UTC)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def no_network(*args, **kwargs):
    raise RuntimeError("Network is forbidden during T63 input preparation")


def snapshot_hashes(archive):
    return {
        name: sha((archive / name).read_bytes())
        for name in git(archive, "ls-tree", "-rz", "--name-only", FIXED)
        .decode()
        .split("\0")
        if name
    }


def read_rows(archive, manifest, table):
    return [
        dict(zip(manifest["columns"][table], json.loads(line), strict=True))
        for line in (archive / f"state/{table}.jsonl").read_text().splitlines()
    ]


def history_commits(archive, table):
    # First-parent order bounds a merged side branch by its containing merge. It
    # never assigns availability from an earlier, possibly unpublished side commit.
    lines = (
        git(
            archive,
            "log",
            "--first-parent",
            "--reverse",
            "--format=%H\t%cI",
            FIXED,
            "--",
            f"state/{table}.jsonl",
        )
        .decode()
        .splitlines()
    )
    return [line.split("\t") for line in lines]


def current_versions(archive, table, wanted, key_fields):
    first, events, prior = {}, defaultdict(list), {}
    commits = history_commits(archive, table)
    for commit, raw_stamp in commits:
        raw = git(archive, "show", f"{commit}:state/{table}.jsonl")
        current = {}
        for line in raw.splitlines():
            row = json.loads(line)
            if row[0] not in wanted:
                continue
            key = tuple(
                row[index] if len(row) > index else None for index in key_fields
            )
            current[row[0]] = key
            proof = {
                "commit": commit,
                "commit_time_raw": raw_stamp,
                "conservative_available_utc": stamp(raw_stamp).isoformat(),
                "path": f"state/{table}.jsonl",
                "row_version_sha256": sha(json.dumps(key, ensure_ascii=False).encode()),
                "method": "first_parent_archive_commit_containing_current_version",
            }
            if key == wanted[row[0]] and row[0] not in first:
                first[row[0]] = proof
            if prior.get(row[0]) != key:
                events[row[0]].append(dict(proof, version=list(key)))
        for key in prior.keys() - current.keys():
            events[key].append(
                {
                    "commit": commit,
                    "conservative_available_utc": stamp(raw_stamp).isoformat(),
                    "version": None,
                }
            )
        prior = current
    return first, dict(events), len(commits)


def prove_cutoffs(archive, targets):
    from zucai.forecast_archive import verify_forecasts
    from zucai.forecast_publication import publication_times
    from zucai.report.render import load_batch

    manifest = verify_forecasts(archive)
    proofs = publication_times(archive, manifest)
    ancestry = set(git(archive, "rev-list", FIXED).decode().splitlines())
    selected, eligible, rejected = {}, Counter(), Counter()
    for run in manifest["runs"].values():
        for entry in run["forecasts"].values():
            relative = entry["path"] + "/forecast.json"
            proof = proofs.get(relative)
            if not proof:
                rejected["batch_without_verified_publication"] += 1
                continue
            if proof["commit"] not in ancestry:
                rejected["publication_commit_not_in_fixed_ancestry"] += 1
                continue
            if (
                sha(git(archive, "show", f"{proof['commit']}:{relative}"))
                != proof["candidate_sha256"]
            ):
                rejected["publication_commit_blob_mismatch"] += 1
                continue
            batch = load_batch(archive / relative)
            when = {key: stamp(batch[key]) for key in ("as_of_utc", "generated_utc")}
            published = stamp(proof["published_utc"])
            ids = set(batch["input"]["match_ids"])
            for target in targets:
                key = target["match_id"]
                if key not in ids:
                    continue
                kickoff = stamp(target["kickoff_utc"])
                if (
                    batch["mode"] != "prematch"
                    or max(*when.values(), published) >= kickoff
                ):
                    continue
                eligible[key] += 1
                cutoff = {
                    "cutoff_utc": batch["as_of_utc"],
                    "generated_utc": batch["generated_utc"],
                    "published_utc": proof["published_utc"],
                    "batch_id": batch["batch_id"],
                    "forecast_path": relative,
                    "forecast_sha256": proof["candidate_sha256"],
                    "publication_proof": proof,
                    "original_batch_prediction_status": next(
                        (
                            row["status"]
                            for row in batch["predictions"]
                            if row["match_id"] == key
                        ),
                        "not_predicted_in_original_batch",
                    ),
                }
                previous = selected.get(key)
                if previous is None or (published, when["as_of_utc"], relative) > (
                    stamp(previous["published_utc"]),
                    stamp(previous["cutoff_utc"]),
                    previous["forecast_path"],
                ):
                    selected[key] = cutoff
    return selected, {
        "verified_runs": len(manifest["runs"]),
        "verified_publication_paths_all_types": len(proofs),
        "cutoff_eligible_batch_counts_by_target": dict(eligible),
        "rejected_batch_counts": dict(rejected),
        "selection": "latest verified publication before kickoff among batches explicitly containing target input ID",
    }


def latest_previous(target, side, matches, results, score_proofs, identity_proofs):
    if target.get("cutoff_utc") is None:
        return {"status": "no_verified_pre_kickoff_cutoff"}
    cutoff = stamp(target["cutoff_utc"])
    team_id = target[f"{side}_team_id"]
    candidates = []
    for match in matches:
        result = results.get(match["match_id"])
        if (
            match["league"] != target["league"]
            or match["status"] != "finished"
            or team_id not in (match["home_team_id"], match["away_team_id"])
            or stamp(match["kickoff_utc"]) >= cutoff
            or result is None
            or match["source"] not in {"jc", "fd", "apif", "fdorg"}
        ):
            continue
        proof = score_proofs.get(match["match_id"])
        if proof is None or stamp(proof["conservative_available_utc"]) >= stamp(END):
            continue
        if not result["available_utc"] or stamp(result["available_utc"]) >= stamp(END):
            continue
        candidates.append(match)
    if not candidates:
        return {"status": "no_identifiable_previous_result"}
    last_time = max(stamp(row["kickoff_utc"]) for row in candidates)
    latest = [row for row in candidates if stamp(row["kickoff_utc"]) == last_time]
    details = []
    for match in latest:
        key = match["match_id"]
        identity = identity_proofs.get(key)
        result = results[key]
        paid = match["source"] in {"apif", "fdorg"}
        status = (
            "unproven_historical_identity"
            if paid
            else "normalized_identity_not_present_before_cutoff"
            if identity is None
            or stamp(identity["conservative_available_utc"]) >= cutoff
            else "legacy_normalized_identity_present_before_cutoff"
        )
        details.append(
            {
                "match_id": key,
                "source": match["source"],
                "kickoff_utc": match["kickoff_utc"],
                "identity_status": status,
                "result": result,
                "current_score_version_proof": score_proofs[key],
                "current_normalized_identity_first_presence": identity,
                "legacy_model_time_before_cutoff": bool(
                    result["available_utc"] and stamp(result["available_utc"]) < cutoff
                ),
                "archive_current_score_version_before_cutoff": stamp(
                    score_proofs[key]["conservative_available_utc"]
                )
                < cutoff,
                "current_counterpart_not_historical_proof": match["counterpart_id"],
            }
        )
    target_identity = identity_proofs.get(target["match_id"])
    status = "identified_legacy_previous"
    if (
        target_identity is None
        or stamp(target_identity["conservative_available_utc"]) >= cutoff
    ):
        status = "target_normalized_identity_not_present_before_cutoff"
    elif len(details) != 1:
        status = "unknown_multiple_latest_rows_without_historical_link"
    elif (
        details[0]["identity_status"]
        != "legacy_normalized_identity_present_before_cutoff"
    ):
        status = details[0]["identity_status"]
    return {
        "status": status,
        "latest_kickoff_utc": last_time.isoformat(),
        "candidates": details,
        "join_basis": "current numeric database team ID; added-source matches remain unapproved without historical binding snapshots",
        "not_a_training_selection_or_coverage_measure": True,
    }


def prepare(archive, code, output):
    if git(archive, "rev-parse", "HEAD").decode().strip() != FIXED:
        raise ValueError("The source snapshot must be exactly the preregistered commit")
    if output.resolve().is_relative_to(archive.resolve()):
        raise ValueError("Preparation output cannot be inside the fixed archive")
    output.mkdir(parents=True, exist_ok=True)
    before = snapshot_hashes(archive)
    save(output / "source-files-sha256-before.json", before)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    matches = read_rows(archive, manifest, "matches")
    results = {row["match_id"]: row for row in read_rows(archive, manifest, "results")}
    teams = {row["id"]: row for row in read_rows(archive, manifest, "teams")}
    scope = [row for row in matches if row["league"] in LEAGUES]
    targets = sorted(
        [
            row
            for row in scope
            if row["source"] == "jc"
            and stamp(START) <= stamp(row["kickoff_utc"]) < stamp(END)
        ],
        key=lambda row: (row["kickoff_utc"], row["match_id"]),
    )
    selected, publication_inventory = prove_cutoffs(archive, targets)
    LOG.info("Frozen cutoff evidence: %s/%s targets", len(selected), len(targets))
    wanted_scores = {
        row["match_id"]: tuple(
            results[row["match_id"]][key]
            for key in (
                "match_id",
                "home_goals",
                "away_goals",
                "ht_home_goals",
                "ht_away_goals",
            )
        )
        for row in scope
        if row["match_id"] in results
    }
    score_proofs, score_events, score_commits = current_versions(
        archive, "results", wanted_scores, (0, 1, 2, 3, 4)
    )
    wanted_identities = {
        row["match_id"]: tuple(
            row[key]
            for key in (
                "match_id",
                "source",
                "league",
                "home_team_id",
                "away_team_id",
                "kickoff_utc",
            )
        )
        for row in scope
    }
    identity_proofs, _, identity_commits = current_versions(
        archive, "matches", wanted_identities, (0, 1, 2, 3, 4, 5)
    )
    save(output / "current-score-version-first-commits.json", score_proofs)
    save(output / "score-version-events.json", score_events)
    save(output / "normalized-match-first-presence.json", identity_proofs)
    target_rows = []
    for match in targets:
        key = match["match_id"]
        row = dict(
            match,
            home=teams[match["home_team_id"]]["name"],
            away=teams[match["away_team_id"]]["name"],
        )
        row.update(
            selected.get(
                key,
                {
                    "cutoff_utc": None,
                    "cutoff_exclusion": "no_verified_pre_kickoff_cutoff",
                },
            )
        )
        result, proof = results.get(key), score_proofs.get(key)
        label_valid = bool(
            match["status"] == "finished"
            and result
            and proof
            and result["available_utc"]
            and stamp(result["available_utc"]) < stamp(END)
            and stamp(proof["conservative_available_utc"]) < stamp(END)
        )
        row.update(
            label_status="fixed_snapshot_result_before_label_cutoff"
            if label_valid
            else "no_proven_label_before_fixed_cutoff",
            result=result,
            current_score_version_proof=proof,
            current_normalized_identity_first_presence=identity_proofs.get(key),
            source_identity_note="Names are display labels only, never historical approval evidence.",
            old_arm_status="not_run_protocol_review_pending",
            new_arm_status="not_run_protocol_review_pending",
        )
        row["previous"] = {
            side: latest_previous(
                row, side, scope, results, score_proofs, identity_proofs
            )
            for side in ("home", "away")
        }
        target_rows.append(row)
    save(output / "targets.json", target_rows)
    inventory = []
    for league in LEAGUES:
        for source in ("fd", "jc", "apif", "fdorg"):
            rows = [
                row
                for row in scope
                if row["league"] == league
                and row["source"] == source
                and row["match_id"] in results
            ]
            inventory.append(
                {
                    "league": league,
                    "source": source,
                    "result_rows": len(rows),
                    "stored_availability_kinds": dict(
                        Counter(
                            results[row["match_id"]]["available_source"] for row in rows
                        )
                    ),
                    "earliest_kickoff_utc": min(
                        (row["kickoff_utc"] for row in rows), default=None
                    ),
                    "latest_kickoff_utc": max(
                        (row["kickoff_utc"] for row in rows), default=None
                    ),
                    "earliest_stored_available_utc": min(
                        (
                            results[row["match_id"]]["available_utc"]
                            for row in rows
                            if results[row["match_id"]]["available_utc"]
                        ),
                        default=None,
                    ),
                    "current_score_version_with_commit_proof": sum(
                        row["match_id"] in score_proofs for row in rows
                    ),
                    "current_counterpart_references": sum(
                        bool(row["counterpart_id"]) for row in rows
                    ),
                    "allowed_in_new_arm": source in {"fd", "jc", "apif"}
                    or league in FDORG,
                }
            )
    save(output / "source-inventory.json", inventory)
    sidecar_paths = sorted(
        path.relative_to(archive).as_posix() for path in archive.rglob("*.meta.json")
    )
    identity_inventory = {
        "timestamped_result_identity_file_exists": (
            archive / "state/result_source_identities.jsonl"
        ).exists(),
        "timestamped_result_link_file_exists": (
            archive / "state/result_source_links.jsonl"
        ).exists(),
        "explicit_identity_manifest": manifest.get("result_source_identities"),
        "explicit_link_manifest": manifest.get("result_source_links"),
        "technical_fixture_index_rows": manifest.get("technical_data", {})
        .get("counts", {})
        .get("apif_fixture_index", 0),
        "fd_stats_link_rows": manifest.get("fd_stats_links", {}).get("count", 0),
        "http_sidecar_count": len(sidecar_paths),
        "http_sidecar_paths": sidecar_paths,
        "not_accepted_as_historical_paid_identity": [
            "Current team names or aliases without a historical verification snapshot",
            "Current counterpart_id without a timestamped approved link",
            "Technical fixture captured_utc alone: this is response capture, not identity verification time",
            "A present-day normalized-record recheck backdated to an old response timestamp",
        ],
    }
    save(output / "identity-and-raw-evidence-inventory.json", identity_inventory)
    params_raw = git(code, "show", f"{BASELINE}:data/model/params.json")
    (output / "baseline-params.json").write_bytes(params_raw)
    protocol = code / "docs/experiments/T63-source-latency-protocol.md"
    previous_counts = Counter(
        item["status"] for row in target_rows for item in row["previous"].values()
    )
    summary = {
        "kind": "input_census_only_no_probability_or_loss_calculation",
        "fixed_snapshot": FIXED,
        "baseline_code": BASELINE,
        "protocol_sha256": sha(protocol.read_bytes()),
        "baseline_params_sha256": sha(params_raw),
        "window_start_utc_inclusive": START,
        "window_end_utc_exclusive": END,
        "targets": len(target_rows),
        "targets_by_league": dict(Counter(row["league"] for row in target_rows)),
        "verified_pre_kickoff_cutoff_targets": len(selected),
        "no_verified_pre_kickoff_cutoff_targets": len(target_rows) - len(selected),
        "valid_fixed_snapshot_labels": sum(
            row["label_status"] == "fixed_snapshot_result_before_label_cutoff"
            for row in target_rows
        ),
        "cutoff_and_label_candidate_denominator": sum(
            row.get("cutoff_utc") is not None
            and row["label_status"] == "fixed_snapshot_result_before_label_cutoff"
            for row in target_rows
        ),
        "original_chosen_batch_not_predicted_targets": sum(
            row.get("original_batch_prediction_status") not in (None, "predicted")
            for row in target_rows
        ),
        "previous_team_occurrences": len(target_rows) * 2,
        "previous_statuses": dict(previous_counts),
        "strict_added_source_identity_rows_from_dedicated_snapshots": 0
        if not identity_inventory["timestamped_result_identity_file_exists"]
        else None,
        "strict_added_source_link_rows_from_dedicated_snapshots": 0
        if not identity_inventory["timestamped_result_link_file_exists"]
        else None,
        "probability_fit_calls": 0,
        "metrics_calculated": False,
        "sports_network_requests": 0,
        "formal_key_uses": 0,
        "ancestry": {
            "shallow": git(archive, "rev-parse", "--is-shallow-repository").strip()
            != b"false",
            "reachable_commits": int(git(archive, "rev-list", "--count", FIXED)),
            "first_parent_commits": int(
                git(archive, "rev-list", "--first-parent", "--count", FIXED)
            ),
            "score_snapshot_changes_scanned": score_commits,
            "normalized_identity_snapshot_changes_scanned": identity_commits,
            "conservative_method": "first containing exact current score version on fixed archive first-parent ancestry; no fetch and no future commits",
        },
        "publication_evidence": publication_inventory,
        "blockers": [
            "Protocol revision awaits independent review; no paired forecast experiment is authorized yet.",
            "25 targets have no verified pre-kickoff cutoff; no manufactured cutoff is substituted.",
            "The fixed snapshot contains no dedicated timestamped API identity/link tables. Current aliases and counterparts are inventory only.",
            "Previous-game candidates sharing a current team ID are not promoted to historical approval or entity links.",
            "Source version commits provide conservative observation bounds, not true first ingestion times.",
            "The candidate denominator is below the preregistered minimum of 500 for a confirmed-quality-improvement claim.",
        ],
    }
    after = snapshot_hashes(archive)
    if before != after:
        raise RuntimeError("Fixed source archive changed during census")
    summary["all_tracked_source_files_unchanged"] = True
    summary["source_file_count"] = len(before)
    summary["source_hash_manifest_sha256"] = sha(
        (output / "source-files-sha256-before.json").read_bytes()
    )
    save(output / "census.json", summary)
    LOG.info(
        "Census complete: %s targets, %s candidate denominator, no fitting",
        len(target_rows),
        summary["cutoff_and_label_candidate_denominator"],
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code / "src"))
    socket.create_connection = no_network
    import requests

    requests.sessions.Session.request = no_network
    return prepare(args.archive, args.code, args.output)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
