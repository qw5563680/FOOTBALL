"""Pure T30 sampling and response audits; no match import or model features."""

import json
from collections import Counter

from zucai.sources.fd_audit import append_unique

DETAIL_ENDPOINTS = (
    "fixtures/statistics",
    "fixtures/events",
    "fixtures/lineups",
    "fixtures/players",
)
FIELDS = (
    "run_id",
    "observed_utc",
    "league",
    "season",
    "fixture_id",
    "match_id",
    "fixture_status",
    "endpoint",
    "parameters",
    "response_get",
    "http",
    "errors",
    "results",
    "paging",
    "coverage",
    "field_paths",
    "statistic_types",
    "expected_goals_present",
    "expected_goals_non_null",
    "null_fields",
    "period_basis",
    "raw_sha256",
    "elapsed_ms",
    "requests",
    "remaining_quota",
    "outcome",
    "reason",
)


def sample_fixtures(rows, per_scope, known_ids):
    """One from each status group first, then fill; known source IDs win within a group."""
    if type(per_scope) is not int or per_scope < 1:
        raise ValueError("per_scope 须为正整数")
    ids = Counter(row.get("fixture", {}).get("id") for row in rows if isinstance(row, dict))
    groups = []
    for states in ({"FT"}, {"AET", "PEN"}, {"PST", "CANC", "Canc"}):
        candidates = [
            row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("fixture"), dict)
            and type(row["fixture"].get("id")) is int
            and row["fixture"]["id"] > 0
            and ids[row["fixture"]["id"]] == 1
            and row["fixture"].get("status", {}).get("short") in states
        ]
        candidates.sort(
            key=lambda row: (str(row["fixture"]["id"]) not in known_ids, row["fixture"]["id"])
        )
        groups.append(candidates)
    selected = [group[0] for group in groups if group]
    selected.extend(row for group in groups for row in group[1:])
    return selected[:per_scope]


def _fields(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _fields(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for child in value:
            yield from _fields(child, prefix + "[]")
    else:
        yield prefix, value


def _entity(value):
    return isinstance(value, dict) and (
        (type(value.get("id")) is int and value["id"] > 0)
        or (isinstance(value.get("name"), str) and bool(value["name"].strip()))
    )


def _detail_payload(endpoint, rows, parameters):
    """Validate record structure, without interpreting discovered technical fields."""
    invalid = ("error", "端点响应结构或所属联赛季不符；保留原件待核")
    if any(not _entity(row.get("team")) for row in rows):
        return invalid
    has_data = False
    for row in rows:
        if endpoint == "fixtures/events":
            if (
                not isinstance(row.get("time"), dict)
                or "elapsed" not in row["time"]
                or not isinstance(row.get("type"), str)
                or not row["type"].strip()
            ):
                return invalid
            has_data = True
        elif endpoint == "fixtures/lineups":
            if any(not isinstance(row.get(key), list) for key in ("startXI", "substitutes")):
                return invalid
            players = row["startXI"] + row["substitutes"]
            if any(not isinstance(p, dict) or not _entity(p.get("player")) for p in players):
                return invalid
            has_data |= bool(players)
        elif endpoint == "fixtures/players":
            if not isinstance(row.get("players"), list):
                return invalid
            for player in row["players"]:
                if (
                    not isinstance(player, dict)
                    or not _entity(player.get("player"))
                    or not isinstance(player.get("statistics"), list)
                    or any(not isinstance(s, dict) for s in player["statistics"])
                ):
                    return invalid
                has_data |= any(v is not None and v != "" for _, v in _fields(player["statistics"]))
        elif endpoint == "injuries":
            if (
                not _entity(row.get("player"))
                or not isinstance(row["player"].get("type"), str)
                or not row["player"]["type"].strip()
                or not isinstance(row.get("fixture"), dict)
                or type(row["fixture"].get("id")) is not int
                or row["fixture"]["id"] < 1
                or not isinstance(row.get("league"), dict)
                or str(row["league"].get("id")) != parameters["league"]
                or str(row["league"].get("season")) != parameters["season"]
            ):
                return invalid
            has_data = True
        else:
            return invalid
    return ("ok", "") if has_data else ("empty", "只有元数据，没有有效阵容或球员统计")


def parse_response(raw, endpoint, parameters):
    """Describe the observed response, explicitly rejecting errors and scope mismatches."""
    result = dict(
        response_get="",
        errors="",
        results="",
        paging="",
        coverage="",
        field_paths="[]",
        statistic_types="[]",
        expected_goals_present=False,
        expected_goals_non_null=False,
        null_fields=0,
        outcome="error",
        reason="响应结构、get、参数或第一页范围不符",
    )
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError):
        return result
    if not isinstance(doc, dict):
        return result
    result.update(
        response_get=doc.get("get", ""),
        errors=json.dumps(doc.get("errors"), ensure_ascii=False),
        results=doc.get("results", ""),
        paging=json.dumps(doc.get("paging")),
    )
    if doc.get("errors") not in ([], {}):
        errors = doc.get("errors")
        categories = ",".join(sorted(errors)) if isinstance(errors, dict) else "未知类别"
        result["reason"] = f"API errors 非空或缺失（{categories}）；详见 errors 列与原件"
        return result
    rows, paging, params = doc.get("response"), doc.get("paging"), doc.get("parameters")
    if (
        doc.get("get") != endpoint
        or not isinstance(rows, list)
        or type(doc.get("results")) is not int
        or doc["results"] != len(rows)
        or not isinstance(params, dict)
        or {k: str(v) for k, v in params.items()} != parameters
        or not isinstance(paging, dict)
        or type(paging.get("current")) is not int
        or paging["current"] != 1
        or type(paging.get("total")) is not int
        or paging["total"] < 1
        or any(not isinstance(row, dict) for row in rows)
    ):
        return result
    leaves = list(_fields(rows))
    result.update(
        field_paths=json.dumps(sorted({key for key, _ in leaves}), ensure_ascii=False),
        null_fields=sum(value is None or value == "" for _, value in leaves),
    )
    if not rows:
        result.update(outcome="empty", reason="响应为空；不证明无权限或整个赛季无数据")
        return result
    if not leaves:
        return result
    if endpoint == "leagues":
        if (
            len(rows) != 1
            or not isinstance(rows[0].get("league"), dict)
            or str(rows[0]["league"].get("id")) != parameters["id"]
        ):
            return result
        seasons = rows[0].get("seasons")
        if not isinstance(seasons, list) or any(
            not isinstance(row, dict) or type(row.get("year")) is not int for row in seasons
        ):
            return result
        result["coverage"] = json.dumps(
            {row["year"]: row.get("coverage") for row in seasons},
            ensure_ascii=False,
            sort_keys=True,
        )
    elif endpoint == "fixtures":
        for row in rows:
            league, match = row.get("league"), row.get("fixture")
            if (
                not isinstance(league, dict)
                or not isinstance(match, dict)
                or str(league.get("id")) != parameters["league"]
                or str(league.get("season")) != parameters["season"]
                or type(match.get("id")) is not int
                or match["id"] < 1
                or not isinstance(match.get("status"), dict)
                or not isinstance(match["status"].get("short"), str)
            ):
                return result
    elif endpoint == "fixtures/statistics":
        if any(
            not _entity(row.get("team")) or not isinstance(row.get("statistics"), list)
            for row in rows
        ):
            return result
        stats = [stat for row in rows for stat in row["statistics"]]
        if any(
            not isinstance(stat, dict)
            or not isinstance(stat.get("type"), str)
            or "value" not in stat
            for stat in stats
        ):
            return result
        xg = [stat["value"] for stat in stats if stat["type"] == "expected_goals"]
        result.update(
            statistic_types=json.dumps(
                sorted({stat["type"] for stat in stats}), ensure_ascii=False
            ),
            expected_goals_present=bool(xg),
            expected_goals_non_null=any(value is not None and value != "" for value in xg),
        )
        if not any(stat["value"] is not None and stat["value"] != "" for stat in stats):
            result.update(outcome="empty", reason="统计值均为空；保留原始空值")
            return result
    else:
        outcome, reason = _detail_payload(endpoint, rows, parameters)
        result.update(outcome=outcome, reason=reason)
        return result
    result.update(outcome="ok", reason="")
    return result


def append_audit(path, row):
    """Retrying the same observation preserves first provenance; distinct runs remain evidence."""
    append_unique(
        path,
        FIELDS,
        [row],
        lambda r: tuple(
            r.get(k, "")
            for k in (
                "run_id",
                "league",
                "season",
                "fixture_id",
                "endpoint",
                "parameters",
                "raw_sha256",
                "outcome",
                "reason",
            )
        ),
    )
