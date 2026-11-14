"""Reverify saved normalized records on a new copy; never an HTTP response replay."""

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import responses

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.source_result_reconcile import reconcile_source_results
from zucai.source_results_audit import audit_source_results

PROOF_TABLES = {"result_source_links", "result_source_identities"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def logical_tables(path):
    result = {}
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if name.startswith("sqlite_"):
                continue
            fields = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
            digest = hashlib.sha256()
            count = 0
            columns = ",".join(f'"{field}"' for field in fields)
            for row in db.execute(f'SELECT {columns} FROM "{name}" ORDER BY {columns}'):
                digest.update((encode(row) + "\n").encode())
                count += 1
            result[name] = {"rows": count, "columns": fields, "sha256": digest.hexdigest()}
    return result


def manifest_files(archive):
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    return {key: sha(archive / key) for key in sorted(manifest["files"])}


def run(args):
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    archive = Path(args.archive)
    original_files = manifest_files(archive)
    database = output / "working" / "feeds.db"
    report = {
        "kind": "saved_normalized_records_reverification_not_http_response_replay",
        "source_commit": "e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4",
        "network_requests": 0,
        "production_key_uses": 0,
        "started_utc": datetime.now(UTC).isoformat(),
        "input_manifest_sha256": sha(archive / "feed_manifest.json"),
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False):
        restore_feeds(archive, database)
        before = logical_tables(database)
        write(output / "tables-before.json", before)
        report["restored_database_sha256"] = sha(database)
        observed = datetime.now(UTC)
        report["verification_utc"] = observed.isoformat()
        report["verification_beijing"] = observed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
        report["first_pass"] = reconcile_source_results(database, observed_utc=observed)
        after = logical_tables(database)
        write(output / "tables-after.json", after)
        report["changed_tables"] = [name for name in before if before[name] != after[name]]
        assert set(report["changed_tables"]) <= PROOF_TABLES
        report["old_tables_unchanged"] = True
        report["second_pass"] = reconcile_source_results(database, observed_utc=datetime.now(UTC))
        assert logical_tables(database) == after
        report["second_pass_logical_tables_unchanged"] = True
        before_audit = sha(database)
        audit_path = database.parent / "state/audit/apif_results_crosscheck.csv"
        report["audit"] = audit_source_results(database, audit_path)
        assert before_audit == sha(database)
        report["audit_database_unchanged"] = True
        report["audit_csv_sha256"] = sha(audit_path)
        exported = output / "exported"
        now = datetime.now(UTC)
        manifest = export_feeds(database, exported, now.astimezone(ZoneInfo("Asia/Shanghai")).date(), "t63-offline-normalized-reverification")
        report["export_manifest_sha256"] = sha(exported / "feed_manifest.json")
        restored = output / "roundtrip" / "feeds.db"
        restore_feeds(exported, restored)
        roundtrip = logical_tables(restored)
        write(output / "tables-roundtrip.json", roundtrip)
        assert roundtrip == after
        report["export_restore_all_tables_equal"] = True
        for name in PROOF_TABLES:
            report[name] = after[name]
        report["export_counts"] = manifest["counts"]
        report["original_archive_unchanged"] = original_files == manifest_files(archive)
        assert report["original_archive_unchanged"]
        report["post_verification_fit_cutoff_utc"] = datetime.now(UTC).isoformat()
        report["completed_utc"] = datetime.now(UTC).isoformat()
        write(output / "summary.json", report)
    print(encode(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--out", required=True)
    run(parser.parse_args())
