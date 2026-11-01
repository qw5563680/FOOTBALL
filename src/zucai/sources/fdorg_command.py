"""Collect v4 fixtures, or replay the exact same HTTP observation without credentials."""

import csv
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from zucai.db import get_engine, init_db, session_scope
from zucai.sources.base import RawStore
from zucai.sources.fdorg_ingest import COUNT_KEYS, import_fdorg_games
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.football_data_org import FootballDataOrg

LOG = logging.getLogger(__name__)
COMPETITIONS = CONFIG_ROOT / "football_data_org/competitions.json"
ALIASES = CONFIG_ROOT / "team_aliases/football_data_org.json"


def _requests(competition, seasons, days, assume, finished_only, today, competitions):
    codes = [code.strip() for code in competition.split(",")]
    if (
        not codes
        or any(code not in competitions for code in codes)
        or len(set(codes)) != len(codes)
    ):
        raise ValueError("--competition 须给出不重复、已映射的联赛代码")
    if bool(seasons) == (days is not None):
        raise ValueError("请选择一种范围：--season 或 --days")
    if seasons:
        if not isinstance(assume, timedelta) or not timedelta(0) < assume <= timedelta(days=14):
            raise ValueError("赛季回填必须显式指定 --assume-known-after，正延时且不超过14天")
        if len(set(seasons)) != len(seasons) or any(
            type(s) is not int or not 1900 <= s <= 9999 for s in seasons
        ):
            raise ValueError("--season 须为不重复的四位年份")
        return [
            dict(competition=code, season=s, status="FINISHED" if finished_only else None)
            for code in codes
            for s in seasons
        ]
    if assume is not None or finished_only:
        raise ValueError("--days 同时采集赛程和赛果，只允许 captured；假设和状态筛选仅用于赛季")
    if type(days) is not int or days < 0:
        raise ValueError("--days 须为非负整数")
    try:
        start = today - timedelta(days=days)
        end = today + timedelta(days=days + 1)  # v4 excludes dateTo
    except OverflowError as exc:
        raise ValueError("--days 日期范围溢出") from exc
    return [dict(competition=",".join(codes), date_from=start.isoformat(), date_to=end.isoformat())]


def _capture_time(meta):
    seen = datetime.fromisoformat(meta["fetched_at_utc"])
    if seen.utcoffset() is None:
        raise ValueError("缺少有时区的真实采集时刻")
    return seen.astimezone(UTC)


def _url_identity(url):
    parts = urlsplit(url)
    return (
        parts.scheme,
        parts.netloc,
        parts.path,
        parts.fragment,
        sorted(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)),
    )


def _audit(path, rows, sha):
    if not rows:
        return
    fields = (
        "raw_sha256",
        "row_number",
        "competition_code",
        "league",
        "provider_match_id",
        "home_provider_id",
        "away_provider_id",
        "home",
        "away",
        "kickoff_utc",
        "status",
        "reason",
        "candidates",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        if stream.tell() == 0:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "raw_sha256": sha,
                    "candidates": json.dumps(row.get("candidates", []), ensure_ascii=False),
                }
            )


def fetch_fdorg(
    *,
    competition,
    seasons=None,
    days=None,
    assume=None,
    finished_only=False,
    aliases_path=None,
    db=None,
    raw_file=None,
    raw_root="data/raw",
    audit_dir="data",
):
    """Commit each response separately and return its row-level audit."""
    competitions, aliases = read_json(COMPETITIONS), read_json(aliases_path or ALIASES)
    path = Path(raw_file) if raw_file is not None else None
    replay_meta = read_json(path.with_suffix(path.suffix + ".meta.json")) if path else None
    today = (
        _capture_time(replay_meta).date() if replay_meta is not None else datetime.now(UTC).date()
    )
    scopes = _requests(competition, seasons, days, assume, finished_only, today, competitions)
    if path is not None and len(scopes) != 1:
        raise ValueError("一次只可重放一个已归档的查询")
    source = FootballDataOrg(store=RawStore(raw_root), competitions=competitions)
    totals = dict.fromkeys(COUNT_KEYS, 0)
    names, files = set(), []
    engine = get_engine(db)
    try:
        for scope in scopes:
            if replay_meta is None:
                path = source.fetch_raw(**scope)
                meta = source.store.load_metadata(path)
            else:
                meta = replay_meta
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            if (
                meta.get("status_code") != 200
                or meta.get("sha256") != sha
                or not isinstance(meta.get("url"), str)
                or _url_identity(meta["url"]) != _url_identity(source.request_url(**scope))
            ):
                raise ValueError("原件必须具有与所请求范围匹配的 URL、HTTP 200 和 SHA256")
            seen = _capture_time(meta)
            games = source.parse(raw, **scope)
            init_db(engine)
            with session_scope(engine) as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                result = import_fdorg_games(
                    session, games, seen=seen, assume=assume, aliases=aliases
                )
            result["rejected"] = [*source.rejected, *result["rejected"]]
            result["rejected_rows"] = len(result["rejected"])
            _audit(Path(audit_dir) / "fdorg_rejected_rows.csv", result["rejected"], sha)
            _audit(Path(audit_dir) / "fdorg_suspected_duplicates.csv", result["suspected"], sha)
            for key in totals:
                totals[key] += result[key]
            names.update((n["league"], n["id"]) for n in result["unmatched_names"])
            files.append(dict(scope=scope, raw_sha256=sha, seen_utc=seen.isoformat(), **result))
            LOG.info(
                "FDORG %s 入库=%d linked_existing=%d linked_fd=%d 拒绝=%d 可疑=%d",
                scope,
                result["inserted"],
                result["linked_existing"],
                result["linked_fd"],
                result["rejected_rows"],
                result["suspected_duplicates"],
            )
    finally:
        source.session.close()
        engine.dispose()
    totals.update(
        requests=source.request_count,
        remaining_quota=source.remaining_quota,
        unmatched_teams=len(names),
    )
    LOG.info(
        "FDORG 请求=%d 剩余配额=%s 入库=%d linked_existing=%d linked_fd=%d 拒绝=%d "
        "可疑重复=%d 未对齐球队=%d assumed=%d",
        totals["requests"],
        totals["remaining_quota"],
        totals["inserted"],
        totals["linked_existing"],
        totals["linked_fd"],
        totals["rejected_rows"],
        totals["suspected_duplicates"],
        totals["unmatched_teams"],
        totals["assumed"],
    )
    return dict(totals, files=files)
