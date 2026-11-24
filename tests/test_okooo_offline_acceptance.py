"""Repeat the two fallback rounds using only captured fixtures and a frozen clock."""

import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest
import requests
from lxml import html
from responses import RequestsMock

from okooo_offline_replay import replay
from zucai import okooo_probe
from zucai.sfc_club_names import load as approved_club_names
from zucai.sources.sfc_official import parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"
SAMPLES = FIXTURES / "okooo_offline"
NOTICE = "https://www.wztc.cn/system/2026/11/18/105874264.shtml"
NOW = "2026-11-24T14:50:06+00:00"


@pytest.fixture
def inputs():
    evidence = json.loads((SAMPLES / "schedule_evidence.json").read_bytes())
    raw_notice = (FIXTURES / "sfc_official/wenzhou_20261118.html").read_bytes()
    assert sha256(raw_notice).hexdigest() == evidence["official_notice_fixture_sha256"]
    notices = parse_notice(
        raw_notice,
        source_url=NOTICE,
        observed_utc="2026-11-23T00:00:00+00:00",
    )
    return notices, evidence["issues"]


@pytest.fixture
def observe(tmp_path, monkeypatch):
    pages = json.loads((SAMPLES / "pages.json").read_bytes())

    def read(filename, expect, *, change=None, at=None):
        record = pages[filename]
        raw = (SAMPLES / filename).read_bytes()
        assert sha256(raw).hexdigest() == record["fixture_sha256"]
        if change:
            raw = change(raw)
        url = okooo_probe.ORIGIN + ("/zucai/" if record["game"] == "sfc" else "/zucai/ren9/")
        url += f"{expect}/"

        class ReplayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(at or record["observed_utc"]).astimezone(tz)

        with monkeypatch.context() as context, RequestsMock() as responses:
            context.setattr(okooo_probe, "datetime", ReplayClock)
            responses.get(url, status=record["http_status"], body=raw)
            with requests.Session() as session:
                return okooo_probe.observe(session, url, expect, record["game"], tmp_path)

    return read


def test_500_same_issue_takes_precedence(inputs):
    primary = (FIXTURES / "sfc_official/c500_26171.html").read_bytes()

    def unused_pages():
        pytest.fail("500 正常时不读澳客页面")
        yield

    result = replay("26171", NOW, primary, unused_pages(), *inputs)
    assert result["candidate"]["provider"] == "500.com"
    assert [row["seq"] for row in result["candidate"]["fixtures"]] == list(range(1, 15))
    assert all("澳客" not in reason for reason in result["pending_reasons"])
    assert result["candidate"]["official_status"] == "pending"


@pytest.mark.parametrize(
    "issue,failed_game,backup_game,first_match,first_kickoff,reviewed_fields",
    [
        ("26170", "sfc", "ren9", "jc:2043004", "2026-11-25T18:00:00+08:00", 19),
        ("26171", "ren9", "sfc", "apif:1636244", "2026-11-27T01:45:00+08:00", 18),
    ],
)
def test_500_missing_one_actual_405_allows_other_same_issue_page(
    observe, inputs, issue, failed_game, backup_game, first_match, first_kickoff, reviewed_fields
):
    failure = observe(f"{issue}-{failed_game}-405.html", issue)
    assert failure["http_status"] == 405 and failure["rows"] == []
    backup = observe(f"{issue}-{backup_game}.html", issue)
    result = replay(issue, NOW, None, [failure, backup], *inputs)
    candidate = result["candidate"]
    assert candidate["provider"] == "okooo.com" and candidate["game"] == backup_game
    assert candidate["issue_no"] == issue
    assert [row["seq"] for row in candidate["fixtures"]] == list(range(1, 15))
    assert candidate["fixtures"][0]["project_match_id"] == first_match
    assert candidate["fixtures"][0]["kickoff_beijing"] == first_kickoff
    assert all(row["evidence_urls"] for row in candidate["fixtures"])
    assert all(
        row["home_team_id"] is None and row["away_team_id"] is None for row in candidate["fixtures"]
    )
    assert candidate["official_status"] == "pending"
    assert len(candidate["official_pending_fields"]) == reviewed_fields
    assert any("HTTP 405" in reason for reason in result["pending_reasons"])
    assert any("公告" in reason for reason in result["pending_reasons"])
    print(issue, backup_game, f"候选 {len(candidate['fixtures'])} 场", result["pending_reasons"])

    without_backup = replay(issue, NOW, None, [failure], *inputs)
    assert without_backup["candidate"] is None
    assert any("HTTP 405" in reason for reason in without_backup["pending_reasons"])


@pytest.mark.parametrize("issue", ["26170", "26171"])
def test_agreeing_pages_yield_one_candidate(observe, inputs, issue):
    pages = [observe(f"{issue}-{game}.html", issue) for game in ("sfc", "ren9")]
    for order in (pages, pages[::-1]):
        result = replay(issue, NOW, None, order, *inputs)
        assert result["candidate"]["game"] == "sfc"
        assert len(result["candidate"]["fixtures"]) == 14
        assert result["candidate"]["official_status"] == "pending"


def test_same_issue_homepage_is_not_a_numbered_backup(observe, inputs):
    page = observe("26170-sfc.html", "26170")
    page.update(expected=None, url=okooo_probe.ORIGIN + "/zucai/")
    result = replay("26170", NOW, None, [page], *inputs)
    assert result["candidate"] is None
    assert any("按期页" in reason for reason in result["pending_reasons"])


def mutate(raw, change):
    doc = html.fromstring(raw.decode("utf-8"))
    rows = doc.xpath('//table[@id="table4"]//tr[td]')
    first = rows[0]
    if change == "13":
        rows[-1].drop_tree()
    elif change == "duplicate_seq":
        (seq,) = rows[1].xpath('.//*[contains(@class,"xh")]')
        seq.text = "1"
    elif change == "swapped":
        (home,) = first.xpath('.//*[contains(@class,"homenameobj")]')
        (away,) = first.xpath('.//*[contains(@class,"awaynameobj")]')
        home.text, away.text = away.text, home.text
    elif change == "kickoff":
        (cell,) = first.xpath('.//*[contains(@class,"timetd")]')
        cell.set("title", cell.get("title").replace("18:00:00", "18:05:00"))
    elif change == "match_id":
        for link in first.xpath('.//a[contains(@href,"/soccer/match/")]'):
            link.set("href", link.get("href").replace("/1346894/", "/9999999/"))
    elif change == "deadline":
        (label,) = doc.xpath('//*[contains(@class,"overTime")]/em')
        label.text = label.text_content().replace("17:30", "17:35")
    return html.tostring(doc, encoding="utf-8")


@pytest.mark.parametrize(
    "scenario,expected_reason",
    [
        ("wrong_issue", "期号"),
        ("13", "场序"),
        ("duplicate_seq", "场序"),
        ("swapped", "主客"),
        ("kickoff", "开球"),
        ("match_id", "比赛编号"),
        ("deadline", "停售"),
        ("no_evidence", "赛程证据缺失"),
        ("no_notice", "官方公告"),
    ],
)
def test_unproven_page_produces_no_candidate(observe, inputs, scenario, expected_reason):
    notices, evidence = deepcopy(inputs)
    if scenario == "wrong_issue":
        page = observe("26171-sfc.html", "26170")
    else:
        page = observe("26170-sfc.html", "26170", change=lambda raw: mutate(raw, scenario))
    if scenario == "no_evidence":
        evidence["26170"].pop(5)
    if scenario == "no_notice":
        notices = []
    result = replay("26170", NOW, None, [page], notices, evidence)
    assert result["candidate"] is None
    assert any(expected_reason in reason for reason in result["pending_reasons"])
    print(scenario, result["pending_reasons"])


@pytest.mark.parametrize("change", ["match_id", "kickoff", "deadline", "swapped"])
def test_conflicting_readable_pages_are_not_selected(observe, inputs, change):
    original = observe("26170-sfc.html", "26170")
    conflicting = observe("26170-ren9.html", "26170", change=lambda raw: mutate(raw, change))
    assert original["read_status"] == conflicting["read_status"] == "complete"
    for pair in ([original, conflicting], [conflicting, original]):
        result = replay("26170", NOW, None, pair, *inputs)
        assert result["candidate"] is None
        assert any("页面冲突" in reason for reason in result["pending_reasons"])
    print("双页冲突", change, result["pending_reasons"])


@pytest.mark.parametrize(
    "now,has_candidate",
    [
        ("2026-11-25T09:29:59+00:00", True),
        ("2026-11-25T09:30:00+00:00", False),
        ("2026-11-25T09:30:01+00:00", False),
    ],
)
def test_candidate_stops_at_official_deadline(observe, inputs, now, has_candidate):
    result = replay("26170", now, None, [observe("26170-sfc.html", "26170", at=now)], *inputs)
    assert (result["candidate"] is not None) is has_candidate
    if not has_candidate:
        assert any("已到停售" in reason for reason in result["pending_reasons"])
    print(now, has_candidate, result["pending_reasons"])


def test_500_approved_names_do_not_approve_okooo_short_names(observe, inputs):
    approved = approved_club_names()
    assert approved["英冠", "728"]["approval_pr"].endswith("/221")
    result = replay("26170", NOW, None, [observe("26170-sfc.html", "26170")], *inputs)
    pending = result["candidate"]["official_pending_fields"]
    assert any(
        d["seq"] == 1
        and d["field"] == "home"
        and d["provider"] == "大阪"
        and d["status"] == "pending"
        for d in pending
    )
    assert all(
        row["home_team_id"] is None and row["away_team_id"] is None
        for row in result["candidate"]["fixtures"]
    )
