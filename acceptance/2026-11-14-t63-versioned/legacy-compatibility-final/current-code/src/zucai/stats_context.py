"""Reuse archived fixture pages from normal collection, without issuing requests."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from zucai.models import ApifFixtureIndex, Match
from zucai.sources.api_football import LEAGUES
from zucai.sources.apif_stats import envelope, fixture_identity, instant
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.stats_client import ALLOWED_LEAGUES
from zucai.stats_link import link_fixtures
from zucai.stats_storage import transaction, write_audit


def _scope(doc, meta):
    url = urlparse(meta.get("url") or "")
    if (
        url.scheme != "https"
        or url.netloc != "v3.football.api-sports.io"
        or url.path != "/fixtures"
        or url.username
        or url.password
        or url.fragment
    ):
        raise ValueError("fixture 上下文原件 URL 无效")
    try:
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("fixture 上下文原件请求参数无效") from None
    if any(len(values) != 1 or not values[0] for values in query.values()):
        raise ValueError("fixture 上下文原件请求参数重复或为空")
    parameters = {key: values[0] for key, values in query.items()}
    actual = doc.get("parameters")
    if (
        not isinstance(actual, dict)
        or {key: str(value) for key, value in actual.items()} != parameters
    ):
        raise ValueError("fixture 上下文响应参数与原件 URL 不符")
    scope = {key: value for key, value in parameters.items() if key != "page"}
    if set(scope) == {"date"}:
        if date.fromisoformat(scope["date"]).isoformat() != scope["date"]:
            raise ValueError("fixture 上下文日期参数无效")
    elif set(scope) == {"league", "season"}:
        if any(
            not value.isascii() or not value.isdecimal() or int(value) <= 0
            for value in scope.values()
        ):
            raise ValueError("fixture 上下文联赛季参数无效")
    else:
        raise ValueError("fixture 上下文请求范围须为 date 或 league + season")
    if "page" in parameters:
        paging = doc.get("paging")
        if not isinstance(paging, dict) or parameters["page"] != str(paging.get("current")):
            raise ValueError("fixture 上下文原件页码不符")
    return scope


def refresh_context(database, raw_root, location):
    count = 0
    aliases = read_json(CONFIG_ROOT / "team_aliases/api_football.json")
    config = read_json(LEAGUES)
    paths = sorted((Path(raw_root) / "api-football").rglob("*.json"))
    pages = []
    for path in paths:
        if path.name.endswith(".meta.json"):
            continue
        raw = path.read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("get") != "fixtures":
            continue
        meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_bytes())
        if meta.get("status_code") != 200:
            continue
        if meta.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError("fixture 上下文原件哈希不符")
        scope = _scope(doc, meta)
        if doc.get("errors") not in ([], {}) or doc.get("paging") != {"current": 1, "total": 1}:
            continue
        envelope(doc, "fixtures", scope)
        pages.append((instant(meta["fetched_at_utc"]), path, doc, meta, scope))
    for captured, path, doc, meta, scope in sorted(pages, key=lambda entry: entry[0]):
        groups = defaultdict(list)
        metadata = dict(
            captured_utc=captured.isoformat(),
            raw_sha256=meta["sha256"],
            raw_location=location(path),
        )
        rejected = []
        ids = Counter(
            str(row["fixture"].get("id"))
            for row in doc["response"]
            if isinstance(row.get("fixture"), dict)
        )

        def reject(
            fixture_id, reason, detail, identity=None, *, rejected=rejected, metadata=metadata
        ):
            identity = identity or {}
            rejected.append(
                dict(
                    provider_fixture_id=str(fixture_id),
                    endpoint="fixtures",
                    outcome="rejected",
                    reason=reason,
                    detail=detail,
                    league=config.get(str(identity.get("provider_league_id")), {}).get(
                        "league", ""
                    ),
                    season=identity.get("season", ""),
                    **metadata,
                )
            )

        for row in doc["response"]:
            fixture_id = (
                row["fixture"].get("id", "") if isinstance(row.get("fixture"), dict) else ""
            )
            try:
                identity = fixture_identity(row)
            except (ValueError, TypeError) as exc:
                reject(fixture_id, "候选不全", str(exc))
                continue
            if ids[identity["provider_fixture_id"]] != 1:
                reject(fixture_id, "多对一", "同一原件含重复 fixture ID", identity)
                continue
            if (
                "date" in scope and identity["kickoff_utc"].date().isoformat() != scope["date"]
            ) or (
                "league" in scope
                and (identity["provider_league_id"], identity["season"])
                != (int(scope["league"]), int(scope["season"]))
            ):
                reject(fixture_id, "候选不全", "fixture 身份与原件请求范围不符", identity)
                continue
            if identity["provider_league_id"] in ALLOWED_LEAGUES and identity["season"] >= 2022:
                groups[(identity["provider_league_id"], identity["season"])].append(row)
        with transaction(database, "stats_link_audit.csv") as (session, audit):
            for (league, season), rows in groups.items():
                # This is an in-memory filtered view of a verified date/league original.
                view = dict(
                    get="fixtures",
                    parameters=dict(league=league, season=season),
                    errors=[],
                    results=len(rows),
                    paging=dict(current=1, total=1),
                    response=rows,
                )
                result = link_fixtures(
                    session,
                    view,
                    league_id=league,
                    season=season,
                    aliases=aliases,
                    metadata=metadata,
                    config=config,
                )
                count += result["linked"]
                write_audit(audit, [dict(item, endpoint="fixtures") for item in result["audit"]])
                blocked = {
                    item["provider_fixture_id"]
                    for item in result["audit"]
                    if item["outcome"] == "rejected"
                }
                for row in rows:
                    identity = fixture_identity(row)
                    fixture_id = identity["provider_fixture_id"]
                    if fixture_id in blocked:
                        continue
                    index = session.get(ApifFixtureIndex, fixture_id)
                    if not index or any(
                        getattr(index, key) != identity[key]
                        for key in (
                            "provider_league_id",
                            "season",
                            "home_provider_id",
                            "away_provider_id",
                        )
                    ):
                        continue
                    match = session.get(Match, index.match_id)
                    if not match or (index.league, index.home_team_id, index.away_team_id) != (
                        match.league,
                        match.home_team_id,
                        match.away_team_id,
                    ):
                        reject(fixture_id, "同窗冲突", "既有索引主客身份与比赛不符", identity)
                        continue
                    if abs(instant(index.kickoff_utc) - identity["kickoff_utc"]) > timedelta(
                        hours=3
                    ):
                        reject(
                            fixture_id, "同窗冲突", "fixture 开球偏离既有索引超过三小时", identity
                        )
                        continue
                    previous = instant(
                        index.evidence.get("fixture_captured_utc", index.captured_utc)
                    )
                    if captured < previous:
                        continue
                    context = {key: row[key] for key in ("fixture", "league", "teams")}
                    if captured == previous:
                        if context == index.evidence.get("fixture"):
                            continue
                        if index.evidence.get("fixture") is not None:
                            reject(
                                fixture_id,
                                "同窗冲突",
                                "同一采集时刻 fixture 上下文不一致",
                                identity,
                            )
                            continue
                    index.evidence = {
                        **index.evidence,
                        "fixture": context,
                        "fixture_captured_utc": captured.isoformat(),
                        "fixture_raw_sha256": meta["sha256"],
                        "fixture_raw_location": location(path),
                    }
                    count += 1
            write_audit(audit, rejected)
    return count
