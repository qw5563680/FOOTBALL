"""T32-only request pacing; normal collection and /status keep their existing policy."""

import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from zucai.sources.api_football import API, APIF_INDEX_LEAGUE_IDS, APIF_LEAGUE_IDS, ApiFootball
from zucai.sources.apif_quota import QuotaExceeded
from zucai.sources.apif_stats import instant

ALLOWED_LEAGUES = APIF_INDEX_LEAGUE_IDS | APIF_LEAGUE_IDS
INJURY_LEAGUES = {39, 140, 135, 78, 61, 88, 119, 113, 103, 253, 188}
DETAILS = ("statistics", "events", "lineups", "players")


class StatsStopped(ValueError):
    """Stop this batch, preserving completed observations and charged requests."""


class LineupWindowMissed(ValueError):
    """Do not charge a request after a wait has passed the pregame window."""


class MinutePacer:
    def __init__(self, now, sleep):
        self.now, self.sleep = now, sleep
        self.last = None
        self.next_minute = None
        self.preflight = None

    def wait(self):
        now = self.now()
        due = max(
            self.last + timedelta(seconds=1) if self.last else now,
            self.next_minute or now,
        )
        if due > now:
            self.sleep((due - now).total_seconds())
        self.last = self.now()
        if self.preflight is not None:
            self.preflight(self.last)

    def observe(self, headers):
        for key in ("x-ratelimit-limit", "x-ratelimit-remaining"):
            if key in headers and (
                not isinstance(headers[key], str)
                or not headers[key].isascii()
                or not headers[key].isdecimal()
            ):
                raise ValueError("技术数据分钟配额头无效")
        if "x-ratelimit-remaining" in headers and int(headers["x-ratelimit-remaining"]) <= 20:
            self.next_minute = self.now().replace(second=0, microsecond=0) + timedelta(minutes=1)


class StatsClient(ApiFootball):
    def __init__(self, *, max_requests=7000, daily_reserve=300, now=None, sleep=None, **kwargs):
        if type(max_requests) is not int or not 1 <= max_requests <= 7000:
            raise ValueError("max_requests 必须为 1–7000")
        if type(daily_reserve) is not int or daily_reserve < 300:
            raise ValueError("日常预留至少 300")
        super().__init__(**kwargs)
        self.now = now or (lambda: datetime.now(UTC))
        self.quota.now = self.now
        self.limiter = MinutePacer(self.now, sleep or time.sleep)
        self.max_requests = max_requests
        self.daily_reserve = daily_reserve
        self.stopped = None
        self.last_http = None
        self.session.hooks.setdefault("response", []).append(self._observe_response)

    def _observe_response(self, response, *args, **kwargs):
        # Observe transport status even when HTML/unsafe JSON cannot be archived.
        # The inherited adapter still owns safe raw persistence and daily quota headers.
        self.last_http = response.status_code
        try:
            self.limiter.observe({k.lower(): v for k, v in response.headers.items()})
        except ValueError:
            self.stopped = "技术数据分钟配额头无效，本批停止"
        if response.status_code == 429:
            self.stopped = "HTTP 429，立即停止本批"

    def fetch(self, endpoint, *, league_id, season, fixture_id=None, kickoff_utc=None):
        if (
            type(league_id) is not int
            or league_id not in ALLOWED_LEAGUES
            or type(season) is not int
            or not 2022 <= season <= self.now().year + 1
        ):
            raise ValueError("技术请求联赛季超出允许范围")
        if endpoint == "fixtures":
            params = self.index_scope(league_id=league_id, season=season)
            resource = endpoint
        elif endpoint == "injuries" and league_id in INJURY_LEAGUES:
            params, resource = {"league": str(league_id), "season": str(season)}, endpoint
        elif (
            endpoint in DETAILS
            and str(fixture_id).isascii()
            and str(fixture_id).isdecimal()
            and int(fixture_id) > 0
        ):
            params, resource = {"fixture": str(fixture_id)}, "fixtures/" + endpoint
        else:
            raise ValueError("技术请求端点或 fixture ID 超出允许范围")
        if self.stopped:
            raise StatsStopped(self.stopped)
        if self.request_count >= self.max_requests:
            raise StatsStopped("已达 max_requests，本批停止")
        self.last_http = None
        if kickoff_utc is not None:
            if endpoint != "lineups":
                raise ValueError("赛前窗口仅用于 lineups")
            kickoff = instant(kickoff_utc)

            def preflight(now):
                if not 30 <= (kickoff - now).total_seconds() / 60 <= 60:
                    raise LineupWindowMissed("请求前已不在开球前 60–30 分钟窗口")

            self.limiter.preflight = preflight
        try:
            with self.quota.locked():
                remaining = self.quota.describe()["remaining_quota"]
                if remaining <= self.quota.reserve + self.daily_reserve:
                    raise StatsStopped("剩余配额不足 reserve + 日常预留 300，本批停止")
                return self._request(
                    API + "/" + resource + "?" + urlencode(params), name="stats-" + endpoint
                )
        except QuotaExceeded as exc:
            raise StatsStopped(str(exc)) from exc
        finally:
            self.limiter.preflight = None
            if self.stopped:
                raise StatsStopped(self.stopped)
