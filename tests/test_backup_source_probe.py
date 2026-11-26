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
SFC = "https://cp.zgzcw.com/zgzcw/lottery/"
JC = "https://cp.zgzcw.com/lottery/jczq/index.jsp"
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


def test_only_the_owner_selected_candidates_are_probed():
    assert probe.CANDIDATES == (
        ("zgzcw", "中国足彩网", {"sfc": SFC, "jc": JC}, None),
        ("okooo", "澳客", {"jc": OKOOO}, None),
    )


@pytest.mark.parametrize(("status", "expected"), [(405, "http_error"), (200, "requires_js")])
def test_offline_probe_classifies_denial_405_js_and_records_only_safe_evidence(
    replay, tmp_path, status, expected
):
    http, _ = replay
    robots(http, SFC, "User-agent: *\nDisallow: /zgzcw/lottery/$\n")
    robots(http, OKOOO, "User-agent: *\nAllow: /\n")
    # Reuse T90's classifier controls. These responses are not observations of ZGZCW.
    js_required = "<title>竞足赛程</title><noscript>Enable JavaScript</noscript>".encode()
    raw = (FIX / "26170-sfc-405.html").read_bytes() if status == 405 else js_required
    http.get(JC, status=status, body=raw)
    http.get(OKOOO, body=js_required)

    report = probe.collect_backup_source_probe(output=tmp_path)
    assert [p["code"] for p in report["candidates"]] == ["zgzcw", "okooo"]
    observed = {row["url"]: row for site in report["candidates"] for row in site["pages"]}
    assert observed[SFC]["status"] == "robots_denied"
    assert observed[SFC]["observed_utc"] is None
    assert observed[JC]["status"] == expected and observed[JC]["http_status"] == status
    assert observed[JC]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert observed[OKOOO]["status"] == "requires_js"
    assert observed[OKOOO]["sha256"] == hashlib.sha256(js_required).hexdigest()
    assert [call.request.url for call in http.calls] == [
        "https://cp.zgzcw.com/robots.txt",
        JC,
        "https://www.okooo.com/robots.txt",
        OKOOO,
    ]
    assert json.loads((tmp_path / "probe.json").read_text(encoding="utf-8")) == report
    assert len(list((tmp_path / "samples").glob("*.json"))) == 4  # Two rules and two responses.
    for manifest_line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        sha, filename = manifest_line.split("  ", 1)
        assert hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest() == sha
    assert not list(tmp_path.rglob("*.html"))


@pytest.mark.parametrize(
    ("rules", "interval"), [("", 3), ("Crawl-delay: 4\nRequest-rate: 1/5\n", 5)]
)
def test_limits_apply_across_hosts_and_repeat_hashes_retained(
    replay, tmp_path, monkeypatch, rules, interval
):
    http, instant = replay
    started = []
    for url in (SFC, OKOOO):
        robots(http, url, f"User-agent: *\nAllow: /\n{rules}")

    raw = (FIX / "26170-sfc.html").read_bytes()

    def body(request):
        started.append((request.url, instant[0]))
        return 200, {}, raw

    for url in (SFC, JC, OKOOO):
        http.add_callback("GET", url, callback=body)
    monkeypatch.setattr(
        probe,
        "inspect_page",
        lambda raw, purpose: {
            "status": "readable",
            "reason": "",
            "fields": ["seq", "league", "home", "away", "kickoff", "period", "sale_end"],
            "complete_fields": ["seq", "league", "home", "away", "kickoff", "period", "sale_end"],
            "sfc_order_complete": True,
            "sample": [],
        },
    )
    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    assert [site["page_requests"] for site in sites] == [3, 2]
    assert [site["robots_requests"] for site in sites] == [1, 1]
    assert all(b - a >= interval for (_, a), (_, b) in pairwise(started))
    requests_observed = sorted(
        datetime.fromisoformat(row["observed_utc"])
        for site in sites
        for row in (*site["robots"], *site["pages"])
    )
    assert all((b - a).total_seconds() >= 3 for a, b in pairwise(requests_observed))
    assert [url for url, _ in started] == [SFC, JC, SFC, OKOOO, OKOOO]
    assert sites[0]["assessment"]["sfc"]["result"] == "可用"
    assert sites[0]["assessment"]["jc"]["result"] == "需所有者决定"
    for site in sites:
        attempts = [row for row in site["pages"] if row["http_status"] is not None]
        assert len(attempts) == site["page_requests"]
        assert all(
            row["observed_utc"] and row["sha256"] == hashlib.sha256(raw).hexdigest()
            for row in attempts
        )


def test_window_in_robots_blocks_page_and_no_redirect_is_followed(replay, tmp_path):
    http, _ = replay
    robots(http, SFC, "User-agent: *\nAllow: /\n")
    robots(http, OKOOO, "User-agent: *\nVisit-time: 0000-0001\n")
    http.get(SFC, status=302, headers={"Location": "https://example.org/jc/"})
    http.get(JC, status=429)
    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    observed = {row["url"]: row for site in sites for row in site["pages"]}
    assert observed[OKOOO]["status"] == "outside_window"
    assert observed[SFC]["http_status"] == 302
    assert observed[JC]["http_status"] == 429
    assert len(http.calls) == 4
