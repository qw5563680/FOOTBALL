"""Collect v4 fixtures, or replay the exact same HTTP observation without credentials."""

import csv
import hashlib
import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from zucai.db import get_engine, init_db, session_scope
from zucai.league_registry import provider_config
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.cup_identity import (
    capture_cup_history,
    identity_capture,
    needs_cup_union,
    resolve_cup_aliases,
)
from zucai.sources.fd_audit import append_unique
from zucai.sources.fdorg_ingest import COUNT_KEYS, import_fdorg_games
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.football_data_org import FootballDataOrg

LOG = logging.getLogger(__name__)
COMPETITIONS = CONFIG_ROOT / "football_data_org/competitions.json"
_DEFAULT_COMPETITIONS = COMPETITIONS
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
    windows = []
    while start < end:
        stop = start + timedelta(days=min(10, (end - start).days))
        windows.append(
            dict(competition=",".join(codes), date_from=start.isoformat(), date_to=stop.isoformat())
        )
        start = stop
    return windows


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
    append_unique(
        path,
        fields,
        (
            {
                **row,
                "raw_sha256": sha,
                "candidates": json.dumps(row.get("candidates", []), ensure_ascii=False),
            }
            for row in rows
        ),
        lambda row: (
            row.get("provider_match_id"),
            "" if row.get("provider_match_id") else row.get("row_number"),
            row.get("reason"),
            row.get("candidates"),
        ),
    )


def _unmatched_teams(path, names, seen):
    """Keep one earliest captured observation per league/provider team ID."""
    if not names:
        return
    fields = ("league", "team_id", "english_name", "first_seen_utc")
    records = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as stream:
            records = {(r["league"], r["team_id"]): r for r in csv.DictReader(stream)}
    changed = False
    for name in names:
        key = name["league"], str(name["id"])
        previous = records.get(key)
        if previous is None or seen < _capture_time({"fetched_at_utc": previous["first_seen_utc"]}):
            records[key] = dict(
                league=name["league"],
                team_id=str(name["id"]),
                english_name=name["name"],
                first_seen_utc=seen.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            )
            changed = True
    if changed:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fields)
        writer.writeheader()
        writer.writerows(records[key] for key in sorted(records))
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, buffer.getvalue().encode("utf-8"))


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
    competitions = (
        provider_config("fdorg")
        if COMPETITIONS == _DEFAULT_COMPETITIONS
        else read_json(COMPETITIONS)
    )
    aliases = read_json(aliases_path or ALIASES)
    path = Path(raw_file) if raw_file is not None else None
    replay_meta = read_json(path.with_suffix(path.suffix + ".meta.json")) if path else None
    today = (
        _capture_time(replay_meta).date() if replay_meta is not None else datetime.now(UTC).date()
    )
    if replay_meta is not None and days is not None and "window_anchor_date" in replay_meta:
        anchor = replay_meta["window_anchor_date"]
        try:
            today = date.fromisoformat(anchor)
        except (ValueError, TypeError):
            raise ValueError("归档窗口起算日期无效") from None
        if today.isoformat() != anchor or today > _capture_time(replay_meta).date():
            raise ValueError("归档窗口起算日期无效或晚于采集日期")
    scopes = _requests(competition, seasons, days, assume, finished_only, today, competitions)
    if path is not None and days is None and len(scopes) != 1:
        raise ValueError("一次只可重放一个已归档的赛季查询")
    source = FootballDataOrg(store=RawStore(raw_root), competitions=competitions)
    totals = dict.fromkeys(COUNT_KEYS, 0)
    names, files = set(), []
    engine = get_engine(db)
    try:
        if replay_meta is not None:
            if not isinstance(replay_meta.get("url"), str):
                raise ValueError("原件 URL 必须为字符串")
            scopes = [
                scope
                for scope in scopes
                if _url_identity(replay_meta["url"]) == _url_identity(source.request_url(**scope))
            ]
            if len(scopes) != 1:
                raise ValueError("原件 URL 必须匹配一个请求窗口；每次重放一份归档")
        staged, deferred_error = None, None
        if replay_meta is None and aliases_path is None and needs_cup_union("fdorg", scopes):
            staged, deferred_error = capture_cup_history(source, scopes)
            scopes = scopes[: len(staged)]
            if staged:
                init_db(engine)
                aliases = resolve_cup_aliases(
                    "fdorg",
                    engine.url.database,
                    aliases,
                    [capture for _, capture in staged],
                    audit_dir,
                )
        for index, scope in enumerate(scopes):
            if replay_meta is None:
                path = staged[index][0] if staged is not None else source.fetch_raw(**scope)
                meta = source.store.load_metadata(path)
                if days is not None:
                    meta["window_anchor_date"] = today.isoformat()
                    _atomic_write(
                        path.with_suffix(path.suffix + ".meta.json"),
                        json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                    )
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
            if aliases_path is None and staged is None:
                aliases = resolve_cup_aliases(
                    "fdorg",
                    engine.url.database,
                    aliases,
                    [
                        identity_capture(
                            games,
                            source.rejected,
                            scope,
                            raw,
                            meta,
                            path.with_suffix(path.suffix + ".meta.json"),
                        )
                    ],
                    audit_dir,
                )
            with session_scope(engine) as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                result = import_fdorg_games(
                    session, games, seen=seen, assume=assume, aliases=aliases, actual_results=True
                )
            result["rejected"] = [*source.rejected, *result["rejected"]]
            result["rejected_rows"] = len(result["rejected"])
            _audit(Path(audit_dir) / "fdorg_rejected_rows.csv", result["rejected"], sha)
            _audit(Path(audit_dir) / "fdorg_suspected_duplicates.csv", result["suspected"], sha)
            for key in totals:
                totals[key] += result[key]
            _unmatched_teams(
                Path(audit_dir) / "fdorg_unmatched_teams.csv", result["unmatched_names"], seen
            )
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
        if deferred_error is not None:
            raise deferred_error
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
