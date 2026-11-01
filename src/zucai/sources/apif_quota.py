"""Durable UTC daily request budget; reserve before dispatch, including failed GETs."""

import fcntl
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from zucai.sources.base import _atomic_write


class QuotaExceeded(ValueError):
    pass


class DailyQuota:
    def __init__(self, path="data/api_football_quota.json", reserve=10, *, now=None):
        if type(reserve) is not int or not 0 <= reserve < 100:
            raise ValueError("--reserve 须为 0–99 的整数")
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
        if not self.path.exists():
            return dict(date_utc=today, used=0, remaining=None)
        try:
            q = json.loads(self.path.read_bytes())
            datetime.strptime(q["date_utc"], "%Y-%m-%d")
            if (
                type(q["used"]) is not int
                or q["used"] < 0
                or q["remaining"] is not None
                and (type(q["remaining"]) is not int or not 0 <= q["remaining"] <= 100)
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("配额文件损坏，拒绝重新假定完整预算") from None
        if q["date_utc"] > today:
            raise ValueError("配额日期晚于当前 UTC 日期")
        return q if q["date_utc"] == today else dict(date_utc=today, used=0, remaining=None)

    def _write(self, q):
        _atomic_write(self.path, json.dumps(q).encode())
        self.remaining = q["remaining"] if q["remaining"] is not None else max(0, 100 - q["used"])

    def before_request(self):
        with self.locked():
            q = self._read()
            remaining = q["remaining"] if q["remaining"] is not None else max(0, 100 - q["used"])
            if remaining <= self.reserve or q["used"] >= 100:
                self.remaining = remaining
                raise QuotaExceeded(
                    f"API-Football 剩余配额 {remaining} <= reserve {self.reserve}，"
                    "或本地日请求已达100；已停止，已完成部分保留"
                )
            q["used"] += 1
            q["remaining"] = remaining - 1
            self._write(q)

    def observe(self, headers):
        with self.locked():
            q = self._read()
            value = next(
                (v for k, v in headers.items() if k.lower() == "x-ratelimit-requests-remaining"),
                None,
            )
            if value is not None:
                if (
                    not isinstance(value, str)
                    or not value.isascii()
                    or not value.isdecimal()
                    or not 0 <= int(value) <= 100
                ):
                    raise ValueError("API-Football 每日剩余配额响应头无效")
                q["remaining"] = int(value)
            self._write(q)
