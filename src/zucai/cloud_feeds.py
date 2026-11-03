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
    "closing_odds": ("match_id", "bookmaker", "pool", "h", "d", "a", "basis"),
}
MANIFEST = "feed_manifest.json"
MONTH = r"[0-9]{4}-(?:0[1-9]|1[0-2])"
STATE_FILE = (
    r"state/(?:api_football_quota\.json|"
    r"audit/(?:jc|fd|fdorg|apif)_[a-z_]+\.csv|"
    r"alias_candidates/(?:fdorg|apif)\.json)"
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
    state = source.parent / "state"
    for path in sorted(state.rglob("*")) if state.exists() else []:
        key = "state/" + path.relative_to(state).as_posix()
        if path.is_file() and re.fullmatch(STATE_FILE, key):
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
        "created_utc": datetime.now(UTC).isoformat(),
        "counts": totals,
        "sources": sources,
        "collection": collection or {},
        "collection_status": "partial"
        if pending or any(item.get("status") == "partial" for item in (collection or {}).values())
        else "complete",
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
    if meta["version"] not in (1, 2):
        raise ValueError("不支持的云端归档版本")
    for key, want in meta["files"].items():
        if not re.fullmatch(
            rf"state/(?:teams|matches|results|closing_odds)\.jsonl|state/(?:unmatched_teams|rejected_results)\.csv|state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv|{STATE_FILE}",
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
            check_db(db)
            if meta["version"] == 2 and source_inventory(db) != meta.get("sources"):
                raise ValueError("各来源行数或哈希校验失败")
        db.close()
        os.replace(dbfile, dest)
        for name in ("unmatched_teams.csv", "rejected_results.csv"):
            if f"state/{name}" in meta["files"]:
                (dest.parent / name).write_bytes((archive / f"state/{name}").read_bytes())
        for key in meta["files"]:
            if re.fullmatch(STATE_FILE, key):
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
        "--probe-kind", choices=("seasons", "leagues"), help="仅 probe：赛季权限或桥接联赛元数据"
    )
    parser.add_argument("--from", dest="first", help="仅 backfill：起始年或赛季，如 2021 / 2122")
    parser.add_argument("--to", dest="last", help="仅 backfill：终止年或赛季，如 2026 / 2627")
    parser.add_argument(
        "--relink-fdorg",
        action="store_true",
        help="仅 Actions 手动 backfill：只补存量 fd/fdorg 链接，不采集",
    )
    args = parser.parse_args()
    if args.relink_fdorg:
        if args.command != "backfill" or any(
            v is not None
            for v in (
                args.source,
                args.leagues,
                args.first,
                args.last,
                args.seasons,
                args.probe_kind,
            )
        ):
            parser.error("--relink-fdorg 仅供无来源/范围参数的 backfill 使用")
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
    if args.command == "reconcile":
        from zucai.cloud_probe import require_probe_actions

        require_probe_actions()
    if args.command in {"probe", "identity"}:
        from zucai.cloud_probe import probe_scopes, require_probe_actions

        if args.source not in (None, "apif"):
            parser.error("probe / identity 仅支持 --source apif")
        require_probe_actions()
        if args.command == "identity":
            from zucai.cloud_identity import identity_scopes

            identity_scopes(args.leagues, args.seasons)
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

        collection = {"apif": reconcile(target, args.run_id)}
        day = date.fromisoformat(meta["through_beijing"])
    if args.command in {"probe", "identity"}:
        if args.command == "identity":
            from zucai.cloud_identity import collect_identity as collector
            from zucai.cloud_identity import write_identity_summary as identity_summary
        elif args.probe_kind == "leagues":
            from zucai.cloud_identity_probe import collect_league_probe as collector
            from zucai.cloud_identity_probe import write_league_probe_summary as identity_summary
        else:
            from zucai.cloud_probe import collect_probe as collector
            from zucai.cloud_probe import write_probe_summary as identity_summary
        options = dict(codes=args.leagues)
        if args.probe_kind != "leagues":
            options["seasons"] = args.seasons
        collection = {"apif": collector(target, args.run_id, **options)}
        day = date.fromisoformat(meta["through_beijing"])
    if args.command == "backfill":
        from zucai.cloud_backfill import collect_backfill

        provider = args.source or "fd"
        options = dict(codes=args.leagues, first=args.first, last=args.last)
        if args.relink_fdorg:
            from zucai.sources.fd_counterpart import relink_fdorg

            result = relink_fdorg(target)
        elif provider == "fd":
            result = collect_backfill(target, args.run_id, **options)
        else:
            from zucai.cloud_api_backfill import collect_api_backfill

            result = collect_api_backfill(target, args.run_id, provider=provider, **options)
        collection = {provider: result}
        day = date.fromisoformat(meta["through_beijing"])
    if args.command == "sync":
        fetch_schedule()
        windows = date_windows(target, date.fromisoformat(meta["through_beijing"]), day, args.days)
        for i, (start, end) in enumerate(windows):
            if i:
                time.sleep(1)
            try:
                fetch_results(start, end)
            except ResultRowsRejected as exc:
                LOG.warning("单场拒绝，继续处理其他窗口：%s", exc)
    manifest = export_feeds(target, archive, day, args.run_id, collection=collection)
    if args.command == "backfill":
        from zucai.forecast_archive import verify_forecasts

        if args.relink_fdorg:
            from zucai.sources.fd_counterpart import write_summary
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
        identity_summary(collection["apif"])
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
                        else result["requests"]
                    )
                    linked = sum(
                        result.get(k, 0) for k in ("linked_existing", "linked_fd", "linked_fdorg")
                    )
                    summary.write(
                        f"| {provider} | {result['status']} | {result['inserted']} | "
                        f"{linked} | {result['rejected_rows']} | {calls} | "
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
