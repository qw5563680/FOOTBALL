"""Read one in-line 14-match issue from the public Okooo schedule."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests

from zucai.okooo_probe import AGENT, ORIGIN, _get, inspect_page
from zucai.sources.base import RateLimiter, RawStore, Source
from zucai.sources.c500 import SfcFixture, SfcSnapshot, issue_number

ROBOTS_URL = f"{ORIGIN}/robots.txt"
FIXTURE_KEYS = {"seq", "fixture_id", "league", "home", "away", "kickoff_utc"}


def _visit_windows(lines):
    windows = []
    for line in lines:
        key, _, value = line.partition(":")
        if key.strip().lower() != "visit-time":
            continue
        pair = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", value.strip())
        if pair is None or any(
            int(part[:2]) >= 24 or int(part[2:]) >= 60 for part in pair.groups()
        ):
            raise ValueError("澳客 robots 访问时段无效")
        windows.append(pair.groups())
    if not windows:
        raise ValueError("澳客 robots 未声明访问时段")
    return windows


def _require_open(windows):
    now = datetime.now(UTC).strftime("%H%M")
    if not any(
        start <= now < end if start <= end else now >= start or now < end for start, end in windows
    ):
        raise ValueError("澳客 robots 访问时段未放行（UTC）")


def _aware(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("澳客时间缺少时区")
    return instant.astimezone(UTC)


class OkoooSfc(Source[SfcSnapshot]):
    """Do not retain page HTML, which can contain odds outside the fixture table."""

    name = "okooo-sfc"
    request_limit = 2  # Fresh robots.txt and one numbered issue. No other page or retry.

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else requests.Session()
        self.limiter = RateLimiter(3)
        self.robots_requests = 0
        self.page_requests = 0
        self.response_sha256 = None
        self.robots_sha256 = None

    @property
    def requests(self):
        return self.robots_requests + self.page_requests

    def _policy(self, url):
        self.limiter.wait()
        self.robots_requests += 1
        status, raw = _get(self.session, ROBOTS_URL)
        self.robots_sha256 = hashlib.sha256(raw).hexdigest()
        if status != 200:
            raise ValueError(f"澳客 robots HTTP {status}，本班不可用")
        try:
            lines = [line.split("#", 1)[0].strip() for line in raw.decode("utf-8-sig").splitlines()]
        except UnicodeError as exc:
            raise ValueError("澳客 robots 内容无法核实") from exc
        if not any(re.match(r"user-agent\s*:\s*\S", line, re.I) for line in lines) or any(
            "<html" in line.lower() for line in lines
        ):
            raise ValueError("澳客 robots 内容无法核实")
        policy = RobotFileParser(ROBOTS_URL)
        policy.parse(lines)
        if not policy.can_fetch(AGENT, url):
            raise ValueError("澳客 robots 不允许访问该期页面")
        rate = policy.request_rate(AGENT)
        self.limiter.min_interval_s = max(
            3,
            policy.crawl_delay(AGENT) or 0,
            rate.seconds / rate.requests if rate and rate.requests else 0,
        )
        return _visit_windows(lines)

    def fetch_raw(self, *, issue: str) -> Path:
        number = issue_number(issue)
        if self.requests:
            raise ValueError("澳客单班只允许一次按期采集")
        url = f"{ORIGIN}/zucai/{number}/"
        windows = self._policy(url)
        _require_open(windows)
        self.limiter.wait()
        _require_open(windows)
        self.page_requests += 1
        status, raw = _get(self.session, url)
        observed = datetime.now(UTC)
        self.response_sha256 = hashlib.sha256(raw).hexdigest()
        if 300 <= status < 400:
            raise ValueError(f"澳客页面发生重定向（HTTP {status}）")
        if status != 200:
            raise ValueError(f"澳客页面 HTTP {status}，本班不可用")
        page = inspect_page(raw, number, "sfc", observed.isoformat())
        if page["read_status"] != "complete":
            raise ValueError("澳客页面不完整：" + "、".join(page["reasons"]))
        if page["deadline_state"] != "截止前":
            raise ValueError("澳客所报停售时间已到")
        document = {
            "issue_no": number,
            "sale_end_utc": page["sale_end_utc"],
            "response_sha256": self.response_sha256,
            "robots_sha256": self.robots_sha256,
            "fixtures": [
                dict(
                    seq=row["seq"],
                    fixture_id=row["okooo_fixture_id"],
                    league=row["league"],
                    home=row["home"],
                    away=row["away"],
                    kickoff_utc=_aware(row["kickoff_beijing"]).isoformat(),
                )
                for row in page["rows"]
            ],
        }
        clean = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.parse(clean)
        return self.store.save(
            self.name,
            f"issue-{number}",
            clean,
            "json",
            captured_at=observed,
            url=url,
            status_code=status,
        )

    def parse(self, raw: bytes) -> list[SfcSnapshot]:
        try:
            document = json.loads(raw)
            number = issue_number(document["issue_no"])
            end = _aware(document["sale_end_utc"])
            rows = document["fixtures"]
            if [row["seq"] for row in rows] != list(range(1, 15)):
                raise ValueError("场序必须为 1 至 14")
            fixtures = []
            seen = set()
            for row in rows:
                if (
                    set(row) != FIXTURE_KEYS
                    or not all(
                        isinstance(row[key], str) and row[key] for key in ("league", "home", "away")
                    )
                    or row["home"] == row["away"]
                    or not re.fullmatch(r"[0-9]+", row["fixture_id"])
                    or row["fixture_id"] in seen
                ):
                    raise ValueError(f"第 {row['seq']} 场的赛事、主客或编号无效")
                seen.add(row["fixture_id"])
                fixtures.append(
                    SfcFixture(**{**row, "kickoff_utc": _aware(row["kickoff_utc"]), "ended": False})
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"澳客名单归档无效：{exc}") from exc
        return [SfcSnapshot(number, end, fixtures, "explicit")]
