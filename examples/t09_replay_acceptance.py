"""Replay T09 capture artifacts on a COPY of the T08 DB, then compare identical EPL cutoffs."""

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from zucai.predict.command import parse_cutoff, train
from zucai.sources.fdorg_command import ALIASES, fetch_fdorg
from zucai.sources.football_data import read_json


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args):
    if not args.baseline.is_file():
        raise ValueError("A real T08 baseline DB is required")
    capture = read_json(args.capture / "capture.json")
    raw = args.capture / "raw" / "football-data-org"
    # A hash can repeat over several polls; keep the original capture time as part of the key.
    files = {}
    for sidecar in raw.rglob("*.json.meta.json"):
        meta = read_json(sidecar)
        if meta["status_code"] == 200:
            files[(meta["sha256"], meta["fetched_at_utc"])] = Path(
                str(sidecar).removesuffix(".meta.json")
            )
    args.output.mkdir(parents=True, exist_ok=False)
    identities = args.output / "identity"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "zucai.sources.fdorg_verify",
            "--db",
            str(args.baseline),
            "--raw-dir",
            str(raw),
            "--season",
            str(args.season),
            "--aliases",
            str(args.aliases),
            "--output",
            str(identities),
        ],
        check=True,
    )
    db = args.output / "comparison.db"
    with closing(sqlite3.connect(args.baseline.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(db)) as dst:
            src.backup(dst)
    coverage = read_json(identities / "coverage.json")
    anchors, gaps = (read_json(identities / name) for name in ("links.json", "unlinked.json"))
    totals, missing = {}, {}
    for code, observations in capture["codes"].items():
        successes = [r for r in observations if r.get("meta")]
        if not successes:
            totals[code] = {"status": "no captured season"}
            continue
        counts, unmatched, linked_jc, refused = {}, set(), set(), {}
        for row in sorted(successes, key=lambda r: r["meta"]["fetched_at_utc"]):
            meta = row["meta"]
            file = files[meta["sha256"], meta["fetched_at_utc"]]
            report = fetch_fdorg(
                competition=code,
                seasons=[row["year"]],
                assume=timedelta(hours=3),
                finished_only=row["year"] != args.season,
                db=db,
                raw_file=file,
                aliases_path=identities / "aliases.json",
                audit_dir=args.output,
            )
            for key in (
                "inserted",
                "linked_existing",
                "linked_fd",
                "rejected_rows",
                "suspected_duplicates",
                "assumed",
                "corrected",
                "unchanged",
            ):
                counts[key] = counts.get(key, 0) + report[key]
            unmatched.update((n["league"], n["id"]) for n in report["files"][0]["unmatched_names"])
            detail = report["files"][0]
            if row["year"] == args.season:
                linked_jc.update(entry["jc_id"] for entry in detail["links"] if "jc_id" in entry)
                for entry in detail["rejected"] + detail["suspected"]:
                    refused[f"fdorg:{entry.get('provider_match_id')}"] = entry["reason"]
            write(args.output / f"{code}-{row['year']}-audit.json", report)
        earliest = min((r["year"] for r in successes if r["rows"] > 0), default=None)
        denominator = coverage[code]["comparable_jc"]
        eligible = {entry["jc_id"]: entry for entry in anchors[code] + gaps[code]}
        linked = len(linked_jc.intersection(eligible)) if denominator is not None else None
        missing[code] = (
            [
                dict(
                    entry,
                    import_reason=refused.get(
                        entry.get("fdorg_id"),
                        entry.get(
                            "reason", "verified candidate absent from current-season import links"
                        ),
                    ),
                )
                for key, entry in eligible.items()
                if key not in linked_jc
            ]
            if denominator is not None
            else None
        )
        totals[code] = dict(
            counts,
            unmatched_teams=len(unmatched),
            earliest_confirmed=earliest,
            comparable_jc=denominator,
            actually_linked_jc=linked,
            current_link_rate=linked / denominator if denominator else None,
        )
    write(args.output / "actual_unlinked.json", missing)
    write(args.output / "league_counts.json", totals)
    for sources in ["jc,fd", "jc,fd,fdorg"]:
        stem = sources.replace(",", "_")
        result = train(
            db=db,
            league="英超",
            source=sources,
            as_of=parse_cutoff(args.as_of),
            since=parse_cutoff(args.since),
            half_life_days=90,
            output=args.output / f"{stem}.pkl",
        )
        write(args.output / f"{stem}.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--season", required=True, type=int)
    parser.add_argument("--aliases", type=Path, default=ALIASES)
    parser.add_argument("--since", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args())
