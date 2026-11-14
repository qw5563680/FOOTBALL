"""Pure T31 endpoint parsers. No acquisition, identity creation or model features."""

import copy
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime

PARSER_VERSION = "t31.1"
STAT_TYPES = {
    "Shots on Goal": ("shots_on_target", "count"),
    "Shots off Goal": ("shots_off_target", "count"),
    "Total Shots": ("shots", "count"),
    "Blocked Shots": ("shots_blocked", "count"),
    "Shots insidebox": ("shots_inside_box", "count"),
    "Shots outsidebox": ("shots_outside_box", "count"),
    "Fouls": ("fouls", "count"),
    "Corner Kicks": ("corners", "count"),
    "Offsides": ("offsides", "count"),
    "Ball Possession": ("possession", "%"),
    "Yellow Cards": ("yellow_cards", "count"),
    "Red Cards": ("red_cards", "count"),
    "Goalkeeper Saves": ("saves", "count"),
    "Total passes": ("passes", "count"),
    "Passes accurate": ("passes_accurate", "count"),
    "Passes %": ("pass_accuracy", "%"),
    "expected_goals": ("expected_goals", "goals"),
    "goals_prevented": ("goals_prevented", "goals"),
    "Free Kicks": ("free_kicks", "count"),
}


def instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("技术数据时间须含时区")
    return result.astimezone(UTC)


def identifier(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError("供应商 ID 须为正整数")
    return value


def envelope(raw, endpoint, parameters):
    data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(data, dict) or data.get("get") != endpoint:
        raise ValueError("响应 get 与端点不符")
    if data.get("errors") not in ([], {}):
        raise ValueError("响应 errors 非空或缺失")
    actual = data.get("parameters")
    if not isinstance(actual, dict) or any(
        str(actual.get(k)) != str(v) for k, v in parameters.items()
    ):
        raise ValueError("响应 fixture / league / season 与请求不符")
    rows = data.get("response")
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or type(data.get("results")) is not int
        or data["results"] != len(rows)
        or data.get("paging") != {"current": 1, "total": 1}
    ):
        raise ValueError("响应条数、分页或结构不完整")
    return rows


def number(raw, *, unit="count", negative=False):
    if raw is None:
        return None, "null"
    if raw == "":
        return None, "empty"
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None, "invalid_value"
    value = raw.strip() if isinstance(raw, str) else raw
    if unit == "%" and isinstance(value, str) and value.endswith("%"):
        value = value[:-1]
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None, "invalid_value"
    if not math.isfinite(value) or (value < 0 and not negative) or (unit == "%" and value > 100):
        return None, "invalid_value"
    return value, None


def fixture_identity(fixture):
    try:
        info, league, teams = fixture["fixture"], fixture["league"], fixture["teams"]
        identity = {
            "provider_fixture_id": str(identifier(info["id"])),
            "provider_league_id": identifier(league["id"]),
            "season": identifier(league["season"]),
            "kickoff_utc": instant(info["date"]),
            "status": info["status"]["short"],
            "home_provider_id": identifier(teams["home"]["id"]),
            "away_provider_id": identifier(teams["away"]["id"]),
        }
        if identity["home_provider_id"] == identity["away_provider_id"]:
            raise ValueError("供应商主客相同")
        if "timestamp" in info and info["timestamp"] != identity["kickoff_utc"].timestamp():
            raise ValueError("fixture 开球时间字段矛盾")
        return identity
    except (KeyError, TypeError) as exc:
        raise ValueError("fixture 身份字段不完整") from exc


def _common(identity, side, endpoint):
    status = identity["status"]
    period = (
        "90" if status == "FT" else "incl_extra_time" if status in {"AET", "PEN"} else "unknown"
    )
    return dict(
        provider="apif",
        provider_fixture_id=identity["provider_fixture_id"],
        endpoint=endpoint,
        provider_team_id=identity[f"{side}_provider_id"],
        side=side,
        period_basis=period,
        extra_time_included=period == "incl_extra_time",
        eligible_90=period == "90",
        parser_version=PARSER_VERSION,
        missing_reason=None,
    )


def _side(row, identity):
    team_id = identifier(row.get("team", {}).get("id"))
    for side in ("home", "away"):
        if team_id == identity[f"{side}_provider_id"]:
            return side
    raise ValueError("端点球队 ID 不属于 fixture 主客")


def _statistics(rows, identity):
    teams = {}
    for row in rows:
        side = _side(row, identity)
        if side in teams or not isinstance(row.get("statistics"), list):
            raise ValueError("statistics 球队重复或字段结构无效")
        values = {}
        for stat in row["statistics"]:
            if (
                not isinstance(stat, dict)
                or not isinstance(stat.get("type"), str)
                or "value" not in stat
            ):
                raise ValueError("statistics type/value 不完整")
            if stat["type"] in values:
                raise ValueError("statistics type 重复")
            values[stat["type"]] = stat["value"]
        teams[side] = values
    result = []
    for side in ("home", "away"):
        values = teams.get(side, {})
        for label, (key, unit) in STAT_TYPES.items():
            if label == "Free Kicks" and label not in values:
                continue
            raw = values.get(label)
            value, missing = number(raw, unit=unit, negative=key == "goals_prevented")
            if label not in values:
                missing = "not_provided"
            if key == "expected_goals" and value is None:
                missing = (
                    "not_provided_before_2024" if identity["season"] < 2024 else "not_provided"
                )
            result.append(
                dict(
                    **_common(identity, side, "statistics"),
                    record_key=f"{side}:{key}",
                    stat_key=key,
                    raw_value=raw,
                    value=value,
                    unit=unit,
                )
            )
            result[-1]["missing_reason"] = missing
    return result


def _events(rows, identity):
    result = []
    for index, raw in enumerate(rows):
        side = _side(raw, identity)
        clock = raw.get("time", {})
        elapsed, extra = clock.get("elapsed"), clock.get("extra")
        if any(v is not None and (type(v) is not int or v < 0) for v in (elapsed, extra)):
            raise ValueError("事件时间须为非负整数或 null")
        if not isinstance(raw.get("type"), str):
            raise ValueError("事件 type 缺失")
        player, assist = raw.get("player") or {}, raw.get("assist") or {}
        value = copy.deepcopy(raw)
        value["total_minute"] = None if elapsed is None else elapsed + (extra or 0)
        result.append(
            dict(
                **_common(identity, side, "events"),
                record_key=f"event:{index}",
                event_type=raw["type"],
                detail=raw.get("detail"),
                elapsed=elapsed,
                extra=extra,
                player_id=identifier(player.get("id"), nullable=True),
                assist_id=identifier(assist.get("id"), nullable=True),
                raw_value=raw,
                value=value,
            )
        )
    return result


def _numeric_tree(value):
    if isinstance(value, dict):
        return {k: _numeric_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_numeric_tree(v) for v in value]
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?%?", value):
        return float(value.rstrip("%"))
    return value


def _lineups(rows, identity, endpoint, captured):
    result, seen = [], set()
    for row in rows:
        side = _side(row, identity)
        if side in seen:
            raise ValueError("阵容球队重复")
        seen.add(side)
        common = dict(
            **_common(identity, side, endpoint),
            formation=row.get("formation"),
            captured_before_kickoff=captured < identity["kickoff_utc"],
            minutes=None,
            rating=None,
            position=None,
        )
        if endpoint == "lineups":
            meta = {k: row.get(k) for k in ("team", "coach", "formation")}
            result.append(
                dict(
                    **common,
                    record_key=f"{side}:team",
                    role="team",
                    player_id=None,
                    raw_value=meta,
                    value=meta,
                )
            )
            entries = []
            for field, role in (("startXI", "start"), ("substitutes", "bench")):
                if not isinstance(row.get(field), list):
                    raise ValueError("阵容名单字段缺失")
                entries.extend((role, player) for player in row[field])
        else:
            if not isinstance(row.get("players"), list):
                raise ValueError("players 字段缺失")
            entries = [("player_stats", player) for player in row["players"]]
        for role, player in entries:
            pid = identifier(player.get("player", {}).get("id"))
            normalized = copy.deepcopy(player)
            fields = common.copy()
            if endpoint == "players":
                stats = player.get("statistics")
                if (
                    not isinstance(stats, list)
                    or len(stats) > 1
                    or any(not isinstance(s, dict) for s in stats)
                ):
                    raise ValueError("球员 statistics 结构无法唯一解释")
                normalized["statistics"] = _numeric_tree(stats)
                games = stats[0].get("games") or {} if stats else {}
                fields.update(
                    minutes=number(games.get("minutes"))[0],
                    rating=number(games.get("rating"))[0],
                    position=games.get("position"),
                )
            else:
                fields["position"] = player["player"].get("pos")
            result.append(
                dict(
                    **fields,
                    record_key=f"{side}:{role}:{pid}",
                    role=role,
                    player_id=pid,
                    raw_value=player,
                    value=normalized,
                )
            )
    if len({row["record_key"] for row in result}) != len(result):
        raise ValueError("阵容或球员重复")
    return result


def parse_endpoint(raw, *, endpoint, fixture, captured_utc, rejected=None):
    """Preserve source values; infer only documented units and status-based period."""
    if endpoint not in {"statistics", "events", "lineups", "players"}:
        raise ValueError("不支持的技术数据端点")
    identity = fixture_identity(fixture)
    captured = instant(captured_utc)
    rows = envelope(raw, f"fixtures/{endpoint}", {"fixture": identity["provider_fixture_id"]})
    if rejected is not None:
        parsed = []
        teams = Counter(
            row.get("team", {}).get("id") for row in rows if isinstance(row.get("team"), dict)
        )
        for index, row in enumerate(rows):
            try:
                side = _side(row, identity)
                if endpoint != "events" and teams[identity[f"{side}_provider_id"]] != 1:
                    raise ValueError("同一球队出现多行")
                if endpoint == "statistics":
                    parsed.extend(r for r in _statistics([row], identity) if r["side"] == side)
                elif endpoint == "events":
                    event = _events([row], identity)[0]
                    event["record_key"] = f"event:{index}"
                    parsed.append(event)
                else:
                    parsed.extend(_lineups([row], identity, endpoint, captured))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                rejected.append({"row_number": index + 1, "reason": str(exc)})
        if endpoint == "statistics":
            absent = {
                side for side in ("home", "away") if identity[f"{side}_provider_id"] not in teams
            }
            parsed.extend(r for r in _statistics([], identity) if r["side"] in absent)
        return parsed
    if endpoint == "statistics":
        return _statistics(rows, identity)
    if endpoint == "events":
        return _events(rows, identity)
    return _lineups(rows, identity, endpoint, captured)
