#!/usr/bin/env python3
"""Read a fixed feeds checkout and report all 90 master rows without guessing source IDs."""

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from zucai.league_registry import REGISTRY_PATH, load_registry


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path, columns):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            values = json.loads(line)
            if not isinstance(values, list) or len(values) != len(columns):
                raise ValueError(f"Invalid archive row in {path.name}")
            yield dict(zip(columns, values, strict=True))


def coverage(archive, registry_path):
    registry = load_registry(registry_path)
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    paths = ["state/matches.jsonl", "state/results.jsonl", "state/apif_fixture_index.jsonl"]
    hashes = {relative: digest(archive / relative) for relative in paths}
    for relative, checksum in hashes.items():
        if manifest["files"].get(relative) != checksum:
            raise ValueError(f"Archive hash mismatch: {relative}")
    hashes["feed_manifest.json"] = digest(manifest_path)
    matches = list(jsonl(archive / paths[0], manifest["columns"]["matches"]))
    results = {row["match_id"] for row in jsonl(archive / paths[1], manifest["columns"]["results"])}
    match_ids = {row["match_id"] for row in matches}
    if len(matches) != len(match_ids) or not results <= match_ids:
        raise ValueError("Duplicate match IDs or dangling results")
    by_label = defaultdict(list)
    for match in matches:
        by_label[match["league"]].append(match)
    aliases = defaultdict(list)
    for row in registry["competitions"]:
        for name in set([row["project_name"], *row["source_names"]]):
            aliases[name].append(row["master_row_id"])
    observed = defaultdict(lambda: defaultdict(set))
    index_columns = manifest["technical_data"]["columns"]["apif_fixture_index"]
    for item in jsonl(archive / paths[2], index_columns):
        if item["match_id"] in match_ids:
            observed[str(item["provider_league_id"])][str(item["season"])].add(item["match_id"])
    output = []
    for row in registry["competitions"]:
        games = by_label[row["project_name"]]
        ids = {game["match_id"] for game in games}
        yearly = defaultdict(lambda: {"matches": 0, "results": 0})
        for game in games:
            year = game["kickoff_utc"][:4]
            yearly[year]["matches"] += 1
            yearly[year]["results"] += game["match_id"] in results
        source_seasons = []
        for mapping in row["mappings"]:
            if mapping["provider"] != "apif" or mapping["status"] != "verified":
                continue
            for season, linked in sorted(observed[mapping["code"]].items()):
                selected = ids & linked
                if selected:
                    source_seasons.append(
                        {
                            "provider": "apif",
                            "code": mapping["code"],
                            "season": season,
                            "linked_stored_matches": len(selected),
                            "linked_stored_results": len(selected & results),
                        }
                    )
        ambiguous = [
            {
                "label": name,
                "master_row_ids": aliases[name],
                "stored_label_matches": len(by_label[name]),
            }
            for name in sorted(set([row["project_name"], *row["source_names"]]))
            if len(aliases[name]) > 1
        ]
        reasons = [row["missing_reason"]] if row["missing_reason"] else []
        if ambiguous:
            reasons.append(
                "shared source labels require competition/edition review; counts are stored-label "
                "observations, not disambiguated master-row totals"
            )
        if not source_seasons:
            reasons.append(
                "provider season absent from match rows and no linked provider fixture season "
                "evidence; calendar years below are not seasons"
            )
        if not games:
            reasons.append(
                "no stored match uses this exact project label; "
                "this does not prove provider absence"
            )
        if row["status"] == "verified":
            reasons.append(
                "all readable seasons and complete B/C scope still require catalogue review "
                "and online acceptance"
            )
        output.append(
            {
                "master_row_id": row["master_row_id"],
                "master_name": row["master_name"],
                "project_name": row["project_name"],
                "source_names_raw": row["source_names_raw"],
                "status": row["status"],
                "mappings": row["mappings"],
                "stored_exact_project_label_matches": len(games),
                "stored_exact_project_label_results": len(ids & results),
                "stored_sources": dict(sorted(Counter(game["source"] for game in games).items())),
                "observed_provider_seasons": source_seasons,
                "observed_calendar_years": dict(sorted(yearly.items())),
                "ambiguous_source_labels": ambiguous,
                "remaining": reasons,
            }
        )
    root = subprocess.check_output(
        ["git", "-C", str(archive), "rev-parse", "--show-toplevel"], text=True
    ).strip()
    if Path(root).resolve() != archive.resolve():
        raise ValueError("Archive must be its own fixed checkout")
    commit = subprocess.check_output(
        ["git", "-C", str(archive), "rev-parse", "HEAD"], text=True
    ).strip()
    if any(digest(archive / path) != checksum for path, checksum in hashes.items()):
        raise ValueError("Archive changed while reading")
    return {
        "version": 1,
        "archive_commit": commit,
        "archive_inputs_sha256": hashes,
        "registry_sha256": digest(registry_path),
        "master_rows": len(output),
        "archive_matches": len(matches),
        "archive_results": len(results),
        "method": (
            "Exact stored project label only; no fuzzy alias joins, no season inference, "
            "no new verification. Source seasons are linked APIF fixture-index evidence only. "
            "Shared labels remain explicitly ambiguous. Counts across providers may include "
            "distinct stored representations of one real match."
        ),
        "rows": output,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args()
    archive, out = args.archive.resolve(), args.out.resolve()
    if out == archive or archive in out.parents:
        parser.error("--out must be outside the read-only archive")
    report = coverage(archive, args.registry)
    out.mkdir(parents=True, exist_ok=True)
    (out / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    columns = [
        "master_row_id",
        "master_name",
        "project_name",
        "status",
        "mappings",
        "stored_matches",
        "stored_results",
        "observed_provider_seasons",
        "calendar_years_not_seasons",
        "remaining",
    ]
    with (out / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in report["rows"]:
            writer.writerow(
                {
                    **{key: row[key] for key in columns[:4]},
                    "mappings": "; ".join(
                        f"{m['provider']}:{m['code'] or '?'} ({m['status']})"
                        for m in row["mappings"]
                    ),
                    "stored_matches": row["stored_exact_project_label_matches"],
                    "stored_results": row["stored_exact_project_label_results"],
                    "observed_provider_seasons": json.dumps(
                        row["observed_provider_seasons"], ensure_ascii=False
                    ),
                    "calendar_years_not_seasons": json.dumps(
                        row["observed_calendar_years"], ensure_ascii=False
                    ),
                    "remaining": "; ".join(row["remaining"]),
                }
            )
    lines = [
        "# T61 fixed-archive coverage",
        "",
        f"Archive `{report['archive_commit']}`; all {report['master_rows']} master rows.",
        "",
        report["method"],
        "",
        "Detailed mappings, counts by calendar year, linked provider seasons and input hashes: "
        "coverage.csv / coverage.json.",
        "",
        "| Row | Competition | Status | Stored label matches/results | "
        "Observed source seasons | Remaining |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in report["rows"]:
        seasons = (
            ", ".join(
                f"apif:{s['code']}/{s['season']} ({s['linked_stored_matches']})"
                for s in row["observed_provider_seasons"]
            )
            or "unknown"
        )
        reasons = "; ".join(row["remaining"]).replace("|", "/")
        lines.append(
            f"| {row['master_row_id']} | {row['master_name']} | {row['status']} | "
            f"{row['stored_exact_project_label_matches']}/"
            f"{row['stored_exact_project_label_results']} | {seasons} | {reasons} |"
        )
    (out / "coverage.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
