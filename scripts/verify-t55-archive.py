#!/usr/bin/env python3
"""Read-only T55 acceptance replay from fixed archived bytes and local Git history."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from zucai.cloud_feeds import restore_feeds
from zucai.forecast_archive import verify_forecasts
from zucai.forecast_publication import publication_times
from zucai.scoreboard.experimental import load_experiments, summarize_experiments
from zucai.scoreboard.paper import advance_ledger, publication_view
from zucai.scoreboard.score import (
    instant,
    load_forecasts,
    outcome,
    prematch_rows,
    read_results,
    score_forecasts,
    summarize_scores,
)

FOUR = {"jc:2042592", "jc:2042593", "jc:2042594", "jc:2042595"}
COMMIT = "b492f4d7c501fd8cdbd2694d760f2f13892f796c"


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--git-repository", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    archive, out = args.archive.resolve(), args.output.resolve()
    if out.is_relative_to(archive):
        raise ValueError("output must be outside the read-only archive")
    out.mkdir(parents=True, exist_ok=False)
    before = hashes(archive)
    manifest = verify_forecasts(archive)
    archived_summary = json.loads((archive / "scoreboard/summary.json").read_bytes())
    at = instant(archived_summary["as_of_utc"])
    publications = publication_times(archive, manifest, repository=args.git_repository)
    rows = load_forecasts(archive, at, publications=publications)
    database = out / "restored.db"
    restore_feeds(archive, database)
    database_before = hashlib.sha256(database.read_bytes()).hexdigest()
    results = read_results(database)
    # Exact old selection rule: generated time was the only publication gate.
    old_rows = [dict(row, published_utc=row["generated_utc"]) for row in rows]
    old_scores, old_audit = score_forecasts(old_rows, results, [], at)
    new_scores, new_audit = score_forecasts(rows, results, [], at)
    old_selected = {r["match_id"]: r for r in prematch_rows(old_rows, at)}
    new_selected = {r["match_id"]: r for r in prematch_rows(rows, at)}
    comparisons = []
    for mid in sorted(FOUR):
        old, new = old_selected[mid], new_selected[mid]
        assert old["batch_id"] == "42380973719-1-2026-11-07"
        assert new["batch_id"] == "42358846882-1-2026-11-07"
        actual = outcome(results[mid])
        comparisons.append(
            dict(
                match_id=mid,
                home=old["home"],
                away=old["away"],
                kickoff_utc=old["kickoff_utc"],
                old_batch_id=old["batch_id"],
                new_batch_id=new["batch_id"],
                old_generated_utc=old["generated_utc"],
                new_generated_utc=new["generated_utc"],
                old_publication=publications[old["forecast_path"]],
                new_publication=publications[new["forecast_path"]],
                old_probabilities=old["model_probabilities"],
                new_probabilities=new["model_probabilities"],
                probability_bytes_equal=raw(old["model_probabilities"])
                == raw(new["model_probabilities"]),
                actual=actual,
                score=[results[mid]["home_goals"], results[mid]["away_goals"]],
                old_hit=max(old["model_probabilities"], key=old["model_probabilities"].get)
                == actual,
                new_hit=max(new["model_probabilities"], key=new["model_probabilities"].get)
                == actual,
            )
        )
    # Old latest rows that were already published before kickoff remain byte identical.
    unchanged_ids = {
        mid
        for mid, row in old_selected.items()
        if mid in new_selected and row["batch_id"] == new_selected[mid]["batch_id"]
    }
    old_score_map = {r["match_id"]: r for r in old_scores}
    new_score_map = {r["match_id"]: r for r in new_scores}
    unchanged_scored = sorted(unchanged_ids & old_score_map.keys() & new_score_map.keys())
    old_unchanged = [old_score_map[mid] for mid in unchanged_scored]
    new_unchanged = [new_score_map[mid] for mid in unchanged_scored]
    assert raw(old_unchanged) == raw(new_unchanged)
    # Explicit constructed edge cases use a real archived row without changing its file.
    template = next(r for r in rows if r["match_id"] in FOUR and r["status"] == "predicted")
    cases = [
        dict(template, match_id="synthetic:late", published_utc=template["kickoff_utc"]),
        dict(template, match_id="synthetic:unknown", published_utc=None),
    ]
    excluded_scores, exclusions = score_forecasts(cases, {}, [], at)
    assert not excluded_scores
    assert exclusions["excluded"] == {"published_after_kickoff": 1, "publication_unknown": 1}
    # Exercise paper read views on actual frozen legacy rows without writing any old ledger.
    ledger = [
        json.loads(line) for line in (archive / "scoreboard/ledger.jsonl").read_bytes().splitlines()
    ]
    ledger_before = raw(ledger)
    settled_view = advance_ledger(rows, results, ledger, at)
    paper_view, paper_audit = publication_view(rows, results, settled_view, at)
    assert raw(ledger) == ledger_before
    experiments, experiment_audit = load_experiments(
        archive, manifest, at, publications=publications
    )
    experiment_scores, _ = score_forecasts(experiments, results, [], at, experimental=True)
    experimental = summarize_experiments(
        experiments, experiment_scores, new_scores, new_audit["active_keys"], at
    )
    old_experiment_publications = {}
    for info in manifest["runs"].values():
        for entry in info["forecasts"].values():
            name = entry.get("experimental_path")
            if name:
                doc = json.loads((archive / name).read_bytes())
                old_experiment_publications[name] = {
                    "published_utc": max(
                        doc["generated_utc"],
                        entry.get("experimental_published_utc", doc["generated_utc"]),
                    )
                }
    old_experiments, _ = load_experiments(
        archive, manifest, at, publications=old_experiment_publications
    )
    old_experiment_scores, _ = score_forecasts(old_experiments, results, [], at, experimental=True)
    old_experimental = summarize_experiments(
        old_experiments, old_experiment_scores, old_scores, old_audit["active_keys"], at
    )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_before
    after = hashes(archive)
    assert before == after
    summary = dict(
        archive_commit=COMMIT,
        actual_replay_generated_utc=datetime.now(UTC).isoformat(),
        historical_as_of_utc=at.isoformat(),
        mode="historical_replay",
        production_written=False,
        publication_rule="strictly before kickoff and no later than replay cutoff",
        legacy_method="first remote-reachable file-addition commit, exact immutable blob SHA256",
        archive_files_unchanged=len(before),
        frozen_categories=dict(Counter(p.split("/")[0] for p in before)),
        four=comparisons,
        four_probability_bytes_equal=all(r["probability_bytes_equal"] for r in comparisons),
        old_hits=sum(r["old_hit"] for r in comparisons),
        new_hits=sum(r["new_hit"] for r in comparisons),
        already_published_scored_rows_byte_identical=len(unchanged_scored),
        unchanged_scored_sha256=hashlib.sha256(raw(old_unchanged)).hexdigest(),
        constructed_exclusions=exclusions["excluded"],
        actual_exclusions=new_audit["excluded"],
        old_selected_matches=old_audit["selected_matches"],
        new_selected_matches=new_audit["selected_matches"],
        old_scored_rows=len(old_scores),
        new_scored_rows=len(new_scores),
        publication_evidence_files=len(publications),
        paper_publication_audit=paper_audit,
        paper_view_rows=len(paper_view),
        original_ledger_bytes_unchanged=True,
        restored_database_sha256_unchanged=database_before,
        experiment_file_audit=experiment_audit,
        old_xg_common_n=sum(
            g["common_n"] for g in old_experimental["groups"] if g["model_name"] == "dc-xg-v1"
        ),
        new_xg_common_n=sum(
            g["common_n"] for g in experimental["groups"] if g["model_name"] == "dc-xg-v1"
        ),
        experimental_common_samples=[
            dict(model_name=g["model_name"], league=g["league"], common_n=g["common_n"])
            for g in experimental["groups"]
        ],
    )
    for name, doc in {
        "summary.json": summary,
        "four-comparison.json": comparisons,
        "publication-evidence.json": publications,
        "archive-sha256-before.json": before,
        "archive-sha256-after.json": after,
        "old-unchanged-scores.json": old_unchanged,
        "new-unchanged-scores.json": new_unchanged,
        "score-summary.json": summarize_scores(
            new_scores, at, active_keys=new_audit["active_keys"]
        ),
        "experimental-summary.json": experimental,
        "old-experimental-summary.json": old_experimental,
    }.items():
        (out / name).write_bytes(raw(doc))
    database.unlink()  # Restored DB is derived temporary evidence, not a portable artifact.
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
