from pathlib import Path

import pytest
import responses
from lxml import html

from zucai.sources import sfc_official
from zucai.sources.base import RawStore
from zucai.sources.sfc_official import parse_notice

FIXTURES = Path(__file__).parents[1] / "data/fixtures/sfc_official"
INDEX = "https://www.wztc.cn/zczq/index.shtml"
ARTICLE = "https://www.wztc.cn/system/2026/11/18/105874264.shtml"
CAPTURED = "2026-11-22T16:00:00Z"


def parse(raw):
    return parse_notice(raw, source_url=ARTICLE, observed_utc=CAPTURED)


def test_same_number_in_other_game_cannot_supply_missing_table():
    doc = html.fromstring((FIXTURES / "wenzhou_20261118.html").read_bytes().decode("gb18030"))
    target = doc.xpath("//table")[5]
    target.drop_tree()
    # A 6场半全场 appendix with the same issue number cannot stand in for the missing 14.
    raw = html.tostring(doc, encoding="unicode").replace("26233", "26171")
    raw = raw.replace('charset="gb2312"', 'charset="utf-8"').encode()
    rows = {n["issue_no"]: n for n in parse(raw)}
    assert len(rows) == 6
    assert rows["26171"]["fixtures"] == []
    assert rows["26171"]["sale_end_utc"] is None


def test_image_only_official_notice_binds_issue_not_fixtures_or_deadline():
    rows = parse((FIXTURES / "gansu_image_only.html").read_bytes())
    assert [n["issue_no"] for n in rows] == [str(n) for n in range(26166, 26172)]
    assert all(n["notice_date"] == "2026-11-17" and n["fixtures"] == [] for n in rows)
    assert all(n["sale_start_utc"] is n["sale_end_utc"] is None for n in rows)


def test_known_revision_unreadable_blocks_earlier_versions(tmp_path, monkeypatch):
    index = (FIXTURES / "wenzhou_index.html").read_text()
    # Simulated newer revision. Only the index of the weekly schedule is a real excerpt.
    updated = ARTICLE.replace("18/105874264", "19/105874265")
    title = "关于调整足球彩票11月26日奖期竞猜场次的通知"
    index = index.replace("<body>", f'<body><a href="{updated}">{title}</a>')
    monkeypatch.setattr(sfc_official, "INDEXES", (INDEX,))
    monkeypatch.setattr(sfc_official, "RawStore", lambda: RawStore(tmp_path))
    monkeypatch.setattr(sfc_official.RateLimiter, "wait", lambda self: None)
    with responses.RequestsMock() as http:
        http.get("https://www.wztc.cn/robots.txt", status=404)
        http.get(INDEX, body=index)
        http.get(updated, status=503)
        with pytest.raises(ValueError, match="公告无法核对"):
            sfc_official.fetch_official_notices()


def test_robots_blocks_bot_even_when_browser_token_is_allowed(tmp_path):
    with responses.RequestsMock() as http:
        http.get(
            "https://www.wztc.cn/robots.txt",
            body="User-agent: ZucaiResearch\nDisallow: /zczq/\n\nUser-agent: *\nAllow: /",
        )
        with sfc_official.requests.Session() as session:
            source = sfc_official.OfficialNotices(session, RawStore(tmp_path))
            with pytest.raises(ValueError, match="robots.txt 禁止"):
                source.fetch_raw(url=INDEX)


def test_offsite_revision_is_not_an_official_article():
    index = (FIXTURES / "wenzhou_index.html").read_bytes().replace(b"www.wztc.cn", b"third.invalid")
    with pytest.raises(ValueError, match="未核准"):
        sfc_official._links(index, INDEX)


def test_fetch_reconciles_genuine_two_centre_publications(monkeypatch, tmp_path):
    from zucai.sfc_verification import notice_for_issue

    gansu = "https://www.gstc.org.cn"
    monkeypatch.setattr(sfc_official, "INDEXES", (INDEX, gansu + "/newsList/110300"))
    store = RawStore(tmp_path)
    monkeypatch.setattr(sfc_official, "RawStore", lambda: store)
    monkeypatch.setattr(sfc_official.RateLimiter, "wait", lambda self: None)
    with responses.RequestsMock() as http:
        for origin in ("https://www.wztc.cn", gansu):
            http.get(origin + "/robots.txt", status=404)
        for url, fixture in (
            (INDEX, "wenzhou_index.html"),
            (ARTICLE, "wenzhou_20261118.html"),
            (gansu + "/newsList/110300", "gansu_index.html"),
            (gansu + "/newsDetail/110300/80791", "gansu_image_only.html"),
        ):
            http.get(url, body=(FIXTURES / fixture).read_bytes())
        notices = sfc_official.fetch_official_notices()
    versions = [n for n in notices if n["issue_no"] == "26171"]
    assert len(versions) == 2
    assert [len(n["fixtures"]) for n in versions] == [14, 0]
    cutoff = max("2026-11-22T16:00:00+00:00", *(n["observed_utc"] for n in versions))
    selected = notice_for_issue("26171", notices, cutoff)
    assert selected["source_url"] == ARTICLE
    assert len(selected["fixtures"]) == 14


def test_amendment_in_index_has_priority_over_older_week(monkeypatch, tmp_path):
    raw = (FIXTURES / "wenzhou_20261118.html").read_bytes().decode("gb18030")
    # Simulate the published form of a national amendment: updated notice and appendix.
    revision = raw.replace(
        "关于下发足球彩票11月20日-11月26日奖期竞猜场次安排", "关于调整足球彩票11月26日奖期竞猜场次"
    ).replace("11月17日", "11月19日")
    revision = revision.replace("2026-11-18 15:22:27", "2026-11-19 14:45:00")
    revision = revision.replace("2026-11-26 22:00", "2026-11-26 21:00")
    updated = ARTICLE.replace("18/105874264", "19/105874265")
    index = (FIXTURES / "wenzhou_index.html").read_text()
    index = index.replace(
        "</body>", f'<a href="{updated}">关于调整足球彩票11月26日奖期竞猜场次的通知</a></body>'
    )
    old = ARTICLE.replace("18/105874264", "11/105870176")
    index = index.replace(
        "</body>",
        f'<a href="{old}">关于下发足球彩票11月13日-11月19日奖期竞猜场次安排的通知</a></body>',
    )
    monkeypatch.setattr(sfc_official, "INDEXES", (INDEX,))
    monkeypatch.setattr(sfc_official, "RawStore", lambda: RawStore(tmp_path))
    monkeypatch.setattr(sfc_official.RateLimiter, "wait", lambda self: None)
    with responses.RequestsMock() as http:
        http.get("https://www.wztc.cn/robots.txt", status=404)
        http.get(INDEX, body=index)
        http.get(ARTICLE, body=raw.encode("gb18030"))
        http.get(updated, body=revision.encode("gb18030"))
        http.get(old, status=503)
        rows = sfc_official.fetch_official_notices()
    versions = [n for n in rows if n["issue_no"] == "26171"]
    assert [(n["kind"], n["sale_end_utc"][11:16]) for n in versions] == [
        ("amendment", "13:00"),
        ("schedule", "14:00"),
    ]
