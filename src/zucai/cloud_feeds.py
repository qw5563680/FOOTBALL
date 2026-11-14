"""Persist four football feeds as portable, versioned tables in the data branch."""

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from requests import RequestException

from zucai.db import get_engine, init_db
from zucai.fd_collection import FD_COLLECTION_FILE, read_fd_success
from zucai.fd_stats_archive import export_fd_links, restore_fd_links, validate_fd_links_manifest
from zucai.fd_stats_links import FD_LINK_FILE
from zucai.result_source_identity import (
    RESULT_IDENTITY_FILE,
    export_result_identities,
    restore_result_identities,
    validate_result_identities_manifest,
)
from zucai.result_source_links import (
    RESULT_LINK_FILE,
    export_result_links,
    restore_result_links,
    validate_result_links_manifest,
)
from zucai.sfc_state import export_sfc_state, restore_sfc_state
from zucai.sources.results import ResultRowsRejected, fetch_results
from zucai.sources.sporttery import BEIJING, fetch_schedule
from zucai.stats_archive import (
    INDEX_FILE,
    ROWS_MANIFEST,
    STATS_FILES,
    check_stats,
    export_stats,
    restore_stats,
    validate_stats_manifest,
    write_rows_manifest,
)

LOG = logging.getLogger(__name__)
TABLES = {
    "teams": ("id", "name", "aliases"),
    "matches": (
        "match_id",
        "source",
        "league",
        "home_team_id",
        "away_team_id",
        "kickoff_utc",
        "jc_num",
        "status",
        "result_checked_utc",
        "result_provider",
        "counterpart_id",
    ),
    "results": (
        "match_id",
        "home_goals",
        "away_goals",
        "ht_home_goals",
        "ht_away_goals",
        "available_utc",
        "available_source",
    ),
    "odds": ("id", "match_id", "pool", "line", "selection", "price", "snapshot_utc"),
    "closing_odds": ("match_id", "bookmaker", "pool", "h", "d", "a", "basis"),
}
MANIFEST = "feed_manifest.json"
MONTH = r"[0-9]{4}-(?:0[1-9]|1[0-2])"
CANDIDATE_REVIEW_DIR = "state/audit/jc_candidate_reviews"
CANDIDATE_REVIEW_RUN_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
CANDIDATE_REVIEW_FILE = (
    rf"{CANDIDATE_REVIEW_DIR}/{CANDIDATE_REVIEW_RUN_ID}/"
    r"(?:plan|identity_inventory|review|operation)\.json"
)
CATALOGUE_RUN_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
CATALOGUE_DATE = rf"{MONTH}-(?:0[1-9]|[12][0-9]|3[01])"
CATALOGUE_FILE = (
    rf"state/catalogue/(?:(?:latest|backfill-checkpoint)\.json|{CATALOGUE_RUN_ID}/(?:"
    r"(?:apif(?:-raw|-meta)?|fdorg(?:-raw|-meta)?|fd|jc(?:-leagues|-kickoff-plan|-kickoffs)?|summary|backfill)\.json|"
    r"jc-heads/(?:[1-9][0-9]*|conflicts/[1-9][0-9]*-[0-9a-f]{64})\.json(?:\.meta\.json)?|"
    r"jc-schedules/[0-9a-f]{64}\.json(?:\.meta\.json)?|"
    r"jc-db-(?:aliases\.json|(?:matches|teams)\.jsonl|manifests/[0-9a-f]{64}\.json)|"
    rf"jc/{MONTH}/{CATALOGUE_DATE}-p[1-9][0-9]*\.json(?:\.meta\.json)?|"
    rf"backfill/(?:api-football|football-data-org|football-data-co-uk)/{CATALOGUE_DATE}/"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*_[0-9]{12}Z_[0-9a-f]{32}\.(?:json|csv|txt)(?:\.meta\.json)?))"
)
STATE_FILE = (
    r"state/(?:(?:api_football_quota|nations_league|fd_collection)\.json|stats_(?:rows_manifest|coverage|backfill_cursor)\.json|"
    rf"audit/jc_(?:catalog|skipped)/{MONTH}\.csv|"
    r"audit/(?:jc|fd|fdorg|apif|sfc|stats|injuries)_[a-z_]+\.csv|"
    r"alias_candidates/(?:fdorg|apif|jc)\.json|publication/(?:forecast|sfc)_history\.json)"
    rf"|{CANDIDATE_REVIEW_FILE}|{CATALOGUE_FILE}"
)
VIEW = """
WITH triples AS (
 SELECT o.match_id,o.snapshot_utc,MAX(CASE WHEN selection='h' THEN price END) AS h,
   MAX(CASE WHEN selection='d' THEN price END) AS d,MAX(CASE WHEN selection='a' THEN price END) AS a
 FROM odds o JOIN matches m ON m.match_id=o.match_id
 WHERE o.pool='had' AND o.line=0 AND o.selection IN ('h','d','a') AND o.snapshot_utc<m.kickoff_utc
 GROUP BY o.match_id,o.snapshot_utc HAVING COUNT(DISTINCT o.selection)=3
), ranked AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY snapshot_utc DESC) AS n FROM triples
)
SELECT m.match_id,m.jc_num,m.league,m.kickoff_utc,
 datetime(m.kickoff_utc,'+8 hours') AS kickoff_beijing,m.source,
 h.name AS home,a.name AS away,m.status,r.home_goals,r.away_goals,r.ht_home_goals,r.ht_away_goals,
 r.available_utc,r.available_source,m.result_provider,
 q.h AS had_home,q.d AS had_draw,q.a AS had_away,q.snapshot_utc AS had_snapshot_utc
FROM matches m JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id
LEFT JOIN results r ON m.match_id=r.match_id LEFT JOIN ranked q ON m.match_id=q.match_id AND q.n=1
ORDER BY m.kickoff_utc,m.match_id
"""


def digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _candidate_review_files(root: Path) -> list[Path]:
    """Find only owned review evidence, rejecting symlinks before any state changes."""
    folder = root
    for part in CANDIDATE_REVIEW_DIR.split("/"):
        folder /= part
        if folder.is_symlink():
            raise ValueError("竞彩候选核验审计不允许符号链接")
        if not folder.exists():
            return []
        if not folder.is_dir():
            raise ValueError("竞彩候选核验审计目录无效")
    files = []
    for run in sorted(folder.iterdir()):
        if not re.fullmatch(CANDIDATE_REVIEW_RUN_ID, run.name):
            continue
        if run.is_symlink():
            raise ValueError("竞彩候选核验审计不允许符号链接")
        if not run.is_dir():
            raise ValueError("竞彩候选核验审计目录无效")
        for path in sorted(run.iterdir()):
            if not re.fullmatch(CANDIDATE_REVIEW_FILE, path.relative_to(root).as_posix()):
                continue
            if path.is_symlink():
                raise ValueError("竞彩候选核验审计不允许符号链接")
            if not path.is_file():
                raise ValueError("竞彩候选核验审计文件无效")
            files.append(path)
    return files


def _catalogue_files(root: Path) -> list[Path]:
    """Reject symlinks and list all owned state, including unexported runtime aliases."""
    folder = root
    for part in ("state", "catalogue"):
        folder /= part
        if folder.is_symlink():
            raise ValueError("赛事目录不允许符号链接")
        if not folder.exists():
            return []
        if not folder.is_dir():
            raise ValueError("赛事目录路径无效")
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("赛事目录不允许符号链接")
        if path.is_file():
            files.append(path)
        elif re.fullmatch(CATALOGUE_FILE, path.relative_to(root).as_posix()):
            raise ValueError("赛事目录证据文件无效")
    return files


def dump_rows(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n")


def source_inventory(db: sqlite3.Connection) -> dict:
    """Hash ordered canonical rows, attributing shared closing prices to their match owner."""
    inventory = {}
    sources = {"jc", "fd", "fdorg", "apif"}
    sources.update(row[0] for row in db.execute("SELECT DISTINCT source FROM matches"))
    for source in sorted(sources):
        counts, hashes = {}, {}
        for table, cols in TABLES.items():
            if table == "teams":  # Shared identities are covered by the file-level hash.
                continue
            names = ",".join(f"t.{c}" for c in cols)
            join = "" if table == "matches" else " JOIN matches m USING(match_id)"
            owner = "t" if table == "matches" else "m"
            rows = db.execute(
                f"SELECT {names} FROM {table} t{join} WHERE {owner}.source=? ORDER BY {names}",
                (source,),
            )
            count, sha = 0, hashlib.sha256()
            for row in rows:
                sha.update(
                    (
                        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
                    ).encode()
                )
                count += 1
            counts[table], hashes[table] = count, sha.hexdigest()
        inventory[source] = {"counts": counts, "sha256": hashes}
    return inventory


def check_db(db: sqlite3.Connection) -> None:
    if (
        db.execute("PRAGMA quick_check").fetchone() != ("ok",)
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("数据库结构或外键校验失败")
    if db.execute("""SELECT 1 FROM matches m LEFT JOIN results r USING(match_id)
                  WHERE (m.status='finished') <> (r.match_id IS NOT NULL) LIMIT 1""").fetchone():
        raise ValueError("完赛状态与比分表不一致")
    from zucai.sources.fd_apif_counterpart import validate_apif_fd_links

    validate_apif_fd_links(db)
    check_stats(db, rows=False)


def pending_results(source: Path) -> list[dict[str, str]]:
    """Keep a rejection pending until a strictly later, accepted same-source observation."""
    audit = source.parent / "rejected_results.csv"
    if not audit.is_file():
        return []
    latest = {}
    with audit.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["date", "match_id", "observed_utc", "reason"]:
            raise ValueError("拒绝清单字段不完整")
        for row in reader:
            key = (date.fromisoformat(row["date"]), row["match_id"])
            observed = datetime.fromisoformat(row["observed_utc"])
            if observed.tzinfo is None:
                raise ValueError("拒绝记录缺少采集时区")
            if key not in latest or observed > latest[key][0]:
                latest[key] = (observed, row)
    pending = []
    with closing(sqlite3.connect(source)) as db:
        for (_, match_id), (observed, row) in sorted(latest.items()):
            accepted = db.execute(
                "SELECT status,result_provider,result_checked_utc FROM matches WHERE match_id=?",
                (match_id,),
            ).fetchone()
            if (
                accepted
                and accepted[0] in {"finished", "cancelled"}
                and accepted[1] == match_id.split(":")[0]
                and accepted[2]
                and datetime.fromisoformat(accepted[2]) > observed
            ):
                continue
            pending.append(row)
    return pending


def export_feeds(
    source: Path, archive: Path, through: date, run_id: str, *, collection=None
) -> dict:
    if not source.is_file():
        raise ValueError("采集数据库缺失")
    if (source.parent / FD_COLLECTION_FILE).exists():
        read_fd_success(source.parent / FD_COLLECTION_FILE, latest=datetime.now(UTC))
    _candidate_review_files(source.parent)
    _candidate_review_files(archive)
    _catalogue_files(source.parent)
    _catalogue_files(archive)
    pending = pending_results(source)
    archive.mkdir(parents=True, exist_ok=True)
    previous = set(archive_info(archive)["files"]) if (archive / MANIFEST).is_file() else set()
    files = []
    totals = {}
    with sqlite3.connect(source) as db:
        db.execute("BEGIN")
        check_db(db)
        for table, cols in TABLES.items():
            totals[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            query = f"SELECT {','.join(cols)} FROM {table}"
            if table == "odds":
                rows = db.execute(query + " ORDER BY substr(snapshot_utc,1,7),id")
                for month, group in groupby(rows, key=lambda r: r[-1][:7]):
                    if not re.fullmatch(MONTH, month):
                        raise ValueError("无效的赔率月份")
                    key = f"state/odds/{month}.jsonl"
                    dump_rows(archive / key, group)
                    files.append(key)
            else:
                key = f"state/{table}.jsonl"
                dump_rows(archive / key, db.execute(query + f" ORDER BY {','.join(cols)}"))
                files.append(key)
        files.extend(export_sfc_state(db, archive))
        fd_links = export_fd_links(db, archive)
        if fd_links is not None:
            files.append(FD_LINK_FILE)
        result_links = export_result_links(db, archive)
        if result_links is not None:
            files.append(RESULT_LINK_FILE)
        result_identities = export_result_identities(db, archive)
        if result_identities is not None:
            files.append(RESULT_IDENTITY_FILE)
        technical_data = export_stats(db, archive, source)
        files.extend((INDEX_FILE, ROWS_MANIFEST))
        sources = source_inventory(db)
        data = db.execute(VIEW)
        for month, group in groupby(data, key=lambda r: r[3][:7]):
            if not re.fullmatch(MONTH, month):
                raise ValueError("无效的比赛月份")
            key = f"matches/{month}.csv"
            out = archive / key
            out.parent.mkdir(exist_ok=True)
            with out.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                writer.writerow(c[0] for c in data.description)
                writer.writerows(group)
            files.append(key)
    for name in ("unmatched_teams.csv", "rejected_results.csv"):
        audit = source.parent / name
        if audit.is_file():
            key = f"state/{name}"
            (archive / key).write_bytes(audit.read_bytes())
            files.append(key)
            if name == "rejected_results.csv":
                destination = source.parent / "state/audit/jc_rejected_results.csv"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(audit, destination)
            else:
                with audit.open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    records = list(reader)
                    for provider in ("jc", "fd", "fdorg", "apif"):
                        rows = [row for row in records if row.get("source") == provider]
                        if not rows:
                            continue
                        destination = source.parent / f"state/audit/{provider}_unmatched_teams.csv"
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("w", encoding="utf-8", newline="") as out:
                            writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
                            writer.writeheader()
                            writer.writerows(rows)
    from zucai.publication_history import HISTORY_FILES, encode_history, read_history

    # A scoreboard/digest may have saved proof after this working DB was restored.
    # Preserve archive entries first; a stale exporter may only add unseen paths.
    for key in HISTORY_FILES:
        current, working = read_history(archive, key), read_history(source.parent, key)
        merged = {**working, **current}
        if merged or (archive / key).exists() or (source.parent / key).exists():
            target = archive / key
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = encode_history(merged)
            if not target.exists() or target.read_bytes() != raw:
                from zucai.sources.base import _atomic_write

                _atomic_write(target, raw)
            files.append(key)
    state = source.parent / "state"
    for path in sorted(state.rglob("*")) if state.exists() else []:
        key = "state/" + path.relative_to(state).as_posix()
        if (
            path.is_file()
            and key not in (ROWS_MANIFEST, *HISTORY_FILES)
            and re.fullmatch(STATE_FILE, key)
        ):
            if path.is_symlink():
                raise ValueError("持久状态不允许符号链接")
            (archive / key).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, archive / key)
            files.append(key)
    # Only remove obsolete files owned and checked by the previous archive.
    for key in previous.difference(files):
        target = (archive / key).resolve()
        if not target.is_relative_to(archive.resolve()):
            raise ValueError("旧档路径超出归档目录")
        target.unlink()
    manifest = {
        "version": 2,
        "columns": {name: list(cols) for name, cols in TABLES.items()},
        "through_beijing": through.isoformat(),
        "run": run_id,
        "event_name": os.environ.get("GITHUB_EVENT_NAME") or "local",
        "operation": os.environ.get("ZUCAI_OPERATION") or None,
        "created_utc": datetime.now(UTC).isoformat(),
        "counts": totals,
        "technical_data": technical_data,
        "sources": sources,
        "collection": collection or {},
        "collection_status": "partial"
        if pending or any(item.get("status") == "partial" for item in (collection or {}).values())
        else "complete",
        "pending_result_count": len(pending),
        "pending_result_dates": sorted({row["date"] for row in pending}),
        "files": {k: digest(archive / k) for k in sorted(files)},
    }
    if fd_links is not None:
        manifest["fd_stats_links"] = fd_links
    if result_links is not None:
        manifest["result_source_links"] = result_links
    if result_identities is not None:
        manifest["result_source_identities"] = result_identities
    (archive / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def archive_info(archive: Path) -> dict:
    meta = json.loads((archive / MANIFEST).read_text(encoding="utf-8"))
    if meta["version"] not in (1, 2):
        raise ValueError("不支持的云端归档版本")
    _candidate_review_files(archive)
    _catalogue_files(archive)
    for key, want in meta["files"].items():
        if key not in STATS_FILES | {
            FD_LINK_FILE,
            RESULT_LINK_FILE,
            RESULT_IDENTITY_FILE,
        } and not re.fullmatch(
            rf"state/(?:teams|matches|results|closing_odds)\.jsonl|state/(?:unmatched_teams|rejected_results)\.csv|state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv|state/sfc/[0-9]{{5}}\.json|{STATE_FILE}",
            key,
        ):
            raise ValueError(f"未知归档文件 {key}")
        target = archive / key
        if (
            not target.resolve().is_relative_to(archive.resolve())
            or target.is_symlink()
            or not target.is_file()
            or digest(target) != want
        ):
            raise ValueError(f"云端数据校验失败 {key}")
    for key in ("state/teams.jsonl", "state/matches.jsonl", "state/results.jsonl"):
        if key not in meta["files"]:
            raise ValueError(f"云端数据校验失败：缺少 {key}")
    if meta["version"] == 2 and "state/closing_odds.jsonl" not in meta["files"]:
        raise ValueError("云端数据校验失败：缺少 closing_odds")
    validate_stats_manifest(meta)
    validate_fd_links_manifest(meta)
    validate_result_links_manifest(meta)
    validate_result_identities_manifest(meta)

    if FD_COLLECTION_FILE in meta["files"]:
        read_fd_success(archive / FD_COLLECTION_FILE, latest=meta.get("created_utc"))
    return meta


def restore_feeds(archive: Path, dest: Path) -> dict:
    meta = archive_info(archive)
    previous_reviews = _candidate_review_files(dest.parent)
    previous_catalogues = _catalogue_files(dest.parent)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=dest.parent) as temporary:
        dbfile = Path(temporary) / "feeds.db"
        engine = get_engine(dbfile)
        try:
            init_db(engine)
        finally:
            engine.dispose()
        with sqlite3.connect(dbfile) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for table, cols in TABLES.items():
                if table == "closing_odds" and meta["version"] == 1 and table not in meta["counts"]:
                    continue
                legacy = tuple(c for c in cols if c != "counterpart_id")
                stored = tuple(meta.get("columns", {}).get(table, legacy))
                if stored not in (cols, legacy):
                    raise ValueError(f"{table} 字段清单校验失败")
                names = (
                    [k for k in sorted(meta["files"]) if k.startswith("state/odds/")]
                    if table == "odds"
                    else [f"state/{table}.jsonl"]
                )
                for key in names:
                    with (archive / key).open(encoding="utf-8") as f:
                        db.executemany(
                            f"INSERT INTO {table} ({','.join(stored)}) "
                            f"VALUES ({','.join('?' for _ in stored)})",
                            (json.loads(line) for line in f),
                        )
                count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count != meta["counts"][table]:
                    raise ValueError(f"{table} 行数校验失败")
            restore_sfc_state(db, archive, meta["files"])
            restore_result_links(db, archive, meta)
            restore_fd_links(db, archive, meta)
            restore_result_identities(db, archive, meta)
            restore_stats(db, archive, meta)
            check_db(db)
            if meta["version"] == 2 and source_inventory(db) != meta.get("sources"):
                raise ValueError("各来源行数或哈希校验失败")
        db.close()
        os.replace(dbfile, dest)
        block = meta.get("technical_data")
        if block and block.get("version") in (2, 3):
            write_rows_manifest(dest, block)
        else:
            (dest.parent / ROWS_MANIFEST).unlink(missing_ok=True)
        # Catalog evidence, technical receipts, cursor and coverage belong to this snapshot.
        # A reused destination must not retain evidence from a later run.
        for key in (
            RESULT_LINK_FILE,
            RESULT_IDENTITY_FILE,
            "state/audit/apif_results_crosscheck.csv",
            "state/audit/jc_catalog.csv",
            "state/audit/jc_skipped.csv",
            "state/alias_candidates/jc.json",
            "state/audit/jc_identity_merges.csv",
            "state/audit/stats_ingest_audit.csv",
            "state/audit/injuries_ingest_audit.csv",
            "state/audit/stats_link_audit.csv",
            "state/stats_backfill_cursor.json",
            "state/stats_coverage.json",
            "state/nations_league.json",
            FD_COLLECTION_FILE,
            "state/publication/forecast_history.json",
            "state/publication/sfc_history.json",
        ):
            if key not in meta["files"]:
                (dest.parent / key).unlink(missing_ok=True)
        for name in ("jc_catalog", "jc_skipped"):
            folder = dest.parent / "state/audit" / name
            for path in folder.glob("*.csv"):
                if not path.is_file() or not re.fullmatch(rf"{MONTH}\.csv", path.name):
                    continue
                if not path.resolve().is_relative_to(dest.parent.resolve()):
                    raise ValueError("竞彩审计分片路径超出恢复目录")
                if path.relative_to(dest.parent).as_posix() not in meta["files"]:
                    path.unlink()
        for path in (*previous_reviews, *previous_catalogues):
            if path.relative_to(dest.parent).as_posix() not in meta["files"]:
                path.unlink()
        for name in ("unmatched_teams.csv", "rejected_results.csv"):
            if f"state/{name}" in meta["files"]:
                (dest.parent / name).write_bytes((archive / f"state/{name}").read_bytes())
        for key in meta["files"]:
            if key != ROWS_MANIFEST and re.fullmatch(STATE_FILE, key):
                target = dest.parent / key
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive / key, target)
    return meta


def date_windows(database: Path, previous: date, today: date, days: int) -> list[tuple[date, date]]:
    if days < 1 or days > 366 or previous > today:
        raise ValueError("云端回看范围无效")
    start = min(today - timedelta(days=days - 1), previous - timedelta(days=1))
    pending_dates = {date.fromisoformat(row["date"]) for row in pending_results(database)}
    if any(day > today for day in pending_dates):
        raise ValueError("拒绝记录日期超过本次同步截止")
    with sqlite3.connect(database) as db:
        extra = {
            date.fromisoformat(row[0])
            for row in db.execute(
                "SELECT DISTINCT date(kickoff_utc,'+8 hours') FROM matches "
                "WHERE source='jc' AND status='scheduled' "
                "AND date(kickoff_utc,'+8 hours') < ?",
                (start.isoformat(),),
            )
        }
    extra.update(day for day in pending_dates if day < start)
    return [(d, d) for d in sorted(extra)] + [(start, today)]


def collect_jc(database: Path, previous: dict, today: date, days: int) -> tuple[dict, bool]:
    """Publish independent stages without turning a failed attempt into a success time."""
    prior = previous.get("collection", {}).get("jc", {})
    result = dict(
        status="complete",
        schedule_status="complete",
        results_status="complete",
        schedule_updated_utc=prior.get("schedule_updated_utc"),
        results_updated_utc=prior.get("results_updated_utc"),
        schedule_matches=0,
        odds_snapshots=0,
        result_windows=0,
        result_windows_completed=0,
        inserted=None,
        linked_existing=0,
        rejected_rows=0,
        requests=None,
        errors=[],
        failed_stages=[],
    )
    metadata = {}
    try:
        result["schedule_matches"], result["odds_snapshots"] = fetch_schedule(metadata=metadata)
        result["schedule_updated_utc"] = metadata["captured_utc"]
    except (RequestException, ValueError, KeyError, TypeError) as exc:
        result["schedule_status"] = "failed"
        result["failed_stages"].append("schedule")
        result["errors"].append(f"赛程更新失败：{type(exc).__name__}")
        LOG.warning("JC schedule collection failed (%s)", type(exc).__name__)

    windows_complete = True
    rows_rejected = False
    try:
        windows = date_windows(
            database, date.fromisoformat(previous["through_beijing"]), today, days
        )
        result["result_windows"] = len(windows)
        for i, (start, end) in enumerate(windows):
            if i:
                time.sleep(1)
            try:
                fetch_results(start, end)
            except ResultRowsRejected as exc:
                # All pages were read; per-row rejects keep their existing retry audit.
                rows_rejected = True
                LOG.warning("单场拒绝，继续处理其他窗口：%s", exc)
            result["result_windows_completed"] += 1
    except (RequestException, ValueError, KeyError, TypeError) as exc:
        windows_complete = False
        result["results_status"] = "failed"
        result["failed_stages"].append("results")
        result["errors"].append(f"赛果更新失败：{type(exc).__name__}")
        LOG.warning("JC results collection failed (%s)", type(exc).__name__)
    pending = pending_results(database)
    result["rejected_rows"] = len(pending)
    if pending or rows_rejected:
        if result["results_status"] != "failed":
            result["results_status"] = "partial"
        result["errors"].append(f"仍有 {len(pending)} 场竞彩赛果待补抓")
    if result["results_status"] == "complete":
        result["results_updated_utc"] = datetime.now(UTC).isoformat()
    if result["schedule_status"] != "complete" or result["results_status"] != "complete":
        result["status"] = "partial"
    # A failed date page is absent from the row-rejection audit. Keep the previous
    # date watermark so next sync retries every missing day, including unseen IDs.
    return result, windows_complete


def write_apif_quota(summary, result):
    plan = html.escape(str(result.get("plan") or "未知（待 probe）"))
    fallback = "（回退）" if result.get("quota_fallback") else ""
    summary.write(
        f"\nAPI-Football plan: {plan}；limit_day: {result.get('limit_day', '未知')}{fallback}；"
        f"本地日上限: {result.get('local_daily_cap', '未知')}；"
        f"另计 /status 请求: {result.get('status_requests', 0)}。\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("export", "sync", "apis", "verify", "backfill", "probe", "identity", "reconcile"),
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/zucai.db"))
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--slot", choices=("auto", "early", "late"), default="auto")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--leagues", help="backfill 的来源赛事代码；probe 的 apif 联赛 ID")
    parser.add_argument(
        "--source", choices=("fd", "fdorg", "apif"), help="backfill 默认 fd；probe 仅 apif"
    )
    parser.add_argument("--seasons", help="probe 的年份；identity 默认 2025,2026")
    parser.add_argument(
        "--probe-kind",
        choices=("seasons", "leagues", "stats", "catalogue", "jc-catalogue", "jc-kickoffs"),
        help="仅 probe：赛季权限、桥接元数据或技术数据",
    )
    parser.add_argument("--per-scope", type=int, help="仅 stats probe：每联赛季最多抽样场数")
    parser.add_argument("--from", dest="first", help="仅 backfill：起始年或赛季，如 2021 / 2122")
    parser.add_argument("--to", dest="last", help="仅 backfill：终止年或赛季，如 2026 / 2627")
    parser.add_argument(
        "--catalogue", action="store_true", help="仅 backfill：按已核验目录赛季回填"
    )
    parser.add_argument(
        "--max-requests", type=int, help="仅 catalogue backfill / jc-kickoffs probe：本批请求上限"
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="仅 catalogue backfill：继续已归档游标（默认开启）",
    )
    relink = parser.add_mutually_exclusive_group()
    relink.add_argument(
        "--relink-fdorg",
        action="store_true",
        help="仅 Actions 手动 backfill：只补存量 fd/fdorg 链接，不采集",
    )
    relink.add_argument(
        "--relink-jc",
        action="store_true",
        help="仅 Actions 手动 backfill：只补独立 API 与后到竞彩的链接，不采集",
    )
    args = parser.parse_args()
    if args.catalogue and (args.command != "backfill" or args.relink_jc or args.relink_fdorg):
        parser.error("--catalogue 仅供非 relink 的 backfill 使用")
    if (args.max_requests is not None or args.resume is not None) and not (
        (args.command == "backfill" and args.catalogue)
        or (args.command == "probe" and args.probe_kind == "jc-kickoffs" and args.resume is None)
    ):
        parser.error(
            "--max-requests 仅供 --catalogue backfill / jc-kickoffs probe；--resume 仅供 backfill"
        )
    if args.max_requests is not None and not 1 <= args.max_requests <= 7000:
        parser.error("--max-requests 必须在 1–7000 之间")
    if args.relink_fdorg or args.relink_jc:
        if args.command != "backfill" or any(
            v is not None
            for v in (
                args.source,
                args.leagues,
                args.first,
                args.last,
                args.seasons,
                args.probe_kind,
                args.per_scope,
            )
        ):
            flag = "--relink-jc" if args.relink_jc else "--relink-fdorg"
            parser.error(f"{flag} 仅供无来源/范围参数的 backfill 使用")
        if args.relink_jc:
            from zucai.sources.jc_counterpart import require_relink_actions
        else:
            from zucai.sources.fd_counterpart import require_relink_actions

        require_relink_actions()
    if args.command not in {"backfill", "probe", "identity"} and any(
        v is not None for v in (args.leagues, args.source)
    ):
        parser.error("--source / --leagues 仅供 backfill / probe / identity 使用")
    if args.command != "backfill" and any(v is not None for v in (args.first, args.last)):
        parser.error("--from / --to 仅供 backfill 使用")
    if args.command not in {"probe", "identity"} and args.seasons is not None:
        parser.error("--seasons 仅供 probe / identity 使用")
    if args.command != "probe" and args.probe_kind is not None:
        parser.error("--probe-kind 仅供 probe 使用")
    if args.per_scope is not None and (args.command != "probe" or args.probe_kind != "stats"):
        parser.error("--per-scope 仅供 probe --probe-kind stats 使用")
    if args.command == "reconcile":
        from zucai.cloud_probe import require_probe_actions

        require_probe_actions()
    if args.command == "probe" and args.probe_kind in {"catalogue", "jc-catalogue", "jc-kickoffs"}:
        if any(value is not None for value in (args.leagues, args.seasons, args.per_scope)):
            parser.error("目录 probe 不接受 --leagues / --seasons / --per-scope")
        if args.probe_kind in {"jc-catalogue", "jc-kickoffs"}:
            if args.source is not None:
                parser.error("JC 目录 probe 不接受 --source")
            from zucai.cloud_jc_catalogue import require_jc_catalogue_actions

            require_jc_catalogue_actions()
        else:
            if args.source not in (None, "apif"):
                parser.error("API 目录 probe 的 --source 仅支持 apif")
            if (
                os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            ):
                raise ValueError("API catalogue 仅允许 github-hosted Actions workflow_dispatch")
    elif args.command in {"probe", "identity"}:
        from zucai.cloud_probe import probe_scopes, require_probe_actions

        if args.source not in (None, "apif"):
            parser.error("probe / identity 仅支持 --source apif")
        require_probe_actions()
        if args.command == "identity":
            from zucai.cloud_identity import identity_scopes

            identity_scopes(args.leagues, args.seasons)
        elif args.probe_kind == "stats":
            from zucai.cloud_stats_probe import DEFAULT_PER_SCOPE, stats_plan

            args.per_scope = DEFAULT_PER_SCOPE if args.per_scope is None else args.per_scope
            stats_plan(args.leagues, args.seasons, args.per_scope, datetime.now(UTC))
        elif args.probe_kind == "leagues":
            from zucai.cloud_identity_probe import identity_codes

            if args.seasons is not None:
                parser.error("联赛元数据 probe 不接受 --seasons")
            identity_codes(args.leagues)
        else:
            probe_scopes(args.leagues, args.seasons, datetime.now(UTC))
    if not 0 <= args.days <= 366:
        parser.error("--days 必须在 0–366 之间")
    logging.basicConfig(level=logging.INFO)
    target = args.db.resolve()
    archive = args.archive.resolve()
    day = args.through or datetime.now(BEIJING).date()
    collection = None
    summary_collection = "apif"
    if args.command in {"sync", "apis", "verify", "backfill", "probe", "identity", "reconcile"}:
        if target.exists():
            raise ValueError("工作目录内已有数据库，请在全新的隔离目录运行云端同步")
        if target.parent != Path("data").resolve():
            parser.error("云端同步请在隔离的工作目录运行，数据库放在该目录的 data 下")
        meta = restore_feeds(archive, target)
        if args.command == "verify":
            from zucai.apif_budget import normalize_quota
            from zucai.forecast_archive import verify_forecasts

            quota_file = target.parent / "state/api_football_quota.json"
            if quota_file.exists():
                normalize_quota(json.loads(quota_file.read_bytes()))
            forecasts = verify_forecasts(archive)
            LOG.info("私有云端归档已恢复并校验：%s", meta["counts"])
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                    summary.write(
                        f"### Football feed archive: verified\n\n行数：{meta['counts']}\n"
                    )
                    summary.write(
                        f"\n预测归档已校验：{len(forecasts['runs']) if forecasts else 0} 次运行\n"
                    )
            return
        os.environ["ZUCAI_DB"] = str(target)
    if args.command == "apis":
        # Keep Unix-only API quota dependencies out of the Windows sporttery path.
        from zucai.cloud_apis import collect_apis

        collection = collect_apis(target, datetime.now(UTC), args.days, args.slot)
        # API collection must not advance the official results reconciliation watermark.
        day = date.fromisoformat(meta["through_beijing"])
    if args.command == "reconcile":
        from zucai.sources.apif_reconcile import reconcile
        from zucai.sources.apif_reconcile import write_summary as identity_summary
        from zucai.sources.jc_identity import reconcile as reconcile_jc

        collection = {
            "apif": reconcile(target, args.run_id),
            "jc_identity": reconcile_jc(target, args.run_id),
        }
        day = date.fromisoformat(meta["through_beijing"])
    if args.command in {"probe", "identity"}:
        if args.command == "identity":
            from zucai.cloud_identity import collect_identity as collector
            from zucai.cloud_identity import write_identity_summary as identity_summary
        elif args.probe_kind == "catalogue":
            from zucai.cloud_catalogue import collect_catalogue as collector
            from zucai.cloud_catalogue import write_summary as identity_summary
        elif args.probe_kind == "jc-catalogue":
            from zucai.cloud_jc_catalogue import collect_jc_catalogue as collector
            from zucai.cloud_jc_catalogue import write_summary as identity_summary
        elif args.probe_kind == "jc-kickoffs":
            from zucai.jc_catalogue_kickoffs import collect_kickoffs as collector
            from zucai.jc_catalogue_kickoffs import write_summary as identity_summary
        elif args.probe_kind == "stats":
            from zucai.cloud_stats_probe import collect_stats_probe as collector
            from zucai.cloud_stats_probe import write_stats_probe_summary as identity_summary
        elif args.probe_kind == "leagues":
            from zucai.cloud_identity_probe import collect_league_probe as collector
            from zucai.cloud_identity_probe import write_league_probe_summary as identity_summary
        else:
            from zucai.cloud_probe import collect_probe as collector
            from zucai.cloud_probe import write_probe_summary as identity_summary
        catalogue_probe = args.probe_kind in {"catalogue", "jc-catalogue", "jc-kickoffs"}
        options = {} if catalogue_probe else dict(codes=args.leagues)
        if args.probe_kind == "stats":
            options["per_scope"] = args.per_scope
        if not catalogue_probe and args.probe_kind != "leagues":
            options["seasons"] = args.seasons
        if args.probe_kind == "jc-kickoffs":
            options["max_requests"] = args.max_requests if args.max_requests is not None else 5
            options["archive"] = archive
        result = collector(target, args.run_id, **options)
        summary_collection = result.get("collection", "apif")
        if args.probe_kind == "jc-kickoffs":
            from zucai.jc_catalogue_kickoffs import collection_summary

            result = collection_summary(result, target.parent)
        collection = {summary_collection: result}
        day = date.fromisoformat(meta["through_beijing"])
    if args.command == "backfill":
        provider = "jc" if args.relink_jc else args.source or "fd"
        options = dict(codes=args.leagues, first=args.first, last=args.last)
        if args.relink_jc:
            from zucai.sources.jc_counterpart import relink_jc

            result = relink_jc(target)
        elif args.relink_fdorg:
            from zucai.sources.fd_counterpart import relink_fdorg

            result = relink_fdorg(target)
        elif args.catalogue:
            from zucai.cloud_catalogue_backfill import collect_catalogue_backfill

            result = collect_catalogue_backfill(
                target,
                args.run_id,
                provider=provider,
                **options,
                max_requests=args.max_requests if args.max_requests is not None else 500,
                resume=args.resume if args.resume is not None else True,
            )
        elif provider == "fd":
            from zucai.cloud_backfill import collect_backfill

            result = collect_backfill(target, args.run_id, **options)
        else:
            from zucai.cloud_api_backfill import collect_api_backfill

            result = collect_api_backfill(target, args.run_id, provider=provider, **options)
        collection = {provider: result}
        day = date.fromisoformat(meta["through_beijing"])
    if args.command == "sync":
        from zucai.sources.sfc import collect_current_sfc

        jc, windows_complete = collect_jc(target, meta, day, args.days)
        collection = {
            "jc": jc,
            "sfc": collect_current_sfc(target),
        }
        if not windows_complete:
            day = date.fromisoformat(meta["through_beijing"])
    published_collection = collection
    if args.command in {"sync", "apis"}:
        from zucai.source_result_reconcile import reconcile_source_results
        from zucai.source_results_audit import audit_source_results

        checked_at = datetime.now(UTC)
        links = reconcile_source_results(target, checked_at)
        audit = audit_source_results(
            target, target.parent / "state/audit/apif_results_crosscheck.csv"
        )
        owner = "apif" if args.command == "apis" else "jc"
        if owner in collection:
            collection[owner]["result_source_links"] = links
            collection[owner]["source_results_audit"] = audit
        for result in collection.values():
            result.update(run_id=args.run_id, observed_utc=datetime.now(UTC).isoformat())
        previous = {
            name: result
            for name, result in meta.get("collection", {}).items()
            if name != "jc-kickoffs"
        }
        published_collection = {**previous, **collection}
    elif args.command == "probe" and args.probe_kind == "jc-kickoffs":
        previous = {
            name: result
            for name, result in meta.get("collection", {}).items()
            if name != "jc-kickoffs"
        }
        published_collection = {**previous, **collection}
    manifest = export_feeds(target, archive, day, args.run_id, collection=published_collection)
    if args.command == "backfill":
        from zucai.forecast_archive import verify_forecasts

        if args.relink_jc:
            from zucai.sources.jc_counterpart import write_summary
        elif args.relink_fdorg:
            from zucai.sources.fd_counterpart import write_summary
        elif args.catalogue:
            from zucai.cloud_catalogue_backfill import write_summary
        elif provider == "fd":
            from zucai.cloud_backfill import write_summary
        else:
            from zucai.cloud_api_backfill import write_summary

        with TemporaryDirectory(prefix="backfill-verify-") as temporary:
            restore_feeds(archive, Path(temporary) / "data/zucai.db")
        verify_forecasts(archive)
        LOG.info("%s 历史回填归档恢复校验通过", provider)
        write_summary(collection[provider])
    if args.command in {"probe", "identity", "reconcile"}:
        from zucai.forecast_archive import verify_forecasts

        with TemporaryDirectory(prefix="apif-probe-verify-") as temporary:
            restore_feeds(archive, Path(temporary) / "data/zucai.db")
        verify_forecasts(archive)
        identity_summary(collection[summary_collection])
        if args.command == "reconcile":
            from zucai.sources.jc_identity import write_summary as jc_identity_summary

            jc_identity_summary(collection["jc_identity"])
    if manifest["pending_result_count"]:
        LOG.warning(
            "部分归档 partial：待补抓 %d 场，拒绝清单已持久保存", manifest["pending_result_count"]
        )
    if args.command in {"sync", "apis"} and os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(
                f"### Football feed archive: {manifest['collection_status']}\n\n"
                f"已归档比分 {manifest['counts']['results']} 场；"
                f"待补抓 {manifest['pending_result_count']} 场。\n\n"
                "拒绝清单：数据分支 state/rejected_results.csv。"
                "待补抓日期会在下次同步重试；历史拒绝记录保留。\n"
            )
            if collection:
                summary.write(
                    "\n| 来源 | 状态 | 入库 | 链接 | 拒绝 | 请求/文件 | 剩余配额 |\n"
                    "| --- | --- | ---: | ---: | ---: | ---: | ---: |\n"
                )
                for provider, result in collection.items():
                    calls = (
                        result.get("downloaded_files", 0)
                        if provider == "fd"
                        else result.get("requests")
                    )
                    linked = sum(
                        result.get(k, 0)
                        for k in ("linked_existing", "linked_late_jc", "linked_fd", "linked_fdorg")
                    )
                    summary.write(
                        f"| {provider} | {result['status']} | "
                        f"{result['inserted'] if result['inserted'] is not None else '未统计'} | "
                        f"{linked} | {result['rejected_rows']} | "
                        f"{calls if calls is not None else '未统计'} | "
                        f"{result.get('remaining_quota', '—')} |\n"
                    )
                    if provider == "apif":
                        write_apif_quota(summary, result)
                    for error in result["errors"]:
                        safe_error = html.escape(error) if provider == "apif" else error
                        summary.write(f"\n- {provider}: {safe_error}\n")
                summary.write("\n次数口径：fd 为成功下载文件数；API 为请求数（含失败请求）。\n")
                summary.write(
                    "\n| 来源 | 欧战赛事 | 本次新增比赛 | 本次新增赛果 | 库内比赛 | 库内赛果 |\n"
                    "| --- | --- | ---: | ---: | ---: | ---: |\n"
                )
                for provider, result in collection.items():
                    for league, counts in result.get("cups", {}).items():
                        summary.write(
                            f"| {provider} | {league} | {counts['matches_added']} | "
                            f"{counts['results_added']} | {counts['matches_after']} | "
                            f"{counts['results_after']} |\n"
                        )
                summary.write(
                    "\n欧战表仅计各来源入库行；链接竞彩计入上表链接数，不覆盖竞彩赛果。\n"
                )


if __name__ == "__main__":
    main()
