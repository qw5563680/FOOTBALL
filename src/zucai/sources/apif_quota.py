"""Durable UTC daily request budget; reserve before dispatch, including failed GETs."""

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from zucai.apif_budget import normalize_quota
from zucai.sources.base import _atomic_write


class QuotaExceeded(ValueError):
    pass


class DailyQuota:
    def __init__(
        self, path="data/api_football_quota.json", reserve=10, *, now=None, daily_cap=None
    ):
        if type(reserve) is not int or reserve < 0:
            raise ValueError("--reserve 须为非负整数且小于 limit_day")
        cap = (
            daily_cap if daily_cap is not None else (os.environ.get("ZUCAI_APIF_DAILY_CAP") or None)
        )
        if isinstance(cap, str) and cap.isascii() and cap.isdecimal():
            cap = int(cap)
        if cap is not None and (type(cap) is not int or cap < 1):
            raise ValueError("ZUCAI_APIF_DAILY_CAP 须为正整数")
        self.daily_cap = cap
        self.path = Path(path)
        self.reserve = reserve
        self.now = now or (lambda: datetime.now(UTC))
        self.remaining = None
        self._locked = False
        self._mutex = RLock()

    @contextmanager
    def locked(self):
        with self._mutex:
            if self._locked:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._locked = True
                try:
                    yield
                finally:
                    self._locked = False
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        today = self.now().astimezone(UTC).date().isoformat()
        try:
            saved = (
                json.loads(self.path.read_bytes())
                if self.path.exists()
                else dict(date_utc=today, used=0, remaining=None)
            )
            q = normalize_quota(saved)
        except (ValueError, TypeError):
            raise ValueError("配额文件损坏，拒绝重新假定完整预算") from None
        if q["date_utc"] > today:
            raise ValueError("配额日期晚于当前 UTC 日期")
        if q["date_utc"] != today:
            q.update(date_utc=today, used=0, remaining=None, current=None)
        return q

    @staticmethod
    def _remaining(q):
        return q["remaining"] if q["remaining"] is not None else max(0, q["limit_day"] - q["used"])

    def _write(self, q):
        q = normalize_quota(q)
        _atomic_write(self.path, json.dumps(q, ensure_ascii=False).encode())
        self.remaining = self._remaining(q)

    def describe(self):
        q = self._read()
        return dict(
            plan=q["plan"],
            limit_day=q["limit_day"],
            current=q["current"],
            remaining_quota=self._remaining(q),
            quota_fallback=q["limit_source"] == "fallback",
            local_daily_cap=min(self.daily_cap or q["limit_day"], q["limit_day"]),
        )

    def before_request(self):
        with self.locked():
            q = self._read()
            if self.reserve >= q["limit_day"]:
                raise ValueError("--reserve 须小于 limit_day")
            remaining = self._remaining(q)
            cap = min(self.daily_cap or q["limit_day"], q["limit_day"])
            if remaining <= self.reserve or q["used"] >= cap:
                self.remaining = remaining
                raise QuotaExceeded(
                    f"API-Football 剩余配额 {remaining} <= reserve {self.reserve}，"
                    f"或本地日请求已达 {cap}；已停止，已完成部分保留"
                )
            q["used"] += 1
            q["remaining"] = remaining - 1
            self._write(q)

    def observe(self, headers):
        with self.locked():
            q = self._read()
            seen = {k.lower(): v for k, v in headers.items()}
            for field, header in (("limit_day", "limit"), ("remaining", "remaining")):
                value = seen.get("x-ratelimit-requests-" + header)
                if value is not None:
                    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                        raise ValueError("API-Football 每日配额响应头无效")
                    if field == "limit_day":
                        if int(value) != q["limit_day"]:
                            q.update(
                                current=None,
                                plan=None,
                                status_free=None,
                                status_checked_utc=None,
                                status_evidence=[],
                            )
                        q["limit_source"] = "header"
                    q[field] = int(value)
            if (
                "x-ratelimit-requests-limit" in seen
                and "x-ratelimit-requests-remaining" not in seen
            ):
                q["remaining"] = min(self._remaining(q), max(0, q["limit_day"] - q["used"]))
            if any(
                k in seen for k in ("x-ratelimit-requests-limit", "x-ratelimit-requests-remaining")
            ):
                q["observed_utc"] = self.now().astimezone(UTC).isoformat()
            self._write(q)

    def observe_status(self, doc, headers):
        """Headers win; /status fills plan/current and a missing daily-limit header."""
        with self.locked():
            q = self._read()
            try:
                response = doc["response"]
                plan = response["subscription"]["plan"]
                current = response["requests"]["current"]
                limit = response["requests"]["limit_day"]
                if (
                    type(limit) is not int
                    or limit < 1
                    or type(current) is not int
                    or not 0 <= current <= limit
                ):
                    raise ValueError
                # A current counter belongs to its own limit; never combine different plans.
                if "x-ratelimit-requests-limit" in headers and limit != q["limit_day"]:
                    raise ValueError
                if plan != q["plan"] or limit != q["limit_day"]:
                    q.update(status_free=None, status_checked_utc=None, status_evidence=[])
                if "x-ratelimit-requests-limit" not in headers:
                    q.update(limit_day=limit, limit_source="status")
                q.update(
                    plan=plan, current=current, observed_utc=self.now().astimezone(UTC).isoformat()
                )
                status_remaining = max(0, q["limit_day"] - current)
                # The body supplies remaining when there is no daily remaining header.
                q["remaining"] = (
                    min(q["remaining"], status_remaining)
                    if "x-ratelimit-requests-remaining" in headers
                    else status_remaining
                )
                self._write(q)
            except (ValueError, TypeError, KeyError):
                raise ValueError("API-Football status 配额结构无效") from None

    def record_status_billing(self, free, evidence):
        with self.locked():
            q = self._read()
            q.update(
                status_free=free,
                status_evidence=evidence,
                status_checked_utc=self.now().astimezone(UTC).isoformat(),
            )
            self._write(q)
