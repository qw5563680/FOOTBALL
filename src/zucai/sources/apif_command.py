"""Import complete APIF scopes and replay credential-free, with each raw page retained."""

import hashlib
import logging
from datetime import timedelta
from pathlib import Path

from zucai.db import get_engine, init_db, session_scope
from zucai.sources.api_common import LocalValidationError
from zucai.sources.api_football import LEAGUES, ApiFootball
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_audit import audit_apif as _audit
from zucai.sources.apif_ingest import COUNT_KEYS, import_apif_games
from zucai.sources.base import RawStore
from zucai.sources.cup_identity import (
    capture_cup_history,
    identity_capture,
    needs_cup_union,
    resolve_cup_aliases,
)
from zucai.sources.fdorg_command import _capture_time, _unmatched_teams, _url_identity
from zucai.sources.football_data import CONFIG_ROOT, read_json

LOG = logging.getLogger(__name__)
ALIASES = CONFIG_ROOT / "team_aliases/api_football.json"


def fetch_apif(
    *,
    league_id=None,
    seasons=None,
    date=None,
    assume=None,
    reserve=10,
    aliases_path=None,
    leagues_path=None,
    db=None,
    raw_file=None,
    raw_root="data/raw",
    audit_dir="data",
    quota_path="data/api_football_quota.json",
):
    if bool(seasons) == (date is not None):
        raise LocalValidationError("须选择 --season 或 --date")
    if seasons:
        if not isinstance(assume, timedelta) or not timedelta(0) < assume <= timedelta(days=14):
            raise LocalValidationError(
                "赛季回填必须显式指定 --assume-known-after，正延时且不超过14天"
            )
        if len(set(seasons)) != len(seasons):
            raise LocalValidationError("--season 不得重复")
    elif assume is not None:
        raise LocalValidationError("--date 增量只允许 captured，不能指定假设")
    leagues = read_json(leagues_path or LEAGUES)
    aliases = read_json(aliases_path or ALIASES)
    validate_approvals(aliases, require_metadata=aliases_path is None)
    source = ApiFootball(
        store=RawStore(raw_root), leagues=leagues, quota_path=quota_path, reserve=reserve
    )
    totals = dict.fromkeys(COUNT_KEYS, 0)
    names = set()
    files = []
    scopes = (
        [dict(league_id=league_id, season=s) for s in seasons]
        if seasons
        else [dict(date=date, league_id=league_id)]
    )
    for scope in scopes:
        source.request_url(**scope)  # Validate every query before network/DB writes.
    if raw_file and len(scopes) != 1:
        raise LocalValidationError("每次只可重放一个赛季或日期原件")
    engine = get_engine(db)
    try:
        staged, deferred_error = None, None
        if not raw_file and aliases_path is None and needs_cup_union("apif", scopes):
            staged, deferred_error = capture_cup_history(source, scopes)
            scopes = scopes[: len(staged)]
            if staged:
                init_db(engine)
                aliases = resolve_cup_aliases(
                    "apif",
                    engine.url.database,
                    aliases,
                    [capture for _, capture in staged],
                    audit_dir,
                )
        for index, scope in enumerate(scopes):
            if staged is not None:
                paths = [staged[index][0]]
            else:
                paths = [Path(raw_file)] if raw_file else [source.fetch_raw(**scope)]
            for path in paths:
                raw = path.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                meta = read_json(path.with_suffix(path.suffix + ".meta.json"))
                # Linking needs the complete capture, so incomplete page replays are refused.
                doc = source._document(raw, source._scope(**scope))
                if doc["paging"] != {"current": 1, "total": 1}:
                    raise LocalValidationError(
                        "分页原件不完整；须重放含全部页面的 assembled-fixtures 归档"
                    )
                page = doc["paging"]["current"]
                expected = source.request_url(**scope, page=page if page > 1 else None)
                if (
                    meta.get("status_code") != 200
                    or meta.get("sha256") != sha
                    or not isinstance(meta.get("url"), str)
                    or _url_identity(meta["url"]) != _url_identity(expected)
                ):
                    raise LocalValidationError("原件须匹配请求 URL、HTTP 200 和 SHA256")
                seen = _capture_time(meta)
                games = source.parse(raw, **scope)
                init_db(engine)
                if aliases_path is None and staged is None:
                    aliases = resolve_cup_aliases(
                        "apif",
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
                    result = import_apif_games(
                        session, games, seen=seen, assume=assume, aliases=aliases
                    )
                result["rejected"] = [*source.rejected, *result["rejected"]]
                result["rejected_rows"] = len(result["rejected"])
                _audit(Path(audit_dir) / "apif_rejected_rows.csv", result["rejected"], sha)
                _audit(Path(audit_dir) / "apif_suspected_duplicates.csv", result["suspected"], sha)
                _unmatched_teams(
                    Path(audit_dir) / "apif_unmatched_teams.csv", result["unmatched_names"], seen
                )
                for key in totals:
                    totals[key] += result[key]
                names.update((n["league"], n["id"]) for n in result["unmatched_names"])
                files.append(
                    dict(
                        scope=scope, page=page, raw_sha256=sha, seen_utc=seen.isoformat(), **result
                    )
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
            "APIF 请求=%s 剩余配额=%s 入库=%s linked_existing=%s linked_fd=%s "
            "linked_fdorg=%s 拒绝=%s 可疑=%s 未对齐球队=%s assumed=%s",
            *(
                totals[k]
                for k in (
                    "requests",
                    "remaining_quota",
                    "inserted",
                    "linked_existing",
                    "linked_fd",
                    "linked_fdorg",
                    "rejected_rows",
                    "suspected_duplicates",
                    "unmatched_teams",
                    "assumed",
                )
            ),
        )
    return dict(totals, files=files)
