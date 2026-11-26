import hashlib
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import responses

from zucai import backup_source_probe as probe
from zucai.sources import base

FIX = Path(__file__).resolve().parents[1] / "data/fixtures/okooo_offline"
SFC = "https://aiplus.titan007.com/ai/pc/zucai"
JC = "https://jc.titan007.com/schedule.aspx"
LEISU = "https://www.leisu.com/"
OKOOO = "https://www.okooo.com/jingcai/"


@pytest.fixture
def replay(monkeypatch):
    instant = [0.0]

    class Clock:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(1_795_690_800 + instant[0], UTC).astimezone(tz)

    class Time:
        @staticmethod
        def monotonic():
            return instant[0]

        @staticmethod
        def sleep(seconds):
            instant[0] += seconds

    monkeypatch.setattr(base, "time", Time)
    monkeypatch.setattr(probe, "datetime", Clock)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        yield http, instant


def robots(http, url, text):
    http.get(f"https://{urlsplit(url).netloc}/robots.txt", body=text)


def test_offline_probe_classifies_denial_405_js_and_records_only_safe_evidence(replay, tmp_path):
    http, _ = replay
    robots(http, SFC, "User-agent: *\nDisallow: /*/zucai$\n")
    robots(http, JC, "User-agent: *\nAllow: /\n")
    robots(http, LEISU, "User-agent: *\nDisallow: /\n")
    robots(http, OKOOO, "User-agent: *\nAllow: /\n")
    first = (FIX / "26170-sfc-405.html").read_bytes()
    http.get(JC, status=405, body=first)
    raw = "<title>竞足赛程</title><noscript>Enable JavaScript</noscript>".encode()
    http.get(OKOOO, body=raw)

    report = probe.collect_backup_source_probe(output=tmp_path)
    observed = {row["url"]: row for site in report["candidates"] for row in site["pages"]}
    assert observed[SFC]["status"] == observed[LEISU]["status"] == "robots_denied"
    assert observed[JC]["status"] == "http_error" and observed[JC]["http_status"] == 405
    assert observed[JC]["sha256"] == hashlib.sha256(first).hexdigest()
    assert observed[OKOOO]["status"] == "requires_js"
    assert observed[OKOOO]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert [
        call.request.url for call in http.calls if not call.request.url.endswith("robots.txt")
    ] == [
        JC,
        OKOOO,
    ]
    assert json.loads((tmp_path / "probe.json").read_text(encoding="utf-8")) == report
    assert len(list((tmp_path / "samples").glob("*.json"))) == 6  # Four rules and two responses.
    for manifest_line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        sha, filename = manifest_line.split("  ", 1)
        assert hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest() == sha
    assert not list(tmp_path.rglob("*.html"))


def test_limits_apply_across_hosts_and_repeat_hashes_retained(replay, tmp_path, monkeypatch):
    http, instant = replay
    started = []
    for url in (SFC, JC, LEISU, OKOOO):
        robots(http, url, "User-agent: *\nCrawl-delay: 4\nRequest-rate: 1/5\n")

    def body(request):
        started.append((request.url, instant[0]))
        return 200, {}, (FIX / "26170-sfc.html").read_bytes()

    for url in (SFC, JC, LEISU, OKOOO, LEISU + "jingcai/", LEISU + "zucai/"):
        http.add_callback("GET", url, callback=body)
    monkeypatch.setattr(
        probe,
        "discover_pages",
        lambda raw, origin: {"sfc": [origin + "/zucai/"], "jc": [origin + "/jingcai/"]},
    )
    monkeypatch.setattr(
        probe,
        "inspect_page",
        lambda raw, purpose: dict(
            status="readable", reason="", fields=[], sample=[], complete_fields=[]
        ),
    )
    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    assert [site["page_requests"] for site in sites] == [3, 3, 2]
    assert [site["robots_requests"] for site in sites] == [2, 1, 1]
    assert all(b - a >= 5 for (_, a), (_, b) in pairwise(started))
    assert [url for url, _ in started].count(SFC) == 2
    assert [url for url, _ in started].count(OKOOO) == 2
    for site in sites:
        attempts = [row for row in site["pages"] if row["http_status"] is not None]
        assert len(attempts) == site["page_requests"]
        assert all(row["observed_utc"] and row["sha256"] for row in attempts)


def test_window_in_robots_blocks_page_and_no_redirect_is_followed(replay, tmp_path):
    http, _ = replay
    robots(http, SFC, "User-agent: *\nVisit-time: 0000-0001\n")
    robots(http, JC, "User-agent: *\nAllow: /\n")
    robots(http, LEISU, "User-agent: *\nAllow: /\n")
    robots(http, OKOOO, "User-agent: *\nAllow: /\n")
    http.get(JC, status=302, headers={"Location": "https://example.org/jc/"})
    http.get(LEISU, status=404)
    http.get(OKOOO, status=429)
    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    observed = {row["url"]: row for site in sites for row in site["pages"]}
    assert observed[SFC]["status"] == "outside_window"
    assert observed[JC]["http_status"] == 302
    assert len(http.calls) == 7
