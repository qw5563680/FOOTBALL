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
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.db import get_engine, init_db
from zucai.sources.results import fetch_results
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
SELECT m.match_id,m.jc_num,m.league,datetime(m.kickoff_utc,'+8 hours') AS kickoff_beijing,
 h.name AS home,a.name AS away,m.status,r.home_goals,r.away_goals,r.ht_home_goals,r.ht_away_goals,
 r.available_utc,r.available_source,m.result_provider
FROM matches m JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id
LEFT JOIN results r ON m.match_id=r.match_id ORDER BY m.kickoff_utc,m.match_id
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


def export_feeds(source: Path, archive: Path, through: date, run_id: str) -> None:
    if not source.is_file():
        raise ValueError("采集数据库缺失")
    archive.mkdir(parents=True, exist_ok=True)
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
    unmatched = source.parent / "unmatched_teams.csv"
    if unmatched.is_file():
        key = "state/unmatched_teams.csv"
        (archive / key).write_bytes(unmatched.read_bytes())
        files.append(key)
    manifest = {
        "version": 1,
        "through_beijing": through.isoformat(),
        "run": run_id,
        "created_utc": datetime.now(UTC).isoformat(),
        "counts": totals,
        "files": {k: digest(archive / k) for k in sorted(files)},
    }
    (archive / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def archive_info(archive: Path) -> dict:
    meta = json.loads((archive / MANIFEST).read_text(encoding="utf-8"))
    if meta["version"] != 1:
        raise ValueError("不支持的云端归档版本")
    for key, want in meta["files"].items():
        if not re.fullmatch(
            rf"state/(?:teams|matches|results)\.jsonl|state/unmatched_teams\.csv|state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv",
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
                names = (
                    [k for k in sorted(meta["files"]) if k.startswith("state/odds/")]
                    if table == "odds"
                    else [f"state/{table}.jsonl"]
                )
                for key in names:
                    with (archive / key).open(encoding="utf-8") as f:
                        db.executemany(
                            f"INSERT INTO {table} ({','.join(cols)}) "
                            f"VALUES ({','.join('?' for _ in cols)})",
                            (json.loads(line) for line in f),
                        )
                count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count != meta["counts"][table]:
                    raise ValueError(f"{table} 行数校验失败")
            check_db(db)
        db.close()
        os.replace(dbfile, dest)
        if "state/unmatched_teams.csv" in meta["files"]:
            (dest.parent / "unmatched_teams.csv").write_bytes(
                (archive / "state/unmatched_teams.csv").read_bytes()
            )
    return meta


def date_windows(database: Path, previous: date, today: date, days: int) -> list[tuple[date, date]]:
    if days < 1 or days > 366 or previous > today:
        raise ValueError("云端回看范围无效")
    start = min(today - timedelta(days=days - 1), previous - timedelta(days=1))
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
    return [(d, d) for d in sorted(extra)] + [(start, today)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "sync"))
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
    if args.command == "sync":
        if target.parent != Path("data").resolve():
            parser.error("云端同步请在隔离的工作目录运行，数据库放在该目录的 data 下")
        meta = restore_feeds(archive, target)
        os.environ["ZUCAI_DB"] = str(target)
        fetch_schedule()
        windows = date_windows(target, date.fromisoformat(meta["through_beijing"]), day, args.days)
        for i, (start, end) in enumerate(windows):
            if i:
                time.sleep(1)
            fetch_results(start, end)
    export_feeds(target, archive, day, args.run_id)


if __name__ == "__main__":
    main()
