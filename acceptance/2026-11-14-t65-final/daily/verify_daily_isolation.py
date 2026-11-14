"""Offline T65 acceptance: identical synthetic raw bytes through old and new code.

No HTTP requests, production files or credentials are used. The baseline source
and small configuration files are read from Git into a temporary directory.
"""

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path("/workspace/work/football-t65")
OUT = Path("/workspace/work/t65-final-evidence/daily")
BASE = "b83d2cc7d975e41f418f59aad3341a7d1574f95a"
BEFORE = "5c1384c"
DAY = "2026-11-01"
CAPTURED = "2026-11-03T00:00:00.000000Z"
REVIEW = Path("/workspace/work/t65-evidence/review-followup/claude-review.json")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def git_json(ref):
    return json.loads(subprocess.check_output(["git", "show", f"{ref}:data/league_map.json"], cwd=ROOT))


def approved_codes():
    review = json.loads(REVIEW.read_bytes())["reviews"][0]
    section = re.search(r"联赛 79、.*?继续候选：", review["body"], re.S).group()
    codes = re.findall(r"\d+", section)
    assert len(codes) == len(set(codes)) == 62
    return codes


def verify_routes():
    from zucai.league_registry import daily_codes, provider_entries

    old, before = git_json(BASE), git_json(BEFORE)
    current = json.loads((ROOT / "data/league_map.json").read_bytes())
    approved = set(approved_codes())
    pending = {"40", "62", "71", "850", "916"}
    only_flags = copy.deepcopy(current)
    for code in approved:
        route = current["providers"]["apif"][code]
        assert route["status"] == "verified" and route["backfill"] and not route["daily"]
        assert route["access_status"] == "unverified"
        only_flags["providers"]["apif"][code]["daily"] = True
    assert only_flags == before
    old_objects = 0
    for provider, entries in old["providers"].items():
        for code, entry in entries.items():
            assert canonical(current["providers"][provider][code]) == canonical(entry)
            old_objects += 1
    assert old_objects == 46
    assert current["competitions"][:21] == before["competitions"][:21]
    for row, previous in zip(current["competitions"], before["competitions"], strict=True):
        assert canonical(row["official_catalogue"]) == canonical(previous["official_catalogue"])
    expected_daily = "98 292 188 113 103 244 119 253 169 3 848".split()
    assert daily_codes("apif") == expected_daily
    assert set(provider_entries("apif", purpose="backfill")) == set(expected_daily) | approved
    for code in pending:
        route = current["providers"]["apif"][code]
        assert route["status"] == "candidate" and not route["daily"] and not route["backfill"]
    return {
        "old_provider_objects_byte_equal": old_objects,
        "old_mapping_rows_byte_equal": 21,
        "official_catalogue_objects_byte_equal": 90,
        "registry_change_is_exactly_62_daily_flags": True,
        "daily_codes": expected_daily,
        "daily_import_routes": 11,
        "backfill_import_routes": 73,
        "new_history_only_routes": 62,
        "pending_disabled_routes": sorted(pending, key=int),
        "registry_sha256": digest((ROOT / "data/league_map.json").read_bytes()),
        "review_sha256": digest(REVIEW.read_bytes()),
    }


def inputs():
    fixture = ROOT / "data/fixtures/t14/apif.json"
    clean = json.loads(fixture.read_bytes())
    mixed = copy.deepcopy(clean)
    for index, code in enumerate(approved_codes()):
        row = copy.deepcopy(clean["response"][0])
        row["fixture"].update(id=20000 + index, status={"short": "FT"})
        row["league"]["id"] = int(code)
        row["teams"]["home"]["id"] = 30000 + index * 2
        row["teams"]["away"]["id"] = 30001 + index * 2
        mixed["response"].append(row)
    rejected = copy.deepcopy(mixed["response"][1])
    rejected["fixture"].update(id=29999, status={"short": "AWD"})
    mixed["response"].append(rejected)
    mixed["results"] = len(mixed["response"])
    paths = {}
    for label, doc in (("clean", clean), ("mixed", mixed)):
        path = OUT / "inputs" / label / "api-football" / DAY / "synthetic.json"
        write_json(path, doc)
        write_json(path.with_suffix(".json.meta.json"), {
            "status_code": 200,
            "url": f"https://v3.football.api-sports.io/fixtures?date={DAY}",
            "fetched_at_utc": CAPTURED,
            "sha256": digest(path.read_bytes()),
        })
        paths[label] = path
    write_json(OUT / "inputs/provenance.json", {
        "kind": "synthetic_derived_from_existing_fixture",
        "source_fixture": str(fixture.relative_to(ROOT)),
        "source_fixture_sha256": digest(fixture.read_bytes()),
        "capture_time_is_synthetic": True,
        "capture_time_utc": CAPTURED,
        "new_league_ids_from_review": approved_codes(),
        "mixed_response_rows": len(mixed["response"]),
        "mixed_new_valid_rows": 62,
        "mixed_new_awd_rows": 1,
        "mixed_new_awd_league_id": rejected["league"]["id"],
        "note": "Business-path regression only; not a real API response or season-access proof.",
        "raw_sha256": {label: digest(path.read_bytes()) for label, path in paths.items()},
    })
    return paths


def run_case(raw_path, output, seed_official):
    import requests
    import zucai
    from zucai.cloud_aliases import write_candidates
    from zucai.db import get_engine, init_db
    from zucai.sources.apif_command import fetch_apif

    def reject_network(*args, **kwargs):
        raise AssertionError("Acceptance must never send HTTP requests")

    requests.sessions.Session.request = reject_network
    for key in ("API_FOOTBALL_KEY", "FOOTBALL_DATA_ORG_TOKEN"):
        os.environ.pop(key, None)
    database = output / "data/zucai.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    if seed_official:
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
            for index, league in enumerate(("日职", "德乙"), 1):
                db.execute(
                    "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
                    "kickoff_utc,status) VALUES (?,'jc',?,1,2,?,'scheduled')",
                    (f"jc:{index}", league, "2026-11-01T11:00:00+00:00"),
                )
    result = fetch_apif(
        date=DAY, raw_file=raw_path, db=database, audit_dir=output / "audit",
        raw_root=raw_path.parents[2], quota_path=output / "quota.json",
    )
    count = write_candidates("apif", database, raw_path.parents[2], result["files"], output / "candidates")
    with sqlite3.connect(database) as db:
        tables = [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        snapshot = {name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for name in tables}
    write_json(output / "tables.json", snapshot)
    write_json(output / "result.json", result)
    write_json(output / "execution.json", {
        "module": str(Path(zucai.__file__).resolve()), "requests": result["requests"],
        "candidate_count": count, "seed_official": seed_official,
        "raw_sha256": digest(raw_path.read_bytes()),
    })


def content_files(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def compare_same_raw(old, new):
    tables_old = (old / "tables.json").read_bytes()
    assert tables_old == (new / "tables.json").read_bytes()
    assert (old / "result.json").read_bytes() == (new / "result.json").read_bytes()
    assert content_files(old / "audit") == content_files(new / "audit")
    assert content_files(old / "candidates") == content_files(new / "candidates")
    db = json.loads(tables_old)
    result = json.loads((new / "result.json").read_bytes())
    candidates = json.loads((new / "candidates/apif.json").read_bytes())
    return {
        "all_database_tables_equal": len(db),
        "database_snapshot_sha256": digest(tables_old),
        "result_byte_equal": True,
        "inserted": result["inserted"],
        "rejected_rows": result["rejected_rows"],
        "suspected_duplicates": result["suspected_duplicates"],
        "audit_files_byte_equal": sorted(content_files(new / "audit")),
        "audit_sha256": {name: digest(raw) for name, raw in content_files(new / "audit").items()},
        "candidate_file_byte_equal": True,
        "candidate_sha256": digest((new / "candidates/apif.json").read_bytes()),
        "candidate_count": sum(len(entries) for entries in candidates.values()),
    }


def main():
    routes = verify_routes()
    paths = inputs()
    selected = ["src", "data/league_map.json", "data/api_football", "data/team_aliases",
                "data/football_data_org", "data/football_data_co_uk"]
    archive = subprocess.check_output(["git", "archive", BASE, *selected], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="baseline-", dir=OUT) as temporary:
        baseline = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(baseline, filter="data")
        for label, source in (("old-main", baseline / "src"), ("current", ROOT / "src")):
            for case, official in (("import", False), ("candidate", True)):
                for raw_name in ("mixed", "clean"):
                    destination = OUT / "runs" / f"{label}-{case}-{raw_name}"
                    assert not destination.exists(), f"Refusing to reuse an evidence run: {destination}"
                    command = [sys.executable, str(Path(__file__).resolve()), "--run-case",
                               str(paths[raw_name]), "--output", str(destination)]
                    if official:
                        command.append("--seed-official")
                    env = {**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
                    subprocess.run(command, env=env, cwd=source.parent, check=True)
    comparisons = {}
    for case in ("import", "candidate"):
        comparisons[case] = compare_same_raw(OUT / "runs" / f"old-main-{case}-mixed",
                                            OUT / "runs" / f"current-{case}-mixed")
    assert comparisons["import"]["inserted"] == 1
    assert comparisons["import"]["rejected_rows"] == comparisons["import"]["suspected_duplicates"] == 0
    assert comparisons["candidate"]["candidate_count"] == 2
    # Clean versus mixed: their raw digests differ by design. Database snapshots
    # and unmatched audit bytes remain exact. Full candidate proof bytes are only
    # compared across versions reading the same mixed original, never rewritten.
    clean = OUT / "runs/current-import-clean"
    mixed = OUT / "runs/current-import-mixed"
    assert (clean / "tables.json").read_bytes() == (mixed / "tables.json").read_bytes()
    assert content_files(clean / "audit") == content_files(mixed / "audit")
    summary = {
        "result": "passed", "baseline_commit": BASE, "pre_revision_head": BEFORE,
        "input_kind": "synthetic_from_data/fixtures/t14/apif.json",
        "requests": 0, "production_database_changes": False,
        "route_validation": routes, "same_mixed_raw_old_main_vs_current": comparisons,
        "clean_vs_mixed_database_and_import_audit_bytes_equal": True,
        "provenance_rule": "Different raw inputs retain different real SHA256 values; same-input old/new candidates and audits are byte-equal.",
        "limits": "Does not prove live API access, fixture-season availability, T68 team binding, or successful production backfill.",
    }
    write_json(OUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-case", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-official", action="store_true")
    args = parser.parse_args()
    if args.run_case:
        run_case(args.run_case, args.output, args.seed_official)
    else:
        main()
