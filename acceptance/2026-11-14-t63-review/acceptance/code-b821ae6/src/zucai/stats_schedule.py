"""Deterministic technical-data units, persisted retries, and snapshot coverage."""

import json
from collections import defaultdict
from datetime import UTC, timedelta
from itertools import groupby
from pathlib import Path

from sqlalchemy import select

from zucai.db import get_engine, session_scope
from zucai.models import ApifFixtureIndex, Match, Odds
from zucai.sources.api_football import APIF_INDEX_LEAGUE_IDS, LEAGUES
from zucai.sources.apif_stats import instant
from zucai.sources.football_data import CONFIG_ROOT, LEAGUES_PATH, read_json
from zucai.sources.sporttery import BEIJING
from zucai.stats_client import ALLOWED_LEAGUES, DETAILS, INJURY_LEAGUES
from zucai.stats_link import provider_season

FD_CODES = ("E0", "E1", "SP1", "I1", "D1", "F1", "F2", "N1", "P1")
ORDER = ("index", "fd", *DETAILS, "injuries")


def plan(codes, seasons, endpoints, now):
    chosen = (endpoints or ",".join(ORDER)).split(",")
    if len(set(chosen)) != len(chosen) or not set(chosen) <= set(ORDER):
        raise ValueError("endpoints 仅限 index,fd,statistics,events,lineups,players,injuries")
    requested = codes.split(",") if codes else []
    if len(set(requested)) != len(requested):
        raise ValueError("leagues 不得重复")
    api = read_json(LEAGUES)
    fd = read_json(LEAGUES_PATH)
    if any(
        code not in FD_CODES and (not code.isdecimal() or int(code) not in ALLOWED_LEAGUES)
        for code in requested
    ):
        raise ValueError("技术数据 leagues 超出允许范围")
    league_ids = (
        sorted({int(code) for code in requested if code.isdecimal()})
        if requested
        else sorted(ALLOWED_LEAGUES)
    )
    fd_codes = [
        code
        for code in FD_CODES
        if not requested
        or code in requested
        or any(fd[code]["league"] == api[str(lid)]["league"] for lid in league_ids)
    ]
    years = []
    for part in (seasons or f"2022-{now.year}").split(","):
        pair = part.split("-")
        if len(pair) not in (1, 2) or any(not v.isdecimal() for v in pair):
            raise ValueError("seasons 须为 2022 起的年份或年份范围")
        first, last = int(pair[0]), int(pair[-1])
        if not 2022 <= first <= last <= now.year + (league_ids == [98]):
            raise ValueError("seasons 超出已开始赛季范围")
        years.extend(range(first, last + 1))
    if len(set(years)) != len(years):
        raise ValueError("seasons 不得重复")
    selection = dict(
        leagues=league_ids,
        fd_codes=fd_codes,
        seasons=sorted(years),
        endpoints=[key for key in ORDER if key in chosen],
    )
    if not seasons and 98 in league_ids:
        current = provider_season(instant(now), 98)
        if current not in years:
            selection["league_seasons"] = {"98": sorted({*years, current})}
    return selection


def _seasons(selection, league_id):
    return selection.get("league_seasons", {}).get(str(league_id), selection["seasons"])


def unit(endpoint, league_id, season, fixture_id="", *, league, phase="history", **extra):
    provider = "fd" if endpoint == "fd" else "apif"
    return dict(
        key=f"{provider}:{league_id}:{season}:{endpoint}:{fixture_id}:{phase}",
        provider=provider,
        endpoint=endpoint,
        league_id=league_id,
        league=league,
        season=season,
        fixture_id=str(fixture_id),
        phase=phase,
        **extra,
    )


def scope_key(row):
    return f"{row['league_id']}:{row['season']}:{row['endpoint']}"


def empty_cursor():
    return dict(version=1, records={}, unsupported={})


def resume_units(items, cursor, enabled):
    """Continue unfinished observations first without changing endpoint priority."""
    for _, group in groupby(items, key=lambda pair: pair[0]["endpoint"]):
        pairs = list(group)
        if enabled:

            def pending(pair):
                old = cursor["records"].get(pair[0]["key"])
                return old is not None and old["outcome"] not in {"ok", "unsupported", "missed"}

            pairs.sort(key=lambda pair: not pending(pair))
        yield from pairs


def load_cursor(path):
    cursor = json.loads(Path(path).read_bytes()) if Path(path).exists() else empty_cursor()
    if (
        cursor.get("version") != 1
        or not isinstance(cursor.get("records"), dict)
        or not isinstance(cursor.get("unsupported"), dict)
    ):
        raise ValueError("技术数据游标格式无效")
    for key, record in cursor["records"].items():
        if (
            record.get("unit", {}).get("key") != key
            or type(record.get("attempts")) is not int
            or record["attempts"] < 0
            or type(record.get("rejected_rows", 0)) is not int
            or record.get("rejected_rows", 0) < 0
        ):
            raise ValueError("技术数据游标记录无效")
        if record.get("last_attempt_utc"):
            instant(record["last_attempt_utc"])
    return cursor


def due(row, cursor, now):
    old = cursor["records"].get(row["key"], {})
    if row["phase"] == "pregame" and old.get("attempts", 0) > 0:
        return False
    if old.get("outcome") in {"ok", "unsupported", "missed"}:
        return False
    if old.get("empty_attempts", 0):
        if old["empty_attempts"] >= 3:
            return False
        if now < instant(old["last_empty_utc"]) + timedelta(days=7):
            return False
    return True


def record(cursor, row, outcome, now, *, requested=False, received=False, reason="", **extra):
    old = cursor["records"].get(row["key"], {})
    entry = {
        **old,
        "unit": row,
        "outcome": outcome,
        "reason": reason,
        "attempts": old.get("attempts", 0) + int(requested),
        "received": old.get("received", False) or received,
        "rejected_rows": old.get("rejected_rows", 0),
        **extra,
    }
    if requested:
        entry["last_attempt_utc"] = now.isoformat()
    if outcome == "empty":
        entry.update(
            empty_attempts=old.get("empty_attempts", 0) + 1, last_empty_utc=now.isoformat()
        )
    cursor["records"][row["key"]] = entry
    return entry


def unsupported(row, cursor, context=None):
    if scope_key(row) in cursor["unsupported"]:
        return cursor["unsupported"][scope_key(row)]
    wildcard = f"{row['league_id']}:{row['season']}:*"
    if wildcard in cursor["unsupported"]:
        return cursor["unsupported"][wildcard]
    if row["endpoint"] == "injuries" and row["league_id"] not in INJURY_LEAGUES:
        return "injuries 不在已确认的 11 联赛范围"
    rules = read_json(CONFIG_ROOT / "api_football/stats_coverage.json")["unsupported"]
    for rule in rules:
        if (
            rule["endpoint"].split("/")[-1] != row["endpoint"]
            or row["league_id"] not in rule["leagues"]
        ):
            continue
        if rule["seasons"] != "all" and row["season"] not in rule["seasons"]:
            continue
        if rule["stage"] == "qualification":
            round_name = ((context or {}).get("league", {}).get("round") or "").lower()
            if not any(
                word in round_name for word in ("qualifying", "qualification", "preliminary")
            ):
                continue
        return "T30 实测 unsupported；待人工复核"
    return None


def index_units(selection):
    leagues = read_json(LEAGUES)
    if "index" in selection["endpoints"]:
        for lid in selection["leagues"]:
            if lid in APIF_INDEX_LEAGUE_IDS:
                for season in selection["seasons"]:
                    yield unit("index", lid, season, league=leagues[str(lid)]["league"])


def fd_units(selection):
    leagues = read_json(LEAGUES_PATH)
    if "fd" in selection["endpoints"]:
        for code in selection["fd_codes"]:
            for season in selection["seasons"]:
                yield unit(
                    "fd",
                    code,
                    season,
                    league=leagues[code]["league"],
                    div=code,
                    fd_season=f"{season % 100:02}{(season + 1) % 100:02}",
                )


def match_units(database, selection, now, *, daily=False):
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            indexed = list(
                session.scalars(
                    select(ApifFixtureIndex).order_by(
                        ApifFixtureIndex.provider_league_id,
                        ApifFixtureIndex.season,
                        ApifFixtureIndex.kickoff_utc,
                        ApifFixtureIndex.provider_fixture_id,
                    )
                )
            )
            games = {m.match_id: m for m in session.scalars(select(Match))}
            sale_ids = set(
                session.scalars(select(Odds.match_id).where(Odds.pool == "had", Odds.line == 0))
            )
            # Explicit existing links only. Precompute both directions once instead of
            # scanning every Match again for each index and endpoint.
            sale_jc_ids = {
                m.match_id for m in games.values() if m.source == "jc" and m.match_id in sale_ids
            }
            sale_targets = sale_jc_ids | {
                m.counterpart_id
                for m in games.values()
                if m.match_id in sale_jc_ids and m.counterpart_id
            }
            yesterday = now.astimezone(BEIJING).date() - timedelta(days=1)
            for endpoint in DETAILS:
                if endpoint not in selection["endpoints"]:
                    continue
                for index in indexed:
                    if index.provider_league_id not in selection[
                        "leagues"
                    ] or index.season not in _seasons(selection, index.provider_league_id):
                        continue
                    game = games[index.match_id]
                    context = index.evidence.get("fixture")
                    item = unit(
                        endpoint,
                        index.provider_league_id,
                        index.season,
                        index.provider_fixture_id,
                        league=index.league,
                        match_id=index.match_id,
                        kickoff_utc=game.kickoff_utc,
                    )
                    kickoff = instant(game.kickoff_utc)
                    if game.status == "finished" and (
                        not daily or kickoff.astimezone(BEIJING).date() == yesterday
                    ):
                        yield item, context
                    if daily and endpoint == "lineups":
                        if (
                            game.match_id not in sale_targets
                            and game.counterpart_id not in sale_jc_ids
                        ):
                            continue
                        minutes = (kickoff - now).total_seconds() / 60
                        item = {
                            **item,
                            "key": item["key"].rsplit(":", 1)[0] + ":pregame",
                            "phase": "pregame",
                        }
                        if 30 <= minutes <= 60 and game.status == "scheduled":
                            yield item, context
                        elif minutes < 30 and kickoff.astimezone(BEIJING).date() >= yesterday:
                            yield {**item, "missed": True}, context
    finally:
        engine.dispose()


def injury_units(selection, now=None):
    leagues = read_json(LEAGUES)
    if "injuries" in selection["endpoints"]:
        for lid in selection["leagues"]:
            seasons = (
                [provider_season(instant(now), lid)]
                if now is not None
                else _seasons(selection, lid)
            )
            for season in seasons:
                yield unit("injuries", lid, season, league=leagues[str(lid)]["league"])


def coverage(cursor, now):
    groups = defaultdict(
        lambda: dict(
            expected=0,
            fetched=0,
            valid=0,
            null=0,
            failed=0,
            unsupported=0,
            requests=0,
            null_fields=0,
            missed=0,
            rejected_rows=0,
        )
    )
    for entry in cursor["records"].values():
        row = entry["unit"]
        key = (row["league"], row["season"], row["endpoint"], row["phase"])
        group = groups[key]
        group["expected"] += 1
        group["fetched"] += int(entry.get("received", False))
        group["requests"] += entry["attempts"]
        group["null_fields"] += entry.get("null_fields", 0)
        group["rejected_rows"] += entry.get("rejected_rows", 0)
        for status, field in (
            ("ok", "valid"),
            ("empty", "null"),
            ("error", "failed"),
            ("unsupported", "unsupported"),
            ("missed", "missed"),
        ):
            group[field] += entry["outcome"] == status
    return dict(
        version=1,
        observed_utc=now.astimezone(UTC).isoformat(),
        rows=[
            dict(league=league, season=season, endpoint=endpoint, phase=phase, **values)
            for (league, season, endpoint, phase), values in sorted(groups.items())
        ],
    )
