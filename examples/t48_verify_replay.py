"""Verify a real, hash-pinned T48 FD replay and local archive roundtrip.

This diagnostic writes only below --output and --evidence. It never trains models,
changes counterpart IDs, creates substitute source data, or contacts a service.
Run against a complete restored baseline and an explicit nine-file replay plan.
"""

import argparse
import contextlib
import csv
import hashlib
import json
import shlex
import shutil
import socket
import sqlite3
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

AS_OF = "2026-11-06T17:13:13.835049+00:00"
EXPERIMENT = "forecasts/2026-11-07/42256764906-1/experimental.json"
CORE_TABLES = ("matches", "teams", "results", "odds", "closing_odds", "predictions")
UNRESOLVED = {"jc:2042564", "jc:2042565", "jc:2042593", "jc:2042594", "jc:2042596"}
CELL_KEYS = tuple(
    f"{side}_all_10_shots_on_target_{direction}"
    for side in ("home", "away")
    for direction in ("for", "against")
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def tree_hashes(roots):
    result = {}
    for root in roots:
        root = Path(root).resolve()
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts:
                result[str(path)] = digest(path)
    return result


def relative_tree_hashes(root):
    return {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def connect(database):
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)


def canonical_tables(database, tables=None):
    """Hash SQL originals, with explicit columns and a deterministic row ordering."""
    result = {}
    with connect(database) as db:
        present = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in sorted(tables if tables is not None else present - {"sqlite_sequence"}):
            if table not in present:
                result[table] = {"exists": False, "count": 0, "sha256": None}
                continue
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            hasher, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT {names} FROM "{table}" ORDER BY {names}'):
                hasher.update(
                    (
                        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
                    ).encode()
                )
                count += 1
            result[table] = {
                "exists": True,
                "columns": columns,
                "count": count,
                "sha256": hasher.hexdigest(),
            }
    return result


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def validate_plan(plan):
    document = json.loads(plan.read_text(encoding="utf-8"))
    files = document["files"]
    require(len(files) == 9, "Acceptance requires the exact nine archived originals")
    require({row["label"] for row in files} == {f"S{i}" for i in range(1, 10)}, "S1-S9 missing")
    for item in files:
        path = Path(item["path"])
        if not path.is_absolute():
            path = plan.parent / path
        sidecar = path.with_suffix(path.suffix + ".meta.json")
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        require(digest(path) == item["sha256"] == metadata["sha256"], "Original hash differs")
        require(digest(sidecar) == item["sidecar_sha256"], "Original sidecar hash differs")
        require(metadata["status_code"] == 200, "An empty redirect is not an original CSV")
        require(
            instant(metadata["fetched_at_utc"]) == instant(item["captured_utc"]),
            "Original capture timestamp differs",
        )
        item["resolved_path"] = str(path.resolve())
    return document


class LocalRelease:
    """An immutable filesystem implementation of the production Release contract."""

    def __init__(self, root, old_assets):
        self.root, self.old_assets = root, old_assets
        self.uploads, self.downloads = [], []

    def upload(self, tag, path):
        from zucai.stats_release import descriptor

        want = descriptor(tag, path)
        target = self.root / tag / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(digest(target) == want["sha256"], f"Local asset collision: {path.name}")
        else:
            shutil.copyfile(path, target)
        self.uploads.append(want)
        return want

    def download(self, tag, name, dest):
        from zucai.stats_release import validate_name, validate_tag

        validate_tag(tag)
        validate_name(name)
        source = self.root / tag / name
        if not source.exists():
            source = self.old_assets / name
        require(source.is_file(), f"Fixed local asset missing: {tag}/{name}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        self.downloads.append({"tag": tag, "name": name, "sha256": digest(dest)})
        return dest


def build_features(database, targets):
    from sqlalchemy.orm import Session

    from zucai.db import get_engine
    from zucai.predict.experiments.history import unified_history

    engine = get_engine(database)
    try:
        with Session(engine) as session:
            builder, _ = unified_history(session, provider="fd")
            return {
                target["match_id"]: builder.build(
                    target,
                    as_of=AS_OF,
                    mode="strict",
                    windows=(10,),
                    stat_keys=("shots_on_target",),
                    research=False,
                    history_source="aligned",
                )
                for target in targets
            }
    finally:
        engine.dispose()


def feature_summary(features):
    rows = []
    for match_id, feature in features.items():
        cells = feature["cells"]
        rows.append(
            {
                "match_id": match_id,
                "n": [cells[key]["n"] for key in CELL_KEYS],
                "N": [cells[key]["N"] for key in CELL_KEYS],
                "sample_n": [cells[key]["sample_n"] for key in CELL_KEYS],
                "complete": all(cells[key]["n"] == cells[key]["N"] == 10 for key in CELL_KEYS),
                "missing_history_ids": sorted(
                    {
                        row["match_id"]
                        for cell in cells.values()
                        for row in cell["evidence"]
                        if row["value"] is None
                    }
                ),
                "missing_reasons": sorted(
                    {reason for cell in cells.values() for reason in cell["missing_reasons"]}
                ),
            }
        )
    return rows


def verify_gap_rows(database, history_path, plan, evidence):
    from zucai.sources.fd_ingest import fd_match_id
    from zucai.sources.fd_stats import parse_fd_statistics
    from zucai.sources.football_data import LEAGUES_PATH, read_json

    gaps = [row for row in csv_rows(history_path) if row["raw_sha256"]]
    require(len(gaps) == 210, "Handover must identify 210 histories with original values")
    originals = {row["sha256"]: row for row in plan["files"]}
    original_rows, parsed_rows = {}, {}
    for sha, original in originals.items():
        path = Path(original["resolved_path"])
        original_rows[sha] = dict(enumerate(csv_rows(path), 2))
        parsed, _ = parse_fd_statistics(
            path.read_bytes(), original["div"], original["season"], read_json(LEAGUES_PATH)
        )
        parsed_rows[sha] = {row.row_number: row for row in parsed}
    records, failures = [], []
    with connect(database) as db:
        db.row_factory = sqlite3.Row
        for gap in gaps:
            original = originals[gap["raw_sha256"]]
            raw_row = original_rows[gap["raw_sha256"]][int(gap["raw_row_number"])]
            parsed = parsed_rows[gap["raw_sha256"]][int(gap["raw_row_number"])]
            stored = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM match_stats WHERE provider='fd' AND match_id=? "
                    "AND provider_fixture_id=? AND stat_key='shots_on_target' "
                    "AND raw_sha256=? ORDER BY side,id",
                    (gap["history_id"], gap["fd_id"], gap["raw_sha256"]),
                )
            ]
            match = db.execute(
                "SELECT home_team_id,away_team_id,counterpart_id FROM matches WHERE match_id=?",
                (gap["history_id"],),
            ).fetchone()
            checks = {
                "exactly_two_sides": len(stored) == 2
                and {r["side"] for r in stored} == {"home", "away"},
                "original_row_identity": fd_match_id(parsed) == gap["fd_id"]
                and raw_row["HomeTeam"] == parsed.home_raw
                and raw_row["AwayTeam"] == parsed.away_raw,
                "original_hst_ast": float(raw_row["HST"]) == float(gap["raw_home_sot"])
                and float(raw_row["AST"]) == float(gap["raw_away_sot"]),
            }
            for side, source_key, team_index in (
                ("home", "raw_home_sot", 0),
                ("away", "raw_away_sot", 1),
            ):
                values = [row for row in stored if row["side"] == side]
                raw_column = "HST" if side == "home" else "AST"
                checks[side] = len(values) == 1 and all(
                    (
                        values[0]["value"] == float(gap[source_key]),
                        json.loads(values[0]["raw_value"]) == raw_row[raw_column],
                        values[0]["team_id"] == match[team_index],
                        values[0]["record_key"] == f"{side}:shots_on_target",
                        values[0]["missing_reason"] is None,
                        values[0]["availability_basis"] == "backfilled",
                        values[0]["available_utc"] is None,
                        instant(values[0]["captured_utc"]) == instant(gap["captured_utc"]),
                        values[0]["raw_location"] == original["raw_location"],
                    )
                )
            record = {
                "gap": gap,
                "original_csv_row": raw_row,
                "parsed_provider_fixture_id": fd_match_id(parsed),
                "stored": stored,
                "checks": checks,
                "passed": all(checks.values()),
            }
            records.append(record)
            if not record["passed"]:
                failures.append(gap["history_id"])
    write_json(evidence / "210-history-provenance.json", records)
    with (evidence / "210-history-provenance.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(
            out,
            fieldnames=(
                "history_id",
                "fd_id",
                "passed",
                "stored_rows",
                "raw_sha256",
                "captured_utc",
            ),
        )
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    **{
                        k: row["gap"][k]
                        for k in ("history_id", "fd_id", "raw_sha256", "captured_utc")
                    },
                    "passed": row["passed"],
                    "stored_rows": len(row["stored"]),
                }
            )
    return {
        "verified_histories": len(records) - len(failures),
        "expected_histories": 210,
        "failures": failures,
    }


def link_inventory(database, target_ids):
    with connect(database) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fd_stats_links'"
        ).fetchone()
        rows = (
            [
                dict(row)
                for row in db.execute("SELECT * FROM fd_stats_links ORDER BY provider_fixture_id")
            ]
            if exists
            else []
        )
    return {
        "total": len(rows),
        "target_210": sum(row["match_id"] in target_ids for row in rows),
        "rows": rows,
    }


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


@contextlib.contextmanager
def stage(summary, name):
    started = time.monotonic()
    print(f"START {name}", flush=True)
    try:
        yield
    finally:
        seconds = round(time.monotonic() - started, 3)
        summary.setdefault("stage_timings_seconds", {})[name] = seconds
        print(f"END {name} {seconds}s", flush=True)


def deny_network(*_args, **_kwargs):
    raise RuntimeError("Network is disabled during offline T48 acceptance")


def run(args, summary):
    inputs, output, evidence = args.inputs, args.output, args.evidence
    plan_path = inputs / "selected-raw-manifest.json"
    baseline = inputs / "restored/input.db"
    handover = args.handover_evidence
    with stage(summary, "input_validation"):
        plan = validate_plan(plan_path)
        experiment = json.loads((inputs / "observed" / EXPERIMENT).read_text(encoding="utf-8"))
        require(instant(experiment["as_of_utc"]) == instant(AS_OF), "Frozen feature cutoff differs")
        targets = [
            row
            for row in experiment["predictions"]
            if row["model_name"] == "dc-sot-v1" and row["features"] is not None
        ]
        require(len(targets) == 21, "Frozen batch must contain 21 SoT feature targets")
        summary.update(
            as_of=AS_OF, window=10, targets=len(targets), source_plan_sha256=digest(plan_path)
        )
        protected_roots = (
            [
                inputs / name
                for name in ("input", "observed", "raw", "assets", "archives", "restored", "code")
            ]
            + [plan_path, handover]
            + args.protected
        )
        summary["protected_roots"] = [str(root) for root in protected_roots]
        summary["source_hashes_before"] = tree_hashes(protected_roots)
        write_json(evidence / "source-hashes-before.json", summary["source_hashes_before"])
        summary["implementation_hashes_before"] = tree_hashes([Path(summary["code"])])
        write_json(
            evidence / "implementation-hashes-before.json", summary["implementation_hashes_before"]
        )
        summary["baseline_sha256"] = digest(baseline)
    if args.prepare_only:
        summary["status"] = "prepared_only_no_replay"
        return

    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.fd_stats_replay import replay_fd
    from zucai.stats_archive import row_snapshot
    from zucai.stats_rows import publish_rows, restore_rows
    from zucai.stats_schema import STATS_COLUMNS

    repair = output / "repair/input.db"
    require(
        not repair.parent.exists(),
        "Repair directory already exists; preserve it and use a fresh --output",
    )
    with stage(summary, "copy_baseline"):
        repair.parent.mkdir(parents=True)
        shutil.copyfile(baseline, repair)
        shutil.copytree(baseline.parent / "state", repair.parent / "state")
        for name in ("unmatched_teams.csv", "rejected_results.csv"):
            if (baseline.parent / name).is_file():
                shutil.copyfile(baseline.parent / name, repair.parent / name)
        require(digest(repair) == summary["baseline_sha256"], "Baseline copy differs")
        require(
            relative_tree_hashes(baseline.parent / "state")
            == relative_tree_hashes(repair.parent / "state"),
            "Copied state differs",
        )
        before_core = canonical_tables(repair, CORE_TABLES)
        summary["core_before"] = before_core
        before_links = link_inventory(repair, set())
        write_json(evidence / "core-before.json", before_core)

    with stage(summary, "frozen_features_before"):
        before = build_features(repair, targets)
        write_json(evidence / "features-before.json", before)
        exact = [
            row["match_id"]
            for row in targets
            if before[row["match_id"]]["cells"] == row["features"]["cells"]
        ]
        summary["frozen_before_exact"] = len(exact)
        require(len(exact) == 21, "Baseline does not reproduce every frozen SoT cell")

    with stage(summary, "replay_first_real_nine_files"):
        first = replay_fd(repair, plan_path)
        summary["first_replay"] = first
        write_json(evidence / "replay-first.json", first)
        require(
            first["requests"] == 0 and len(first["files"]) == 9,
            "Replay was not the nine-file offline operation",
        )
        summary["core_after_replay"] = canonical_tables(repair, CORE_TABLES)
        require(
            summary["core_after_replay"] == before_core, "Replay changed a protected core table"
        )

    with stage(summary, "verify_210_original_histories_and_targets"):
        gaps = csv_rows(handover / "history-gaps.csv")
        target_ids = {row["history_id"] for row in gaps if row["raw_sha256"]}
        summary["provenance_210"] = verify_gap_rows(
            repair, handover / "history-gaps.csv", plan, evidence
        )
        after_links = link_inventory(repair, target_ids)
        write_json(evidence / "links-after.json", after_links)
        summary["links"] = {
            "before": before_links["total"],
            "after_all_nine_files": after_links["total"],
            "within_target_210": after_links["target_210"],
        }
        after = build_features(repair, targets)
        write_json(evidence / "features-after.json", after)
        target_summary = feature_summary(after)
        summary["targets_after"] = target_summary
        summary["complete_targets"] = sum(row["complete"] for row in target_summary)
        summary["unresolved_targets"] = [row for row in target_summary if not row["complete"]]
        write_json(evidence / "targets-after.json", target_summary)
        history_unchanged = all(
            before[mid]["cells"][key]["match_ids"] == after[mid]["cells"][key]["match_ids"]
            and before[mid]["cells"][key]["sample_n"] == after[mid]["cells"][key]["sample_n"]
            for mid in before
            for key in CELL_KEYS
        )
        summary["selected_histories_unchanged"] = history_unchanged
        require(history_unchanged, "Replay changed the frozen historical sample lists")
        require(
            not summary["provenance_210"]["failures"],
            "Some original HST/AST provenance checks failed",
        )

    with stage(summary, "idempotent_second_nine_file_replay"):
        first_db_sha, first_state = digest(repair), tree_hashes([repair.parent / "state"])
        first_all_tables = canonical_tables(repair)
        second = replay_fd(repair, plan_path)
        write_json(evidence / "replay-second.json", second)
        summary["second_replay"] = second
        summary["idempotent"] = {
            "inserted_zero": second["inserted"] == 0,
            "database_bytes_unchanged": digest(repair) == first_db_sha,
            "all_tables_unchanged": canonical_tables(repair) == first_all_tables,
            "state_audit_index_files_unchanged": tree_hashes([repair.parent / "state"])
            == first_state,
        }
        require(
            all(summary["idempotent"].values()),
            "Second replay changed the database or audit/index state",
        )

    with stage(summary, "local_publish_rows_with_full_old_snapshot"):
        old_feed = json.loads((inputs / "input/feed_manifest.json").read_text(encoding="utf-8"))
        release = LocalRelease(output / "local-release", inputs / "assets")
        block = publish_rows(
            repair,
            output / "published",
            release=release,
            tag="stats-raw-202611",
            run_id="t48-local-roundtrip",
            previous=old_feed["technical_data"],
        )
        write_json(evidence / "published-technical-manifest.json", block)
        technical_tables = sorted(set(STATS_COLUMNS) | {"fd_stats_links"})
        source_technical = canonical_tables(repair, technical_tables)
        with connect(repair) as db:
            source_snapshot = row_snapshot(db)
        summary["technical_counts_after"] = {
            key: value["count"] for key, value in source_technical.items()
        }

    with stage(summary, "local_export_restore_full_roundtrip"):
        archive, restored = output / "feeds", output / "roundtrip/input.db"
        exported = export_feeds(
            repair,
            archive,
            through=date.fromisoformat(old_feed["through_beijing"]),
            run_id="t48-local-roundtrip",
        )
        restore_feeds(archive, restored)
        restored_counts = restore_rows(restored, archive, release=release)
        summary["roundtrip_restore_rows"] = restored_counts
        summary["database_sha256"] = {"repair": digest(repair), "roundtrip": digest(restored)}
        restored_technical = canonical_tables(restored, technical_tables)
        with connect(restored) as db:
            restored_snapshot = row_snapshot(db)
            foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
            integrity = db.execute("PRAGMA integrity_check").fetchall()
        roundtrip_features = build_features(restored, targets)
        write_json(evidence / "features-roundtrip.json", roundtrip_features)
        write_json(
            evidence / "technical-canonical-roundtrip.json",
            {"source": source_technical, "restored": restored_technical},
        )
        write_json(
            evidence / "technical-snapshot-roundtrip.json",
            {"source": source_snapshot, "restored": restored_snapshot},
        )
        summary["roundtrip"] = {
            "technical_rows_and_links_exact": source_technical == restored_technical,
            "production_snapshot_exact": source_snapshot == restored_snapshot,
            "features_exact": after == roundtrip_features,
            "source_core_unchanged": canonical_tables(repair, CORE_TABLES) == before_core,
            "restored_core_exact": canonical_tables(restored, CORE_TABLES) == before_core,
            "foreign_keys_clean": not foreign_keys,
            "integrity_ok": integrity == [("ok",)],
            "feed_manifest_sha256": digest(archive / "feed_manifest.json"),
            "feed_counts": exported["counts"],
        }
        write_json(
            evidence / "local-release-operations.json",
            {"uploads": release.uploads, "downloads": release.downloads},
        )
        for key, value in summary["roundtrip"].items():
            if isinstance(value, bool):
                require(value, f"Roundtrip failed: {key}")
    require(
        summary["complete_targets"] == 16, "Actual completeness differs from the expected 16/21"
    )
    require(
        {row["match_id"] for row in summary["unresolved_targets"]} == UNRESOLVED,
        "Actual unresolved targets differ from the five documented limitations",
    )
    summary["status"] = "passed"


def main():
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs",
        type=Path,
        required=True,
        help="Fixed workspace containing the restored baseline, snapshots, assets, and replay plan",
    )
    parser.add_argument(
        "--handover-evidence",
        type=Path,
        required=True,
        help="Original handover evidence directory containing history-gaps.csv",
    )
    parser.add_argument(
        "--code", type=Path, required=True, help="Implementation repository or its src directory"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Local-only output root; repair/ must not already exist",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Evidence destination; defaults to OUTPUT/evidence/repair-verification",
    )
    parser.add_argument(
        "--protected",
        type=Path,
        action="append",
        default=[],
        help="Additional frozen forecast/account/scoring path to hash before and after",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate originals and inventory without copying or replaying a database",
    )
    args = parser.parse_args()
    for key in ("inputs", "handover_evidence", "code", "output"):
        setattr(args, key, getattr(args, key).resolve())
    args.evidence = (args.evidence or args.output / "evidence/repair-verification").resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    code_src = args.code / "src" if (args.code / "src/zucai").is_dir() else args.code
    sys.path.insert(0, str(code_src))
    summary = {
        "status": "running",
        "command": shlex.join([sys.executable, *sys.argv]),
        "inputs": str(args.inputs),
        "code": str(code_src),
        "output": str(args.output),
        "network_requests_permitted": 0,
    }
    exit_code = 0
    with (args.evidence / "verification.log").open("a", encoding="utf-8") as log:
        with (
            contextlib.redirect_stdout(Tee(sys.stdout, log)),
            contextlib.redirect_stderr(Tee(sys.stderr, log)),
        ):
            try:
                with (
                    patch.object(socket.socket, "connect", deny_network),
                    patch.object(socket, "create_connection", deny_network),
                ):
                    run(args, summary)
            except Exception as exc:
                summary.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                traceback.print_exc()
                exit_code = 1
            except KeyboardInterrupt:
                summary.update(status="interrupted", error_type="KeyboardInterrupt")
                traceback.print_exc()
                exit_code = 130
            finally:
                if "source_hashes_before" in summary:
                    before = summary.pop("source_hashes_before")
                    after = tree_hashes(summary["protected_roots"])
                    write_json(args.evidence / "source-hashes-after.json", after)
                    changed = sorted(
                        path
                        for path in before.keys() | after.keys()
                        if before.get(path) != after.get(path)
                    )
                    summary["source_artifacts_forecasts_accounts_unchanged"] = not changed
                    summary["changed_protected_paths"] = changed
                    if changed:
                        summary["status"], exit_code = "failed", 1
                if "implementation_hashes_before" in summary:
                    before = summary.pop("implementation_hashes_before")
                    after = tree_hashes([Path(summary["code"])])
                    write_json(args.evidence / "implementation-hashes-after.json", after)
                    changed = sorted(
                        path
                        for path in before.keys() | after.keys()
                        if before.get(path) != after.get(path)
                    )
                    summary["implementation_source_unchanged"] = not changed
                    summary["changed_implementation_paths"] = changed
                    if changed:
                        summary["status"], exit_code = "failed", 1
                write_json(args.evidence / "acceptance-summary.json", summary)
                print(
                    json.dumps(
                        {
                            key: summary.get(key)
                            for key in (
                                "status",
                                "complete_targets",
                                "source_artifacts_forecasts_accounts_unchanged",
                                "error",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
