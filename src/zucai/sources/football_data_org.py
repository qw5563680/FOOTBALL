"""Authenticated football-data.org v4 match downloads and credential-free replay."""

import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from zucai.sources.api_common import ApiGame
from zucai.sources.base import RateLimiter, RawStore, Source, make_session
from zucai.sources.football_data import CONFIG_ROOT, read_json

COMPETITIONS_PATH = CONFIG_ROOT / "football_data_org/competitions.json"
API = "https://api.football-data.org/v4"
# v4 league lookup table. The same filter can be represented by code or by numeric ID.
LEAGUE_CODES = {
    "2021": "PL",
    "2016": "ELC",
    "2014": "PD",
    "2019": "SA",
    "2002": "BL1",
    "2015": "FL1",
    "2003": "DED",
    "2017": "PPL",
    "2013": "BSA",
}
QUOTA_HEADERS = (
    "x-requests-available-minute",
    "x-requestsavailable-minute",
    "x-requestsavailable",
    "x-requests-available",
)
RESET_HEADER = "x-requestcounter-reset"
STATUSES = {
    "SCHEDULED",
    "TIMED",
    "IN_PLAY",
    "PAUSED",
    "EXTRA_TIME",
    "PENALTY_SHOOTOUT",
    "FINISHED",
    "POSTPONED",
    "SUSPENDED",
    "CANCELLED",
    "AWARDED",
}


class FdorgHttpError(ValueError):
    def __init__(self, status_code: int, detail=""):
        self.status_code = status_code
        super().__init__(
            f"football-data.org HTTP {status_code}" + (f"（{detail}）" if detail else "")
        )


@dataclass(frozen=True)
class _Scope:
    codes: tuple[str, ...]
    season: str | None
    date_from: str | None
    date_to: str | None
    status: str | None

    @property
    def url(self):
        if self.season is not None:
            path = f"/competitions/{self.codes[0]}/matches"
            params = {"season": self.season}
        else:
            path = "/matches"
            params = dict(
                dateFrom=self.date_from, dateTo=self.date_to, competitions=",".join(self.codes)
            )
        if self.status:
            params["status"] = self.status
        return f"{API}{path}?{urlencode(params, safe=',')}"

    def check_game(self, game, actual_season):
        if self.status and game.status != self.status:
            raise ValueError("比赛状态与请求不符")
        if self.season is not None:
            if actual_season != self.season:
                raise ValueError("无法核实比赛属于所请求赛季")
        elif not self.date_from <= game.kickoff_utc.date().isoformat() < self.date_to:
            raise ValueError("UTC 开球日期超出请求窗口")


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("日期必须为有效的 YYYY-MM-DD") from None
    if value != parsed.isoformat():
        raise ValueError("日期必须为有效的 YYYY-MM-DD")
    return parsed


def _kickoff(value):
    try:
        when = datetime.fromisoformat(value)
        if when.utcoffset() is None:
            raise ValueError
        return when.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("utcDate 缺失或不是带时区的开球时间") from None


def _code(row, fallback=None):
    if "competition" not in row:
        return fallback
    competition = row["competition"]
    code = competition.get("code") if isinstance(competition, dict) else None
    return code if isinstance(code, str) else None


def _season(row):
    season = row.get("season")
    if season is None:
        return None
    if not isinstance(season, dict):
        raise ValueError("比赛 season 字段无效")
    start = season.get("startDate")
    return _date(start).isoformat()[:4] if start is not None else None


def _filter_items(value):
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or any(type(v) not in (str, int) for v in items):
        raise ValueError("响应筛选字段无效")
    return {str(v).strip() for v in items if str(v).strip()}


def _document(raw, scope):
    """Whole-file scope contradictions abort the batch; malformed records go to the row audit."""
    try:
        doc = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("响应不是有效 JSON") from None
    if not isinstance(doc, dict) or "errorCode" in doc or not isinstance(doc.get("matches"), list):
        raise ValueError("响应缺少有效的 matches 列表")
    rows = doc["matches"]
    result = doc.get("resultSet", {})
    if not isinstance(result, dict) or (
        "count" in result and (type(result["count"]) is not int or result["count"] != len(rows))
    ):
        raise ValueError("resultSet.count 与实际行数不符")
    filters = doc.get("filters", {})
    if not isinstance(filters, dict):
        raise ValueError("响应 filters 无效")
    top = _code(doc)
    if "competition" in doc and not top:
        raise ValueError("响应顶层赛事缺少代码")
    if scope:
        if top is not None and top not in scope.codes:
            raise ValueError("响应顶层赛事与请求不符")
        for key, expected in (
            ("season", scope.season),
            ("dateFrom", scope.date_from),
            ("dateTo", scope.date_to),
        ):
            if expected is not None and key in filters and str(filters[key]) != expected:
                raise ValueError("响应 filters 与请求不符")
        if filters.get("competitions") is not None:
            supplied = _filter_items(filters["competitions"])
            if {LEAGUE_CODES.get(item, item) for item in supplied} != set(scope.codes):
                raise ValueError("响应 filters.competitions 与请求不符")
        if filters.get("status") is not None:
            expected = {scope.status} if scope.status else set()
            if _filter_items(filters["status"]) != expected:
                raise ValueError("响应 filters.status 与请求不符")
    return rows, top, str(filters.get("season", ""))


def _id(value):
    return value if type(value) is int and value > 0 else None


def _team(item):
    if not isinstance(item, dict) or _id(item.get("id")) is None:
        raise ValueError("球队数字 ID 缺失或无效")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("球队名称缺失")
    return item["id"], name.strip()


def _goals(score, field, *, optional=False):
    pair = score.get(field)
    if optional and pair is None:
        return None
    if isinstance(pair, dict):
        home, away = pair.get("home"), pair.get("away")
        if optional and home is None and away is None:
            return None
        if all(type(goals) is int and goals >= 0 for goals in (home, away)):
            return home, away
    raise ValueError(f"{field} 缺少完整的非负整数比分")


def _scores(row, state):
    if state != "FINISHED":
        return None, None, "missing"
    score = row.get("score")
    if not isinstance(score, dict):
        raise ValueError("完赛场次缺少 score")
    duration = score.get("duration")
    if duration == "REGULAR":
        full, basis = _goals(score, "fullTime"), "regular"
    elif duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
        full, basis = _goals(score, "regularTime"), "derived"
    else:
        raise ValueError("score.duration 未能确认常规时间比分")
    half = _goals(score, "halfTime", optional=True)
    if half is not None and (half[0] > full[0] or half[1] > full[1]):
        raise ValueError("半场比分大于常规时间比分")
    return full, half, basis


def _header_count(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value.strip(" \t")):
        return None
    try:
        return int(value)
    except ValueError:  # Python refuses excessively long decimal strings.
        return None


def _rate_headers(headers, token):
    selected = {}
    for key, value in headers.items():
        if key.lower() not in (*QUOTA_HEADERS, RESET_HEADER):
            continue
        count = _header_count(value)
        if count is not None and token not in value and token not in key:
            selected[key] = count
    return selected


def _reset_seconds(headers):
    if RESET_HEADER not in headers:
        return 60
    reset = _header_count(headers[RESET_HEADER])
    if reset is None:
        raise FdorgHttpError(429, "重置秒数必须为无符号整数，已停止重试")
    return reset


def _reject_json_constant(_):
    raise ValueError("非标准 JSON 数值")


def safe_json_for_archive(raw, token):
    """Require an intact UTF-8 JSON document containing no raw or decoded credential."""
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]+", token):
        return False
    if token.encode("ascii") in raw:
        return False
    try:
        # Inspect the complete UTF-8 JSON, including values shadowed by duplicate keys.
        values = [
            json.loads(
                raw.decode("utf-8"), object_pairs_hook=list, parse_constant=_reject_json_constant
            )
        ]
        while values:
            value = values.pop()
            if isinstance(value, str):
                value.encode("utf-8")  # Reject lone surrogates introduced by JSON escapes.
                if token in value:
                    return False
            elif isinstance(value, (list, tuple)):
                values.extend(value)
    except (ValueError, UnicodeError, RecursionError):
        return False
    return True


class FootballDataOrg(Source[ApiGame]):
    name = "football-data-org"
    limiter = RateLimiter(6.0)

    def __init__(self, store=None, session=None, competitions=None, *, sleep=time.sleep):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        if session is None:
            # Each actual GET must count, including the one permitted retry after 429.
            self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.competitions = read_json(COMPETITIONS_PATH) if competitions is None else competitions
        self.request_count = 0
        self.remaining_quota: int | None = None
        self.rate_events: list[dict] = []
        self.rejected: list[dict] = []
        self._sleep = sleep

    def _league(self, code):
        if isinstance(code, str) and code in LEAGUE_CODES.values():
            entry = self.competitions.get(code)
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("league"), str)
                and entry["league"].strip()
            ):
                return entry["league"]
        raise ValueError("赛事不在已配置的九个联赛中")

    def _scope(self, competition, season, date_from, date_to, status):
        if not isinstance(competition, str):
            raise ValueError("缺少赛事代码")
        codes = tuple(c.strip() for c in competition.split(","))
        for code in codes:
            self._league(code)
        if len(set(codes)) != len(codes):
            raise ValueError("赛事代码重复")
        if status is not None and (not isinstance(status, str) or status not in STATUSES):
            raise ValueError("无效的比赛状态筛选")
        if season is not None:
            if (
                len(codes) != 1
                or date_from is not None
                or date_to is not None
                or not re.fullmatch(r"[0-9]{4}", str(season))
                or int(season) == 0
            ):
                raise ValueError("赛季请求必须使用一个赛事、四位年份，且不能混用日期")
            season = str(season)
        elif _date(date_from) >= _date(date_to):
            raise ValueError("dateTo 必须晚于 dateFrom")
        return _Scope(codes, season, date_from, date_to, status)

    def request_url(self, *, competition, season=None, date_from=None, date_to=None, status=None):
        return self._scope(competition, season, date_from, date_to, status).url

    def fetch_raw(
        self, *, competition, season=None, date_from=None, date_to=None, status=None
    ) -> Path:
        scope = self._scope(competition, season, date_from, date_to, status)
        url = scope.url
        token = os.environ.get("FOOTBALL_DATA_ORG_TOKEN", "").strip()
        if not token or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise ValueError("请设置有效的 FOOTBALL_DATA_ORG_TOKEN")
        retried = False
        while True:
            self.limiter.wait()
            self.request_count += 1
            self.remaining_quota = None
            try:
                response = self.session.get(
                    url, headers={"X-Auth-Token": token}, allow_redirects=False
                )
            except (requests.RequestException, OSError):
                # Transport exceptions may contain the request and its headers.
                raise ValueError("football-data.org 网络请求失败") from None
            seen = datetime.now(UTC)
            rates = _rate_headers(response.headers, token)
            self.rate_events.append(
                {
                    "status_code": response.status_code,
                    "fetched_at_utc": seen.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    "headers": rates,
                }
            )
            by_name = {key.lower(): value for key, value in rates.items()}
            self.remaining_quota = next((by_name[k] for k in QUOTA_HEADERS if k in by_name), None)
            if (
                response.url != url
                or token in url
                or not safe_json_for_archive(response.content, token)
            ):
                raise ValueError(f"football-data.org HTTP {response.status_code} 响应未能安全归档")
            path = self.store.save_response(
                self.name, "matches", response, "json", captured_at=seen
            )
            if response.status_code == 200:
                self._parse(response.content, scope)
                return path
            if response.status_code != 429 or retried:
                raise FdorgHttpError(response.status_code)
            self._sleep(_reset_seconds(response.headers))
            retried = True

    def parse(
        self,
        raw: bytes,
        *,
        competition=None,
        season=None,
        date_from=None,
        date_to=None,
        status=None,
    ) -> list[ApiGame]:
        self.rejected = []
        args = (competition, season, date_from, date_to, status)
        scope = self._scope(*args) if any(v is not None for v in args) else None
        games, self.rejected = self._parse(raw, scope)
        return games

    def _parse(self, raw, scope):
        rows, top, season_filter = _document(raw, scope)
        digest = hashlib.sha256(raw).hexdigest()
        ids = Counter(_id(row.get("id")) for row in rows if isinstance(row, dict))
        games, rejected = [], []
        for n, row in enumerate(rows, 1):
            match_id = _id(row.get("id")) if isinstance(row, dict) else None
            code = _code(row, top) if isinstance(row, dict) else None
            if code and ((top and code != top) or (scope and code not in scope.codes)):
                raise ValueError("比赛赛事与请求或顶层赛事不符")
            try:
                if match_id is None:
                    raise ValueError("比赛数字 ID 缺失或无效")
                if ids[match_id] > 1:
                    raise ValueError("响应内重复比赛 ID")
                season = _season(row)
                game = self._game(row, match_id, code)
            except ValueError as exc:
                rejected.append(
                    dict(
                        row_number=n, provider_match_id=match_id, raw_sha256=digest, reason=str(exc)
                    )
                )
                continue
            if scope:
                scope.check_game(game, season or season_filter)
            games.append(game)
        return games, rejected

    def _game(self, row, match_id, code):
        league = self._league(code)
        when = _kickoff(row.get("utcDate"))
        state = row.get("status")
        if not isinstance(state, str) or state not in STATUSES:
            raise ValueError("无效的比赛状态")
        if state == "AWARDED":
            raise ValueError("AWARDED 判罚结果")
        home_id, home = _team(row.get("homeTeam"))
        away_id, away = _team(row.get("awayTeam"))
        if home_id == away_id or home.casefold() == away.casefold():
            raise ValueError("主客队身份相同")
        full, half, basis = _scores(row, state)
        return ApiGame(
            "fdorg",
            match_id,
            code,
            league,
            when,
            state,
            home,
            away,
            home_id,
            away_id,
            full,
            half,
            basis,
        )
