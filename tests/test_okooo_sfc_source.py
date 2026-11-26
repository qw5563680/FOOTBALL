import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from lxml import html
from responses import RequestsMock

from zucai.sources.base import RawStore
from zucai.sources.okooo_sfc import OkoooSfc

FIXTURES = Path(__file__).parent.parent / "data" / "fixtures" / "okooo"
ROBOTS_URL = "https://www.okooo.com/robots.txt"
ISSUE_URL = "https://www.okooo.com/zucai/26169/"
ROBOTS = b"User-agent: *\nDisallow: /private/\nVisit-time: 0130-1830\n"
NOW = datetime(2026, 11, 22, 16, tzinfo=UTC)


@pytest.fixture
def source(tmp_path, monkeypatch):
    from zucai.sources import base, okooo_sfc

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(okooo_sfc, "datetime", Clock)
    ticks = [100.0]

    def sleep(seconds):
        ticks[0] += seconds

    monkeypatch.setattr(base.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(base.time, "sleep", sleep)
    result = OkoooSfc(store=RawStore(tmp_path))
    yield result, ticks
    result.session.close()


def test_fetch_respects_rate_and_persists_fixture_only_snapshot(source):
    adapter, ticks = source
    raw = (
        (FIXTURES / "26169.html")
        .read_bytes()
        .replace(b"</body>", b"<div data-odds='19.81'>do-not-save-odds</div></body>")
    )
    assert b"do-not-save-odds" in raw

    with RequestsMock() as mock:
        mock.get(ROBOTS_URL, body=ROBOTS + b"Request-rate: 1/5\n")
        mock.get(ISSUE_URL, body=raw)
        path = adapter.fetch_raw(issue="26169")

    archived = adapter.store.load(path)
    metadata = adapter.store.load_metadata(path)
    (result,) = adapter.parse(archived)
    payload = json.loads(archived)
    assert result.issue_no == "26169"
    assert result.sale_end_utc == datetime(2026, 11, 24, 14, tzinfo=UTC)
    assert [f.seq for f in result.fixtures] == list(range(1, 15))
    assert result.fixtures[0].fixture_id == "1347499"
    assert result.fixtures[0].kickoff_utc == datetime(2026, 11, 24, 17, 45, tzinfo=UTC)
    assert result.fixtures[0].away == "林 茨"
    assert all(f.home_id is None and f.away_id is None for f in result.fixtures)
    assert b"odds" not in archived and b"19.81" not in archived and b"/soccer/" not in archived
    assert payload["response_sha256"] == adapter.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert payload["robots_sha256"] == adapter.robots_sha256
    assert metadata["sha256"] == hashlib.sha256(archived).hexdigest()
    assert metadata["url"] == ISSUE_URL
    assert ticks[0] == 105
    assert (adapter.robots_requests, adapter.page_requests, adapter.requests) == (1, 1, 2)


@pytest.mark.parametrize(
    ("reason", "status"),
    [("期号", 200), ("场序", 200), ("HTTP 405", 405), ("重定向", 302)],
)
def test_rejected_page_never_retried_or_archived(source, reason, status):
    adapter, _ = source
    raw = (FIXTURES / ("26170.html" if reason == "期号" else "26169.html")).read_bytes()
    if reason == "场序":
        page = html.fromstring(raw.decode())
        page.xpath('//table[@id="table4"]//tr')[-1].drop_tree()
        raw = html.tostring(page, encoding="utf-8")

    with RequestsMock() as mock:
        mock.get(ROBOTS_URL, body=ROBOTS)
        mock.get(ISSUE_URL, status=status, body=raw, headers={"Location": ROBOTS_URL})
        with pytest.raises(ValueError, match=reason):
            adapter.fetch_raw(issue="26169")
        with pytest.raises(ValueError, match="单班"):
            adapter.fetch_raw(issue="26169")

    assert adapter.requests == adapter.request_limit == 2
    assert not list(adapter.store.root.rglob("*"))


@pytest.mark.parametrize(
    ("policy", "status", "offset", "reason"),
    [
        (ROBOTS, 200, timedelta(hours=8), "访问时段"),
        (ROBOTS.replace(b"/private/", b"/zucai/"), 200, timedelta(0), "不允许"),
        (ROBOTS.replace(b"0130-1830", b"0190-2461"), 200, timedelta(0), "访问时段"),
        (ROBOTS.split(b"Visit-time:")[0], 200, timedelta(0), "访问时段"),
        (b"<html>not robots</html>", 200, timedelta(0), "robots"),
        (ROBOTS, 404, timedelta(0), "HTTP 404"),
    ],
)
def test_runtime_robots_check_stops_the_page_request(source, policy, status, offset, reason):
    adapter, _ = source
    with (
        RequestsMock() as mock,
        patch("zucai.sources.okooo_sfc.datetime") as clock,
        pytest.raises(ValueError, match=reason),
    ):
        clock.now.return_value = NOW + offset
        mock.get(ROBOTS_URL, status=status, body=policy)
        adapter.fetch_raw(issue="26169")

    assert adapter.requests == adapter.robots_requests == 1
    assert adapter.page_requests == 0
    assert not list(adapter.store.root.rglob("*"))


def test_request_spacing_cannot_push_page_past_window_end(source):
    adapter, ticks = source
    just_before = datetime(2026, 11, 22, 18, 29, 59, tzinfo=UTC)
    with (
        RequestsMock() as mock,
        patch("zucai.sources.okooo_sfc.datetime") as clock,
        pytest.raises(ValueError, match="访问时段"),
    ):
        clock.now.side_effect = lambda _: just_before + timedelta(seconds=ticks[0] - 100)
        mock.get(ROBOTS_URL, body=ROBOTS)
        adapter.fetch_raw(issue="26169")

    assert adapter.requests == 1 and adapter.page_requests == 0


def test_already_closed_issue_never_persists_a_snapshot(source):
    adapter, _ = source
    with (
        RequestsMock() as mock,
        patch("zucai.sources.okooo_sfc.datetime") as clock,
        pytest.raises(ValueError, match="停售时间已到"),
    ):
        clock.now.return_value = datetime(2026, 11, 24, 14, tzinfo=UTC)
        mock.get(ROBOTS_URL, body=ROBOTS)
        mock.get(ISSUE_URL, body=(FIXTURES / "26169.html").read_bytes())
        adapter.fetch_raw(issue="26169")

    assert adapter.requests == 2
    assert not list(adapter.store.root.rglob("*"))
