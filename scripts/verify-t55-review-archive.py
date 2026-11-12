#!/usr/bin/env python3
"""Replay PR115 review counterexamples in disposable full-history data worktrees."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from zucai.daily_digest import generate
from zucai.parlay import paper as f_paper
from zucai.scoreboard.command import read_state, run
from zucai.scoreboard.paper import account_statistics
from zucai.scoreboard.score import instant

COMMIT = "b492f4d7c501fd8cdbd2694d760f2f13892f796c"
UNKNOWN_BATCH = "41509202754-1-2026-11-03"


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def rows(content):
    return [json.loads(line) for line in content.splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--git-repository", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--observe-only", action="store_true", help="record old behavior without repair assertions"
    )
    args = parser.parse_args()
    archive, repository, output = (
        args.archive.resolve(),
        args.git_repository.resolve(),
        args.output.resolve(),
    )
    if output.is_relative_to(archive):
        raise ValueError("output must be outside the immutable input archive")
    output.mkdir(parents=True, exist_ok=False)
    source_hashes = hashes(archive)
    original_summary = json.loads((archive / "scoreboard/summary.json").read_bytes())
    at = instant(original_summary["as_of_utc"])
    digest_at = max(
        [
            at,
            *[
                instant(json.loads(path.read_bytes())["generated_utc"])
                for path in (archive / "daily/2026-11-12").glob("*.json")
                if not path.name.endswith(".notification.json")
            ],
        ]
    )
    original_ae = (archive / "scoreboard/ledger.jsonl").read_bytes()
    original_f = (archive / f_paper.LEDGER).read_bytes()
    ae_rows, f_rows = rows(original_ae), rows(original_f)
    f_batch = next(row["batch_id"] for row in f_rows if row["record_type"] == "group")
    results = {}
    for case, excluded_batch in (
        ("unchanged", None),
        ("nov3_publication_unknown", UNKNOWN_BATCH),
        ("f_publication_unknown", f_batch),
    ):
        destination = output / case
        destination.mkdir()
        worktree = destination / "archive"
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree), COMMIT],
            check=True,
            capture_output=True,
        )
        try:
            assert hashes(worktree) == source_hashes
            if excluded_batch:
                path = worktree / "forecast_manifest.json"
                manifest = json.loads(path.read_bytes())
                changed = []
                for info in manifest["runs"].values():
                    for entry in info["forecasts"].values():
                        if entry["batch_id"] == excluded_batch:
                            entry["publication_required"] = True
                            for prefix in ("", "experimental_", "parlay_"):
                                entry.pop(prefix + "published_utc", None)
                                entry.pop(prefix + "publication_evidence", None)
                            changed.append(entry["path"])
                assert len(changed) == 1
                path.write_bytes(raw(manifest))
            injected_hashes = hashes(worktree)
            summary = run(worktree, as_of=at)
            after_ae = (worktree / "scoreboard/ledger.jsonl").read_bytes()
            after_f = (worktree / f_paper.LEDGER).read_bytes()
            expected_ae = [
                row
                for row in ae_rows
                if row["record_type"] != "ticket"
                or not any(leg.get("batch_id") == excluded_batch for leg in row["legs"])
            ]
            expected_f = [
                row
                for row in f_rows
                if row["record_type"] not in {"group", "ticket"}
                or row["batch_id"] != excluded_batch
            ]
            expected_accounts = account_statistics(expected_ae, at)
            # Exact fields/markers of all unaffected frozen records remain inspectable.
            retained = {row["record_id"]: row for row in expected_ae}
            retained_f = {row["record_id"]: row for row in expected_f}
            actual_ae = {row["record_id"]: row for row in rows(after_ae)}
            actual_f = {row["record_id"]: row for row in rows(after_f)}
            expected_f_summary = f_paper.summarize(expected_f, {}, at)
            digest = generate(
                worktree, destination / "digest", now=digest_at, run_id="t55-review-" + case
            )
            after_hashes = hashes(worktree)
            changed_files = sorted(
                name for name in injected_hashes if injected_hashes[name] != after_hashes[name]
            )
            pending_keys = set(summary["audit"]["active_keys"]) - {
                row["record_id"] for row in read_state(worktree)[0]
            }
            result = dict(
                excluded_batch=excluded_batch,
                original_ae_rows=len(ae_rows),
                resulting_ae_rows=len(rows(after_ae)),
                original_f_rows=len(f_rows),
                resulting_f_rows=len(rows(after_f)),
                ae_ledger_bytes_unchanged=after_ae == original_ae,
                f_ledger_bytes_unchanged=after_f == original_f,
                retained_ae_records_exact=all(
                    actual_ae.get(key) == row for key, row in retained.items()
                ),
                retained_f_records_exact=all(
                    actual_f.get(key) == row for key, row in retained_f.items()
                ),
                account_summary_is_original_subset=summary["accounts"] == expected_accounts,
                f_totals_are_original_subset=summary["parlay_sd"]["totals"]
                == expected_f_summary["totals"],
                f_evidence_is_original_subset=summary["parlay_sd"]["evidence"]
                == expected_f_summary["evidence"],
                paper_publication=summary["audit"]["paper_publication"],
                f_publication=summary["parlay_sd"]["publication_audit"],
                a_105=summary["accounts"]["1.05"]["A"],
                expected_a_105=expected_accounts["1.05"]["A"],
                prospective_ticket_markers_preserved=all(
                    actual_ae.get(key, {}).get("replayed") == row.get("replayed")
                    for key, row in retained.items()
                ),
                active_keys=len(summary["audit"]["active_keys"]),
                pending_keys=len(pending_keys),
                zero_sample_leagues=[
                    name
                    for name, value in summary["probability"]["leagues"].items()
                    if value["n"] == 0
                ],
                digest_contains_zero_leagues=all(
                    name + "：前瞻样本 0 / 200" in digest.read_text()
                    for name, value in summary["probability"]["leagues"].items()
                    if value["n"] == 0
                ),
                changed_files_after_injection=changed_files,
            )
            (destination / "summary.json").write_bytes(raw(result))
            (destination / "scoreboard-summary.json").write_bytes(raw(summary))
            (destination / "expected-account-summary.json").write_bytes(raw(expected_accounts))
            (destination / "expected-f-summary.json").write_bytes(raw(expected_f_summary))
            (destination / "archive-sha256-before.json").write_bytes(raw(injected_hashes))
            (destination / "archive-sha256-after.json").write_bytes(raw(after_hashes))
            results[case] = result
        finally:
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)],
                check=True,
                capture_output=True,
            )
    assert hashes(archive) == source_hashes
    evidence = dict(
        archive_commit=COMMIT,
        generated_utc=datetime.now(UTC).isoformat(),
        historical_scoreboard_as_of=at.isoformat(),
        historical_digest_as_of=digest_at.isoformat(),
        source_files_unchanged=len(source_hashes),
        production_written=False,
        cases=results,
    )
    (output / "summary.json").write_bytes(raw(evidence))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    if not args.observe_only:
        for case, result in results.items():
            assert result["ae_ledger_bytes_unchanged"] and result["f_ledger_bytes_unchanged"], case
            assert result["retained_ae_records_exact"] and result["retained_f_records_exact"], case
            assert result["account_summary_is_original_subset"], case
            assert (
                result["f_totals_are_original_subset"] and result["f_evidence_is_original_subset"]
            ), case
            assert result["prospective_ticket_markers_preserved"], case
            assert result["digest_contains_zero_leagues"], case
            assert set(result["changed_files_after_injection"]) <= {
                "scoreboard/summary.json",
                "scoreboard/scored.jsonl",
                "scoreboard/scoreboard.md",
            }, case
        assert results["nov3_publication_unknown"]["paper_publication"]["excluded_tickets"] == 33
        assert results["unchanged"]["pending_keys"] == 27


if __name__ == "__main__":
    main()
