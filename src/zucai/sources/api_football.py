"""API-Sports direct v3 fixtures: safe archives, strict scope, regulation-time scores."""

import hashlib
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from datetime import date as Date
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from zucai.sources.api_common import ApiGame, LocalValidationError
from zucai.sources.apif_quota import DailyQuota
from zucai.sources.base import RateLimiter, RawStore, Source, _atomic_write
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.football_data_org import _goals, _id, _team, safe_json_for_archive

API = "https://v3.football.api-sports.io"
LEAGUES = CONFIG_ROOT / "api_football/leagues.json"
APIF_LEAGUE_IDS = frozenset({3, 848, 98, 292, 188, 113, 103, 244, 119, 253, 169})
APIF_IDENTITY_LEAGUE_IDS = frozenset({39, 140, 135, 78, 61, 88, 94})
APIF_INDEX_LEAGUE_IDS = APIF_IDENTITY_LEAGUE_IDS | {2}
STATES = {
    "FT",
    "AET",
    "PEN",
    "PST",
    "CANC",
    "ABD",
    "AWD",
    "WO",
    "NS",
    "TBD",
    "1H",
    "HT",
    "2H",
    "ET",
    "BT",
    "P",
    "SUSP",
    "INT",
    "LIVE",
}
FINISHED = {"FT", "AET", "PEN"}


class ApifResponseError(LocalValidationError):
    """Only safely archived plan/access error text may be included in cloud summaries."""


def check_api_errors(doc, raw):
    if isinstance(doc, dict) and doc.get("errors") in ([], {}):
        return
    errors = doc.get("errors") if isinstance(doc, dict) else None
    token = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if isinstance(errors, dict) and token and safe_json_for_archive(raw, token):
        messages = [
            f"errors.{k}: {errors[k]}" for k in ("plan", "access") if isinstance(errors.get(k), str)
        ]
        if messages:
            # Keep the upstream wording; escape controls when rendering a log/Markdown line.
            raise ApifResponseError("API-Football API 错误：" + "; ".join(messages))
    raise LocalValidationError("API-Football API 错误（权限、限流或参数），已保留安全归档")


class ApiFootball(Source[ApiGame]):
    name = "api-football"
    limiter = RateLimiter(6.1)

    def __init__(
        self,
        store=None,
        session=None,
        leagues=None,
        *,
        quota_path="data/api_football_quota.json",
        reserve=10,
        min_interval=None,
        identity_only=False,
    ):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.mount("https://", HTTPAdapter(max_retries=0))
            self.session.headers["User-Agent"] = "ZucaiResearch/0.1"
        configured = read_json(LEAGUES) if leagues is None else leagues
        self.leagues = {
            code: config
            for code, config in configured.items()
            if bool(config.get("identity_only", False)) == identity_only
            and not config.get("index_only", False)
        }
        self.quota = DailyQuota(quota_path, reserve)
        self.limiter = self.limiter if min_interval is None else RateLimiter(min_interval)
        self.request_count = 0
        self.rejected = []
        self.pages = []

    @property
    def remaining_quota(self):
        return self.quota.remaining

    def _scope(self, league_id=None, season=None, date=None):
        if date is not None:
            try:
                parsed = Date.fromisoformat(date)
            except (ValueError, TypeError):
                raise LocalValidationError("--date 须为 YYYY-MM-DD") from None
            if parsed.isoformat() != date or league_id is not None or season is not None:
                raise LocalValidationError("--date 不能与联赛或赛季混用")
            return {"date": date}
        if (
            str(league_id) not in self.leagues
            or type(season) is not int
            or not 1900 <= season <= 9999
        ):
            raise LocalValidationError("赛季请求须指定已配置的 --league 和四位 --season")
        return {"league": str(league_id), "season": str(season)}

    def request_url(self, *, league_id=None, season=None, date=None, page=None):
        params = self._scope(league_id, season, date)
        if page is not None:
            if type(page) is not int or page < 1:
                raise LocalValidationError("无效分页")
            params["page"] = str(page)
        return API + "/fixtures?" + urlencode(params)

    @staticmethod
    def index_scope(*, league_id, season):
        if (
            type(league_id) is not int
            or league_id not in APIF_INDEX_LEAGUE_IDS
            or type(season) is not int
            or not 2022 <= season <= datetime.now(UTC).year
        ):
            raise LocalValidationError("索引请求仅限七个桥接联赛和欧冠，以及 2022 起的已开始赛季")
        return {"league": str(league_id), "season": str(season)}

    def fetch_index_raw(self, *, league_id, season) -> Path:
        """Archive one index-only fixture page without widening the importer scope."""
        scope = self.index_scope(league_id=league_id, season=season)
        return self._request(API + "/fixtures?" + urlencode(scope))

    def _request(self, url, *, name="fixtures"):
        token = os.environ.get("API_FOOTBALL_KEY", "").strip()
        if not token or not re.fullmatch(r"[!-~]+", token):
            raise LocalValidationError("请设置有效的 API_FOOTBALL_KEY")
        self.limiter.wait()
        with self.quota.locked():
            self.quota.before_request()
            self.request_count += 1
            try:
                response = self.session.get(
                    url, headers={"x-apisports-key": token}, allow_redirects=False, timeout=30
                )
            except (requests.RequestException, OSError):
                raise LocalValidationError("API-Football 网络请求失败，已计入本地配额") from None
            seen = datetime.now(UTC)
            # Numeric quota headers remain authoritative even when the body cannot be archived.
            quota_error = None
            try:
                self.quota.observe(response.headers)
            except ValueError as exc:
                quota_error = exc
            # Check the body before creating a raw file or sidecar.
            if (
                response.url != url
                or token in url
                or not safe_json_for_archive(response.content, token)
            ):
                raise LocalValidationError("API-Football 响应未能安全归档")
            path = self.store.save_response(self.name, name, response, "json", captured_at=seen)
            meta = self.store.load_metadata(path)
            # Only decimal quota headers are retained; never archive authentication headers.
            meta["quota_headers"] = {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower()
                in (
                    "x-ratelimit-requests-limit",
                    "x-ratelimit-requests-remaining",
                    "x-ratelimit-limit",
                    "x-ratelimit-remaining",
                )
                and isinstance(v, str)
                and v.isascii()
                and v.isdecimal()
                and token not in v
            }
            meta["quota_headers_valid"] = quota_error is None
            _atomic_write(path.with_suffix(path.suffix + ".meta.json"), json.dumps(meta).encode())
        self.pages.append(path)
        if quota_error is not None:
            raise quota_error
        if response.status_code != 200:
            doc = json.loads(path.read_bytes())
            errors = doc.get("errors") if isinstance(doc, dict) else None
            if isinstance(errors, dict) and any(
                isinstance(errors.get(key), str) for key in ("plan", "access")
            ):
                check_api_errors(doc, path.read_bytes())
            raise LocalValidationError(f"API-Football HTTP {response.status_code}，已保留安全归档")
        return path

    def status(self):
        # Keep the body observation in the same lock as reservation and header processing.
        with self.quota.locked():
            path = self._request(API + "/status", name="status")
            raw = path.read_bytes()
            doc = json.loads(raw)
            check_api_errors(doc, raw)
            if doc.get("get") != "status":
                raise LocalValidationError("API-Football status 响应端点不符")
            meta = self.store.load_metadata(path)
            self.quota.observe_status(doc, meta["quota_headers"])
            return dict(
                plan=doc["response"]["subscription"]["plan"],
                limit_day=doc["response"]["requests"]["limit_day"],
                current=doc["response"]["requests"]["current"],
                observed_utc=meta["fetched_at_utc"],
                sha256=meta["sha256"],
                headers=meta["quota_headers"],
            )

    def _document(self, raw, scope=None):
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeError):
            raise LocalValidationError("API-Football 响应不是 JSON") from None
        check_api_errors(doc, raw)
        rows = doc.get("response")
        paging = doc.get("paging")
        params = doc.get("parameters")
        if (
            doc.get("get") != "fixtures"
            or not isinstance(rows, list)
            or type(doc.get("results")) is not int
            or doc["results"] != len(rows)
        ):
            raise LocalValidationError("API-Football fixtures 结构或 results 不符")
        if (
            not isinstance(paging, dict)
            or any(type(paging.get(k)) is not int for k in ("current", "total"))
            or not 1 <= paging["current"] <= paging["total"] <= 1000
        ):
            raise LocalValidationError("API-Football paging 无效")
        if not isinstance(params, dict):
            raise LocalValidationError("API-Football parameters 无效")
        if scope is not None and {k: str(v) for k, v in params.items() if k != "page"} != scope:
            raise LocalValidationError("API-Football parameters 与请求不符")
        if "page" in params and str(params["page"]) != str(paging["current"]):
            raise LocalValidationError("API-Football 页码与 parameters 不符")
        return doc

    def iter_raw(self, *, league_id=None, season=None, date=None):
        scope = self._scope(league_id, season, date)
        self.pages = []
        page, total, ids = 1, None, set()
        while True:
            path = self._request(
                self.request_url(
                    league_id=league_id, season=season, date=date, page=page if page > 1 else None
                )
            )
            doc = self._document(path.read_bytes(), scope)
            if (
                doc["paging"]["current"] != page
                or total is not None
                and total != doc["paging"]["total"]
            ):
                raise LocalValidationError("API-Football 分页发生变化或缺页；此前页面保留")
            total = doc["paging"]["total"]
            current = {
                _id(r.get("fixture", {}).get("id"))
                for r in doc["response"]
                if isinstance(r, dict) and isinstance(r.get("fixture"), dict)
            }
            current.discard(None)
            if current & ids:
                raise LocalValidationError("API-Football 跨页比赛 ID 重复")
            ids.update(current)
            yield path
            if page == total:
                return
            page += 1

    def fetch_raw(self, *, league_id=None, season=None, date=None) -> Path:
        pages = list(self.iter_raw(league_id=league_id, season=season, date=date))
        if len(pages) == 1:
            return pages[0]
        doc = json.loads(pages[0].read_bytes())
        doc["response"] = [r for p in pages for r in json.loads(p.read_bytes())["response"]]
        doc["results"] = len(doc["response"])
        doc["paging"] = {"current": 1, "total": 1}
        doc["archive_pages"] = [self.store.load_metadata(p) for p in pages]
        latest = self.store.load_metadata(pages[-1])
        return self.store.save(
            self.name,
            "assembled-fixtures",
            json.dumps(doc).encode(),
            "json",
            captured_at=datetime.fromisoformat(latest["fetched_at_utc"]),
            url=self.request_url(league_id=league_id, season=season, date=date),
            status_code=200,
        )

    def parse(self, raw: bytes, *, league_id=None, season=None, date=None) -> list[ApiGame]:
        self.rejected = []
        scope = (
            self._scope(league_id, season, date)
            if any(v is not None for v in (league_id, season, date))
            else None
        )
        doc = self._document(raw, scope)
        rows = doc["response"]
        games = []
        ids = Counter(
            _id(r.get("fixture", {}).get("id"))
            for r in rows
            if isinstance(r, dict) and isinstance(r.get("fixture"), dict)
        )
        sha = hashlib.sha256(raw).hexdigest()
        for n, row in enumerate(rows, 1):
            mid = None
            try:
                if not isinstance(row, dict):
                    raise LocalValidationError("比赛行不是对象")
                league = row.get("league", {})
                code = str(league.get("id")) if isinstance(league, dict) else ""
                # The date endpoint returns all leagues; filter unconfigured competitions.
                if code not in self.leagues:
                    if scope and "league" in scope:
                        raise LocalValidationError("比赛联赛与请求不符")
                    continue
                fixture = row.get("fixture", {})
                if not isinstance(fixture, dict):
                    raise LocalValidationError("fixture 无效")
                mid = _id(fixture.get("id"))
                if mid is None or ids[mid] != 1:
                    raise LocalValidationError("比赛数字 ID 无效或重复")
                stamp = fixture.get("timestamp")
                if type(stamp) is not int:
                    raise LocalValidationError("fixture.timestamp 缺失或无效")
                when = datetime.fromtimestamp(stamp, UTC)
                if scope:
                    if "league" in scope and (
                        code != scope["league"] or str(league.get("season")) != scope["season"]
                    ):
                        raise LocalValidationError("比赛联赛/赛季与请求不符")
                    if "date" in scope and when.date().isoformat() != scope["date"]:
                        raise LocalValidationError("比赛 UTC 日期与请求不符")
                state = fixture.get("status", {}).get("short")
                # Real MLS 2023 includes one "Canc" alongside the usual "CANC".
                if state == "Canc":
                    state = "CANC"
                if not isinstance(state, str) or state not in STATES:
                    raise LocalValidationError("未知比赛状态")
                if state in {"AWD", "WO"}:
                    raise ValueError(f"{state} 判罚/弃权不写常规时间比分")
                teams = row.get("teams", {})
                home, hn = _team(teams.get("home"))
                away, an = _team(teams.get("away"))
                if home == away:
                    raise LocalValidationError("主客数字球队 ID 相同")
                full = half = None
                if state in FINISHED:
                    score = row.get("score")
                    if not isinstance(score, dict):
                        raise LocalValidationError("缺少 score.fulltime")
                    full = _goals(score, "fulltime")
                    half = _goals(score, "halftime", optional=True)
                    if half and any(a > b for a, b in zip(half, full, strict=True)):
                        raise LocalValidationError("半场比分大于全场比分")
                games.append(
                    ApiGame(
                        "apif",
                        mid,
                        code,
                        self.leagues[code]["league"],
                        when,
                        state,
                        hn,
                        an,
                        home,
                        away,
                        full,
                        half,
                        "regular" if full else "missing",
                    )
                )
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError, OSError) as exc:
                self.rejected.append(
                    dict(row_number=n, provider_match_id=mid, raw_sha256=sha, reason=str(exc))
                )
        return games
