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
T97 = FIX.parent / "backup_sources"
HOME = "https://www.zgzcw.com/"
SFC = "https://cp.zgzcw.com/sfc/index.html"
JC = "https://jc.zgzcw.com/jczq/index.html"
OKOOO = "https://www.okooo.com/jingcai/"
DISALLOWED = "https://cp.zgzcw.com/homePage/sfc.html"
ALLOW = "User-agent: *\nAllow: /\n"


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


def robots(http, url, text=ALLOW):
    http.get(f"https://{urlsplit(url).netloc}/robots.txt", body=text)


def homepage(http, *, filename="t97-probe-home.html", blocked=False):
    robots(http, HOME)
    raw = (T97 / filename).read_bytes()
    if blocked:
        raw = raw.replace(SFC.encode(), DISALLOWED.encode())
    http.get(HOME, body=raw)
    return raw


def sample(root, observation):
    return json.loads((root / observation["sample_path"]).read_bytes())


def test_only_the_owner_selected_candidates_are_probed():
    assert probe.CANDIDATES == (
        ("zgzcw", "中国足彩网", {"sfc": None, "jc": None}, HOME),
        ("okooo", "澳客", {"jc": OKOOO}, None),
    )


@pytest.mark.parametrize(("status", "expected"), [(405, "http_error"), (200, "requires_js")])
def test_offline_probe_classifies_denial_405_js_and_records_only_safe_evidence(
    replay, tmp_path, status, expected
):
    http, _ = replay
    home_raw = homepage(http, blocked=True)
    robots(http, SFC, "User-agent: *\nDisallow: /homePage/\n")
    robots(http, JC)
    robots(http, OKOOO)
    # Archived 405 is a classifier control, not an observation of ZGZCW.
    js_required = "<title>竞足赛程</title><noscript>Enable JavaScript</noscript>".encode()
    rejected = (FIX / "26170-sfc-405.html").read_bytes()
    raw = rejected if status == 405 else js_required
    http.get(JC, status=status, body=raw)
    http.get(OKOOO, status=405, body=rejected)

    report = probe.collect_backup_source_probe(output=tmp_path)
    assert [p["code"] for p in report["candidates"]] == ["zgzcw", "okooo"]
    observed = {row["url"]: row for site in report["candidates"] for row in site["pages"]}
    assert observed[DISALLOWED]["status"] == "robots_denied"
    assert observed[DISALLOWED]["observed_utc"] is None
    assert observed[JC]["status"] == expected and observed[JC]["http_status"] == status
    assert observed[JC]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert observed[OKOOO]["status"] == "http_error"
    assert sample(tmp_path, observed[OKOOO])["evidence"]["body_bytes"] == len(rejected)
    assert sample(tmp_path, observed[OKOOO])["sample"] == []
    assert sample(tmp_path, observed[HOME])["evidence"]["body_bytes"] == len(home_raw)
    assert sample(tmp_path, observed[HOME])["evidence"]["title"] == "中国足彩网首页离线样本"
    assert [call.request.url for call in http.calls] == [
        "https://www.zgzcw.com/robots.txt",
        HOME,
        "https://cp.zgzcw.com/robots.txt",
        "https://jc.zgzcw.com/robots.txt",
        JC,
        "https://www.okooo.com/robots.txt",
        OKOOO,
    ]
    assert json.loads((tmp_path / "probe.json").read_text(encoding="utf-8")) == report
    assert len(list((tmp_path / "samples").glob("*.json"))) == 7
    for manifest_line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        sha, filename = manifest_line.split("  ", 1)
        assert hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest() == sha
    assert not list(tmp_path.rglob("*.html"))


def test_no_homepage_links_means_no_guessed_url_or_other_robots_requests(replay, tmp_path):
    http, _ = replay
    homepage(http, filename="t97-probe-no-entry.html")
    robots(http, OKOOO)
    http.get(OKOOO, status=405)
    archive = probe.collect_backup_source_probe(output=tmp_path)

    site = archive["candidates"][0]
    assert [row["url"] for row in site["pages"]] == [HOME]
    assert site["pages"][0]["link_labels"] == ["竞彩足球对阵", "胜负彩介绍"]
    assert site["robots_requests"] == site["page_requests"] == 1
    assert len(http.calls) == 4
    readme = (tmp_path / "README.md").read_text()
    assert "静态首页未确认专用足彩或竞彩赛程链接" in readme
    assert "竞彩足球对阵" in readme
    assert "胜负彩介绍" in readme


def test_disallowed_home_never_discovers_or_contacts_a_second_origin(replay, tmp_path):
    http, _ = replay
    robots(http, HOME, "User-agent: *\nDisallow: /\n")
    robots(http, OKOOO)
    http.get(OKOOO, status=405)
    archive = probe.collect_backup_source_probe(output=tmp_path)

    site = archive["candidates"][0]
    assert site["page_requests"] == 0 and site["robots_requests"] == 1
    assert [row["status"] for row in site["pages"]] == ["robots_denied"]
    assert len(http.calls) == 3


@pytest.mark.parametrize("status", ["blocked", "unconfirmed"])
def test_home_http_200_access_challenge_or_empty_body_cannot_discover(replay, tmp_path, status):
    http, _ = replay
    robots(http, HOME)
    robots(http, OKOOO)
    home = (T97 / "t97-probe-home.html").read_bytes()
    challenged = home.replace("中国足彩网首页离线样本".encode(), "安全验证".encode())
    http.get(HOME, body=challenged if status == "blocked" else b"")
    http.get(OKOOO, status=405)
    site = probe.collect_backup_source_probe(output=tmp_path)["candidates"][0]

    assert site["page_requests"] == site["robots_requests"] == 1
    assert [row["status"] for row in site["pages"]] == [status]
    assert len(http.calls) == 4
    assert site["assessment"]["jc"]["reason"].endswith(site["pages"][0]["reason"])


@pytest.mark.parametrize(
    ("rules", "interval"), [("", 3), ("Crawl-delay: 4\nRequest-rate: 1/5\n", 5)]
)
def test_both_discovered_pages_are_repeated_and_no_market_values_leave_the_probe(
    replay, tmp_path, rules, interval
):
    http, _ = replay
    homepage(http)
    for url in (SFC, JC, OKOOO):
        robots(http, url, ALLOW + rules)
    controls = {
        SFC: (FIX / "26170-sfc.html").read_bytes(),
        JC: (T97 / "t97-probe-jc.html").read_bytes(),
        OKOOO: (T97 / "t97-probe-jc.html").read_bytes(),
    }
    for url, body in controls.items():
        http.get(url, body=body)

    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    assert [site["page_requests"] for site in sites] == [5, 2]
    assert [site["page_limit"] for site in sites] == [6, 3]
    assert [site["robots_requests"] for site in sites] == [3, 1]
    requests_observed = sorted(
        datetime.fromisoformat(row["observed_utc"])
        for site in sites
        for row in (*site["robots"], *site["pages"])
    )
    assert all((b - a).total_seconds() >= 3 for a, b in pairwise(requests_observed))
    assert [row["url"] for row in sites[0]["pages"]] == [HOME, SFC, JC, SFC, JC]
    for site in sites:
        for purpose, verdict in site["assessment"].items():
            assert verdict["result"] == "可用"
            stability = verdict["stability"]
            assert stability["status"] == "consistent"
            assert stability["row_count_matches"] and stability["fields_match"]
            count = 14 if purpose == "sfc" else 1
            assert [reading["row_count"] for reading in stability["readings"]] == [count, count]
            attempts = [row for row in site["pages"] if row["purpose"] == purpose]
            a, b = (datetime.fromisoformat(row["observed_utc"]) for row in attempts)
            assert (b - a).total_seconds() >= interval
            assert all(
                row["sha256"] == hashlib.sha256(controls[row["url"]]).hexdigest()
                for row in attempts
            )
    for path in (tmp_path / "samples").glob("*.json"):
        payload = path.read_text()
        assert "88.765" not in payload and "987654321" not in payload
    assert "同页复读" in (tmp_path / "README.md").read_text()


@pytest.mark.parametrize("missing", ["row", "field"])
def test_repeat_differences_are_reported_without_calling_source_stable(replay, tmp_path, missing):
    http, _ = replay
    homepage(http)
    for url in (SFC, JC, OKOOO):
        robots(http, url)
    http.get(SFC, status=404)
    http.get(OKOOO, status=405)
    raw = (T97 / "t97-probe-jc.html").read_bytes()
    if missing == "row":
        new_row = raw.split(b"\n")[4].replace("周一001".encode(), "周一002".encode())
        second = raw.replace(b"</table>", new_row + b"\n</table>")
    else:
        second = raw.replace("演练联赛".encode(), b"")
    http.get(JC, body=raw)
    http.get(JC, body=second)

    site = probe.collect_backup_source_probe(output=tmp_path)["candidates"][0]
    verdict = site["assessment"]["jc"]
    assert verdict["result"] == "需所有者决定"
    assert verdict["stability"]["status"] == "different"
    assert verdict["stability"]["row_count_matches"] == (missing == "field")
    assert verdict["stability"]["fields_match"] == (missing == "row")


@pytest.mark.parametrize("show_time_header", [True, False])
def test_jc_pairs_without_kickoff_are_repeated_but_not_declared_complete(
    replay, tmp_path, show_time_header
):
    http, _ = replay
    homepage(http)
    for url in (SFC, JC, OKOOO):
        robots(http, url)
    http.get(SFC, status=404)
    http.get(OKOOO, status=405)
    raw = (T97 / "t97-probe-jc.html").read_bytes()
    time_value = b"2026-11-25 04:00"
    if show_time_header:
        raw = raw.replace(time_value, b"")
    else:
        raw = raw.replace("<th>开球时间</th>".encode(), b"")
        raw = raw.replace(b"<td>" + time_value + b"</td>", b"")
    http.get(JC, body=raw)
    http.get(JC, body=raw)

    site = probe.collect_backup_source_probe(output=tmp_path)["candidates"][0]
    verdict = site["assessment"]["jc"]
    assert verdict["result"] == "需所有者决定"
    assert "开球时间未核实" in verdict["reason"] or "核心赛程字段未齐" in verdict["reason"]
    assert verdict["stability"]["status"] == "consistent"
    assert [reading["pair_rows"] for reading in verdict["stability"]["readings"]] == [1, 1]
    assert [reading["row_count"] for reading in verdict["stability"]["readings"]] == [0, 0]
    assert verdict["fields"] == ["away", "home", "league", "seq"]


def test_window_in_robots_blocks_page_and_no_redirect_is_followed(replay, tmp_path):
    http, _ = replay
    homepage(http)
    robots(http, SFC)
    robots(http, JC)
    robots(http, OKOOO, "User-agent: *\nVisit-time: 0000-0001\n")
    http.get(SFC, status=302, headers={"Location": "https://example.org/jc/"})
    http.get(JC, status=429)
    sites = probe.collect_backup_source_probe(output=tmp_path)["candidates"]
    observed = {row["url"]: row for site in sites for row in site["pages"]}
    assert observed[OKOOO]["status"] == "outside_window"
    assert observed[SFC]["http_status"] == 302
    assert observed[JC]["http_status"] == 429
    assert len(http.calls) == 7


@pytest.mark.parametrize(("code", "url", "budget"), [("zgzcw", JC, 6), ("okooo", OKOOO, 3)])
def test_hard_candidate_request_budget(replay, tmp_path, code, url, budget):
    http, _ = replay
    (tmp_path / "samples").mkdir()
    robots(http, url)
    http.get(url, status=405)
    with probe.requests.Session() as session:
        candidate = probe._CandidateProbe(session, base.RateLimiter(3), tmp_path, code, code)
        for _ in range(budget):
            assert candidate.page("jc", url)["http_status"] == 405
        stopped = candidate.page("jc", url)
    assert stopped["status"] == "budget_exhausted"
    assert len(http.calls) == budget + 1
    assert candidate.result["robots_requests"] == 1
