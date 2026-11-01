"""Persist the official feed as portable, versioned tables in the private data branch."""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.db import get_engine, init_db
from zucai.sources.results import ResultRowsRejected, fetch_results
from zucai.sources.sporttery import BEIJING, fetch_schedule

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
}
MANIFEST = "feed_manifest.json"
MONTH = r"[0-9]{4}-(?:0[1-9]|1[0-2])"
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
SELECT m.match_id,m.jc_num,m.league,datetime(m.kickoff_utc,'+8 hours') AS kickoff_beijing,
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


def dump_rows(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n")


def check_db(db: sqlite3.Connection) -> None:
    if (
        db.execute("PRAGMA quick_check").fetchone() != ("ok",)
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("数据库结构或外键校验失败")
    if db.execute("""SELECT 1 FROM matches m LEFT JOIN results r USING(match_id)
                  WHERE (m.status='finished') <> (r.match_id IS NOT NULL) LIMIT 1""").fetchone():
        raise ValueError("完赛状态与比分表不一致")


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


def export_feeds(source: Path, archive: Path, through: date, run_id: str) -> dict:
    if not source.is_file():
        raise ValueError("采集数据库缺失")
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
                dump_rows(archive / key, db.execute(query + f" ORDER BY {cols[0]}"))
                files.append(key)
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
    # Only remove obsolete files owned and checked by the previous archive.
    for key in previous.difference(files):
        target = (archive / key).resolve()
        if not target.is_relative_to(archive.resolve()):
            raise ValueError("旧档路径超出归档目录")
        target.unlink()
    manifest = {
        "version": 1,
        "columns": {name: list(cols) for name, cols in TABLES.items()},
        "through_beijing": through.isoformat(),
        "run": run_id,
        "created_utc": datetime.now(UTC).isoformat(),
        "counts": totals,
        "collection_status": "partial" if pending else "complete",
        "pending_result_count": len(pending),
        "pending_result_dates": sorted({row["date"] for row in pending}),
        "files": {k: digest(archive / k) for k in sorted(files)},
    }
    (archive / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def archive_info(archive: Path) -> dict:
    meta = json.loads((archive / MANIFEST).read_text(encoding="utf-8"))
    if meta["version"] != 1:
        raise ValueError("不支持的云端归档版本")
    for key, want in meta["files"].items():
        if not re.fullmatch(
            rf"state/(?:teams|matches|results)\.jsonl|state/(?:unmatched_teams|rejected_results)\.csv|state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv",
            key,
        ):
            raise ValueError(f"未知归档文件 {key}")
        if not (archive / key).is_file() or digest(archive / key) != want:
            raise ValueError(f"云端数据校验失败 {key}")
    for key in ("state/teams.jsonl", "state/matches.jsonl", "state/results.jsonl"):
        if key not in meta["files"]:
            raise ValueError(f"云端数据校验失败：缺少 {key}")
    return meta


def restore_feeds(archive: Path, dest: Path) -> dict:
    meta = archive_info(archive)
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
            check_db(db)
        db.close()
        os.replace(dbfile, dest)
        for name in ("unmatched_teams.csv", "rejected_results.csv"):
            if f"state/{name}" in meta["files"]:
                (dest.parent / name).write_bytes((archive / f"state/{name}").read_bytes())
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
                "WHERE status='scheduled' "
                "AND date(kickoff_utc,'+8 hours') < ?",
                (start.isoformat(),),
            )
        }
    extra.update(day for day in pending_dates if day < start)
    return [(d, d) for d in sorted(extra)] + [(start, today)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "sync", "verify"))
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/zucai.db"))
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    target = args.db.resolve()
    archive = args.archive.resolve()
    day = args.through or datetime.now(BEIJING).date()
    if args.command in {"sync", "verify"}:
        if target.exists():
            raise ValueError("工作目录内已有数据库，请在全新的隔离目录运行云端同步")
        if target.parent != Path("data").resolve():
            parser.error("云端同步请在隔离的工作目录运行，数据库放在该目录的 data 下")
        meta = restore_feeds(archive, target)
        if args.command == "verify":
            LOG.info("私有云端归档已恢复并校验：%s", meta["counts"])
            return
        os.environ["ZUCAI_DB"] = str(target)
        fetch_schedule()
        windows = date_windows(target, date.fromisoformat(meta["through_beijing"]), day, args.days)
        for i, (start, end) in enumerate(windows):
            if i:
                time.sleep(1)
            try:
                fetch_results(start, end)
            except ResultRowsRejected as exc:
                LOG.warning("单场拒绝，继续处理其他窗口：%s", exc)
    manifest = export_feeds(target, archive, day, args.run_id)
    if manifest["pending_result_count"]:
        LOG.warning(
            "部分归档 partial：待补抓 %d 场，拒绝清单已持久保存", manifest["pending_result_count"]
        )
    if args.command == "sync" and os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(
                f"### Football feed archive: {manifest['collection_status']}\n\n"
                f"已归档比分 {manifest['counts']['results']} 场；"
                f"待补抓 {manifest['pending_result_count']} 场。\n\n"
                "拒绝清单：数据分支 state/rejected_results.csv。"
                "待补抓日期会在下次同步重试；历史拒绝记录保留。\n"
            )


if __name__ == "__main__":
    main()
