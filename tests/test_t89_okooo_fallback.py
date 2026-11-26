"""T89 offline replays through collection, portable state and the daily digest."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
import responses
from lxml import html
from responses import matchers
from sqlalchemy import select

from test_sfc_official_pipeline import (
    FIXTURES,
    INDEX,
    NOW,
    SAMPLES,
    WENZHOU,
    ReplayClock,
    _setup_replay,
    _state,
)
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sfc_state import export_sfc_state
from zucai.sources import sfc
from zucai.sources.c500 import URL, C500Sfc
from zucai.sources.sporttery import BEIJING

OKOOO = "https://www.okooo.com"
BOT_RULES = b"User-agent: *\nAllow: /zucai/\nVisit-time: 0130-1830\n"
SALE_26170 = datetime(2026, 11, 24, 14, 1, tzinfo=UTC)


def _mock_500(http, case):
    if case == "root_failure":
        http.get(C500Sfc.url, status=503, match=[matchers.query_param_matcher({})])
    else:
        number = "26169" if case == "healthy" else "26171"
        http.get(
            C500Sfc.url,
            body=(SAMPLES / f"c500_{number}.html").read_bytes(),
            match=[matchers.query_param_matcher({})],
        )
    if case in {"due_complete", "due_incomplete"}:
        original = (SAMPLES / "c500_26169.html").read_bytes()
        if case == "due_incomplete":
            doc = html.fromstring(original.decode("gb18030"))
            doc.xpath('//table[@id="vsTable"]//tr[@data-cid]')[-1].drop_tree()
            original = html.tostring(doc, encoding="unicode").encode("gb18030")
        http.get(
            C500Sfc.url,
            body=original,
            match=[matchers.query_param_matcher({"expect": "26169"})],
        )
    http.get(URL, body=(FIXTURES / "t26/full-bridge.html").read_bytes())


def _okooo_page(number, change=None):
    target = "26170" if change == "wrong_issue" else number
    raw = (FIXTURES / f"okooo/{target}.html").read_bytes()
    if change not in {"13_rows", "swapped"}:
        return raw
    doc = html.fromstring(raw.decode("utf-8"))
    rows = doc.xpath('//table[@id="table4"]//tr[td]')
    if change == "13_rows":
        rows[-1].drop_tree()
    else:
        (home,) = rows[5].xpath('.//*[contains(@class,"homenameobj")]')
        (away,) = rows[5].xpath('.//*[contains(@class,"awaynameobj")]')
        home.text, away.text = away.text, home.text
    return html.tostring(doc, encoding="utf-8")


@pytest.fixture
def replay(tmp_path, monkeypatch):
    _setup_replay(tmp_path, monkeypatch)
    # The red replay runs against main before the production adapter exists.
    if importlib.util.find_spec("zucai.sources.okooo_sfc"):
        from zucai.sources import okooo_sfc

        monkeypatch.setattr(okooo_sfc, "datetime", ReplayClock)

    def collect(
        *, primary="root_failure", backup=None, now=NOW, number="26169", seed=None, notice=True
    ):
        monkeypatch.setattr(ReplayClock, "instant", now)
        db = tmp_path / "data/feed.db"
        if seed is not None:
            engine = get_engine(db)
            init_db(engine)
            try:
                with session_scope(engine) as session:
                    seed(session)
            finally:
                engine.dispose()
        with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
            _mock_500(http, primary)
            http.get("https://www.wztc.cn/robots.txt", status=404)
            http.get(INDEX, body=(SAMPLES / "wenzhou_index.html").read_bytes())
            http.get(
                WENZHOU,
                body=(SAMPLES / "wenzhou_20261118.html").read_bytes(),
                status=200 if notice else 503,
            )
            http.get(OKOOO + "/robots.txt", body=BOT_RULES)
            http.get(
                f"{OKOOO}/zucai/{number}/",
                body=_okooo_page(number, backup),
                status={"405": 405, "redirect": 302}.get(backup, 200),
                headers={"Location": OKOOO + "/zucai/"} if backup == "redirect" else {},
            )
            result = sfc.collect_current_sfc(db)
            requests = [
                call.request.url
                for call in http.calls
                if urlsplit(call.request.url).hostname == "www.okooo.com"
            ]
        archive = tmp_path / "archive"
        export_feeds(db, archive, now.astimezone(BEIJING).date(), "t89", collection={"sfc": result})
        digest = generate(archive, tmp_path / "digest", now=now, run_id="t89")
        return result, requests, archive, digest.read_text(encoding="utf-8")

    return collect


@pytest.mark.parametrize("primary", ["root_failure", "due_incomplete"])
def test_backup_collected_for_the_due_period_and_restored(replay, tmp_path, primary):
    result, requested, archive, digest = replay(primary=primary)
    expected = f"{OKOOO}/zucai/26169/"
    assert requested == [OKOOO + "/robots.txt", expected], result
    assert result["status"] == "complete" and result["issue_no"] == "26169", result
    assert result["okooo"]["status"] == "collected"
    assert result["okooo"]["issue_no"] == "26169"
    assert result["okooo"]["requests"] == result["okooo"]["request_limit"] == 2
    assert result["okooo"]["robots_requests"] == result["okooo"]["page_requests"] == 1
    assert "本期名单来自备用来源澳客" in digest and "期号：26169" in digest
    issue = _state(archive, "26169")
    assert issue["source_url"] == expected
    assert [row["seq"] for row in issue["snapshot"]] == list(range(1, 15))
    assert issue["snapshot"][0]["fixture_id"] == "1347499"
    assert issue["official_verification"]["provider"] == "okooo.com"
    assert issue["official_verification"]["official"]["source_url"] == WENZHOU
    assert issue["official_verification"]["status"] == "pending"

    restored = tmp_path / "restored/feed.db"
    restore_feeds(archive, restored)
    with sqlite3.connect(restored) as connection:
        export_sfc_state(connection, tmp_path / "roundtrip")
    assert _state(tmp_path / "roundtrip", "26169") == issue
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    assert manifest["collection"]["sfc"]["okooo"] == result["okooo"]


@pytest.mark.parametrize("primary", ["healthy", "due_complete"])
def test_complete_500_page_does_not_request_a_backup(replay, primary):
    result, requested, archive, digest = replay(primary=primary)
    assert not requested
    assert result["status"] == "complete" and result["issue_no"] == "26169"
    source_url = _state(archive, "26169")["source_url"]
    assert source_url == C500Sfc.url + ("?expect=26169" if primary == "due_complete" else "")
    assert "本期名单来自备用来源澳客" not in digest


@pytest.mark.parametrize(
    ("backup", "reason"),
    [
        ("405", "405"),
        ("13_rows", "场序"),
        ("wrong_issue", "期号"),
        ("outside", "时段"),
        ("redirect", "302"),
    ],
)
def test_unusable_backup_never_retries_or_becomes_a_lineup(replay, backup, reason):
    outside = backup == "outside"
    now = datetime(2026, 11, 23, 0, tzinfo=UTC) if outside else NOW
    result, requested, archive, digest = replay(backup=backup, now=now)
    expected = [OKOOO + "/robots.txt"]
    if not outside:
        expected.append(OKOOO + "/zucai/26169/")
    assert requested == expected, result
    assert not (archive / "state/sfc/26169.json").exists()
    assert result["okooo"]["status"] == "unavailable"
    assert result["okooo"]["requests"] == len(expected)
    assert reason in result["okooo"]["reason"]
    assert "澳客不可用" in digest and reason in digest
    assert "本期名单来自备用来源澳客" not in digest


def test_failed_500_without_a_known_target_does_not_use_the_backup_homepage(replay):
    result, requested, archive, digest = replay(notice=False)
    assert requested == []
    assert result["issue_no"] is None
    assert not list(archive.glob("state/sfc/*.json"))
    assert "本期名单来自备用来源澳客" not in digest


def _store_matches(session):
    bruj, liverpool, paris, roma = [
        Team(name=name, aliases=["日尔曼"] if name == "巴黎圣日尔曼" else [])
        for name in ("布鲁日", "利物浦", "巴黎圣日尔曼", "罗马")
    ]
    session.add_all((bruj, liverpool, paris, roma))
    session.flush()
    for number, home, away in (
        ("jc:exact-home-away", bruj, liverpool),
        ("jc:general-alias-only", paris, roma),
    ):
        session.add(
            Match(
                match_id=number,
                source="jc",
                league="欧冠",
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=datetime(2026, 11, 25, 20, tzinfo=UTC),
                status="scheduled",
            )
        )


def test_unapproved_short_name_does_not_hide_rows_with_exact_identity(replay, tmp_path):
    result, requested, archive, digest = replay(now=SALE_26170, number="26170", seed=_store_matches)
    assert result["issue_no"] == "26170" and requested[-1].endswith("/26170/")
    issue = _state(archive, "26170")
    exact, short = (issue["snapshot"][index] for index in (5, 8))
    assert exact["fixture_id"] == "1347511" and exact["match_id"] == "jc:exact-home-away"
    assert short["home"] == "日尔曼" and short["match_id"] is None
    assert short["mapping"] == "unmatched"
    assert exact["provider_home_id"] is None and short["provider_home_id"] is None
    assert result["mapped"] == 1 and result["unmatched"] == 13
    assert "布鲁日" in digest and "日尔曼" in digest and "未映射" in digest

    db = tmp_path / "restored/feed.db"
    restore_feeds(archive, db)
    engine = get_engine(db)
    try:
        with session_scope(engine) as session:
            assert set(session.scalars(select(Match.match_id))) == {
                "jc:exact-home-away",
                "jc:general-alias-only",
            }
    finally:
        engine.dispose()


def test_official_conflict_from_backup_names_the_real_provider(replay):
    _, _, archive, digest = replay(now=SALE_26170, number="26170", backup="swapped")
    issue = _state(archive, "26170")
    check = issue["official_verification"]
    assert check["status"] == "conflict" and check["provider"] == "okooo.com"
    assert "第 6 场主队：官方公告写『布鲁日』，澳客写『利物浦』" in digest
    assert "第 6 场客队：官方公告写『利物浦』，澳客写『布鲁日』" in digest
    assert "本期不展示单选、复式和任九" in digest
