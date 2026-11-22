"""Offline SFC collection, portable archive, restore and digest regression for period 26171."""

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests
import responses
from responses import matchers

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sfc_state import export_sfc_state, restore_sfc_state
from zucai.sources import base, sfc, sfc_official
from zucai.sources.c500 import URL, C500Sfc
from zucai.sources.sfc_official import parse_notice

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
SAMPLES = FIXTURES / "sfc_official"
INDEX = "https://www.wztc.cn/zczq/index.shtml"
WENZHOU = "https://www.wztc.cn/system/2026/11/18/105874264.shtml"
NOW = datetime(2026, 11, 22, 16, tzinfo=UTC)
BEIJING_DAY = date(2026, 11, 23)


class ReplayClock(datetime):
    @classmethod
    def now(cls, tz=None):
        assert tz is not None
        return NOW.astimezone(tz)


def _mock_provider(http):
    for params, number in (({}, "26171"), ({"expect": "26169"}, "26169")):
        http.get(
            C500Sfc.url,
            body=(SAMPLES / f"c500_{number}.html").read_bytes(),
            match=[matchers.query_param_matcher(params)],
        )
    http.get(URL, body=(FIXTURES / "t26/full-bridge.html").read_bytes())


def _archive(tmp_path, monkeypatch, discover=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(base, "datetime", ReplayClock)
    monkeypatch.setattr(sfc, "datetime", ReplayClock)
    monkeypatch.setattr(base.RateLimiter, "wait", lambda self: None)
    monkeypatch.setattr(sfc_official, "INDEXES", (INDEX,))
    if discover is not None:
        monkeypatch.setattr(sfc, "fetch_official_notices", discover)
    path = tmp_path / "data/feed.db"
    path.parent.mkdir()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _mock_provider(http)
        if discover is None:
            http.get("https://www.wztc.cn/robots.txt", status=404)
            http.get(INDEX, body=(SAMPLES / "wenzhou_index.html").read_bytes())
            http.get(WENZHOU, body=(SAMPLES / "wenzhou_20261118.html").read_bytes())
        result = sfc.collect_current_sfc(path)
    assert result["status"] == "complete", result["errors"]
    archive = tmp_path / "archive"
    export_feeds(path, archive, BEIJING_DAY, "replay", collection={"sfc": result})
    report = generate(archive, tmp_path / "digest", now=NOW, run_id="regression")
    assert report.name == "2026-11-23.md"
    return result, archive, report.read_text(encoding="utf-8")


def _state(archive, number):
    return json.loads((archive / f"state/sfc/{number}.json").read_bytes())["issue"]


def _notice(raw=None, url=WENZHOU):
    return parse_notice(
        raw or (SAMPLES / "wenzhou_20261118.html").read_bytes(),
        source_url=url,
        observed_utc=NOW.isoformat(),
    )


def test_26171_root_selects_26169_and_archives_verification(tmp_path, monkeypatch):
    result, archive, rendered = _archive(tmp_path, monkeypatch)
    assert result["root_issue_no"] == "26171" and result["issue_no"] == "26169"
    assert "期号：26169" in rendered and "期号：26171" not in rendered
    windows = result["official_schedule"]
    on_sale = {
        number
        for number, window in windows.items()
        if datetime.fromisoformat(window["sale_start_utc"])
        <= NOW
        < datetime.fromisoformat(window["sale_end_utc"])
    }
    assert on_sale == {"26169", "26170", "26171"}
    check = _state(archive, "26171")["official_verification"]
    assert check["status"] == "pending"  # e.g. 伯恩茅 vs 伯恩茅斯, no approved mapping
    assert set(check["compared_fields"]) == {"issue_no", "sale_end_utc", "seq", "home", "away"}
    assert check["official"]["source_url"] == WENZHOU
    assert (
        check["official"]["raw_sha256"]
        == hashlib.sha256((SAMPLES / "wenzhou_20261118.html").read_bytes()).hexdigest()
    )
    assert len(check["official"]["fixtures"]) == 14
    assert any(
        d["field"] == "away" and d["seq"] == 1 and d["status"] == "pending"
        for d in check["differences"]
    )
    assert all(d["field"] != "sale_end_utc" for d in check["differences"])
    assert "待核" in rendered

    # Use the real restore/export path to verify that evidence survives a later cloud run.
    restored = tmp_path / "restored/feed.db"
    restore_feeds(archive, restored)
    with sqlite3.connect(restored) as db:
        export_sfc_state(db, tmp_path / "roundtrip")
    assert _state(tmp_path / "roundtrip", "26171") == _state(archive, "26171")


@pytest.mark.parametrize("field", ["sale_end_utc", "home"])
def test_adjusted_official_deadline_or_swapped_home_reported(tmp_path, monkeypatch, field):
    # Construct a newer test-only revision from an actual notice; retain both versions.
    raw = (SAMPLES / "wenzhou_20261118.html").read_bytes()
    revised = raw.replace(b"2026-11-18 15:22:27", b"2026-11-22 15:22:27")
    revised = revised.replace(
        "2026年11月17日".encode("gb18030"), "2026年11月22日".encode("gb18030")
    )
    if field == "sale_end_utc":
        revised = revised.replace(b"2026-11-24 22:00", b"2026-11-24 21:30")
    else:
        # Arsenal vs Dortmund: only the home side has the same label in both sources.
        left, right = (s.encode("gb18030") for s in ("阿森纳", "多特蒙德"))
        revised = revised.replace(left, b"$SIDE$").replace(right, left).replace(b"$SIDE$", right)
    amended = WENZHOU.replace("105874264", "999999999")
    notices = _notice() + _notice(revised, amended)
    _, archive, rendered = _archive(tmp_path, monkeypatch, lambda: notices)
    check = _state(archive, "26169")["official_verification"]
    assert check["status"] == "conflict" and "冲突" in rendered
    assert check["official"]["source_url"] == amended
    assert any(d["field"] == field and d["status"] == "conflict" for d in check["differences"])


def test_official_outage_keeps_third_party_source(tmp_path, monkeypatch):
    def unavailable():
        raise requests.Timeout("official schedule unavailable")

    _, archive, rendered = _archive(tmp_path, monkeypatch, unavailable)
    issue = _state(archive, "26171")
    assert issue["source_url"] == C500Sfc.url
    assert issue["official_verification"]["status"] == "pending"
    assert "待核" in rendered and "第三方" in rendered


def test_legacy_26171_state_still_restores(tmp_path):
    key = "state/sfc/26171.json"
    archived = tmp_path / "legacy" / key
    archived.parent.mkdir(parents=True)
    archived.write_bytes((SAMPLES / "legacy_26171.json").read_bytes())
    old = _state(tmp_path / "legacy", "26171")
    dest = tmp_path / "db.sqlite"
    engine = get_engine(dest)
    init_db(engine)
    # Feeds restore normally loads its match table before restoring issue snapshots.
    linked = next(row for row in old["snapshot"] if row["match_id"])
    identity = linked["matched_identity"]
    with session_scope(engine) as session:
        home, away = Team(name=identity["home"]), Team(name=identity["away"])
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id=linked["match_id"],
                source=identity["source"],
                league=identity["league"],
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=identity["kickoff_utc"],
            )
        )
    engine.dispose()
    with sqlite3.connect(dest) as db:
        restore_sfc_state(db, tmp_path / "legacy", [key])
        export_sfc_state(db, tmp_path / "restored")
    restored = _state(tmp_path / "restored", "26171")
    assert restored["snapshot"] == old["snapshot"]
    assert not restored.get("official_verification")


def test_new_attempt_preserves_last_known_window_only_for_transport_failure(tmp_path, monkeypatch):
    initial, _, _ = _archive(tmp_path, monkeypatch)
    database = tmp_path / "data/feed.db"
    number = "26171"
    for exc in (requests.Timeout("offline"), ValueError("最新调整通知无法定位适用期号")):

        def fail(error=exc):
            raise error

        monkeypatch.setattr(sfc, "fetch_official_notices", fail)
        with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
            _mock_provider(http)
            result = sfc.collect_current_sfc(
                database, previous_schedule=initial["official_schedule"]
            )
        assert result["status"] == "complete", result["errors"]
        with sqlite3.connect(database) as db:
            check = json.loads(
                db.execute(
                    "SELECT official_verification FROM sfc_issues WHERE issue_no=?", (number,)
                ).fetchone()[0]
            )
        assert check["status"] == "pending" and not check["official"]
        if isinstance(exc, requests.Timeout):
            assert result["official_schedule"] == initial["official_schedule"]
            assert (
                check["last_known_official"]["raw_sha256"]
                == initial["official_schedule"][number]["raw_sha256"]
            )
        else:
            assert result["official_schedule"] == {} and "last_known_official" not in check


def test_official_period_available_when_500_root_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sfc, "datetime", ReplayClock)
    monkeypatch.setattr(sfc, "fetch_official_notices", _notice)
    database = tmp_path / "data/feed.db"
    with responses.RequestsMock() as http:
        http.get(C500Sfc.url, status=503)
        result = sfc.collect_current_sfc(database)
    assert (
        result["status"] == "partial"
        and result["official_schedule"]["26169"]["source_url"] == WENZHOU
    )
    archive = tmp_path / "archive"
    export_feeds(database, archive, BEIJING_DAY, "offline-provider", collection={"sfc": result})
    report = generate(archive, tmp_path / "digest", now=NOW, run_id="provider-offline")
    text = report.read_text(encoding="utf-8")
    assert "期号：26169" in text and "第三方场次快照待采集" in text


def test_published_issue_without_readable_sale_window_is_not_on_sale(tmp_path, monkeypatch):
    def listing():
        return [dict(n, sale_start_utc=None, sale_end_utc=None, fixtures=[]) for n in _notice()]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sfc, "datetime", ReplayClock)
    monkeypatch.setattr(sfc, "fetch_official_notices", listing)
    database = tmp_path / "data/feed.db"
    with responses.RequestsMock() as http:
        http.get(C500Sfc.url, status=503)
        result = sfc.collect_current_sfc(database)
    archive = tmp_path / "archive"
    export_feeds(database, archive, BEIJING_DAY, "no-table", collection={"sfc": result})
    report = generate(archive, tmp_path / "digest", now=NOW, run_id="not-yet-known")
    text = report.read_text(encoding="utf-8")
    assert "官方已公布期号" in text and "26171" in text and "开售与停售时间待核" in text
    assert "期号：26171" not in text


def test_verification_cannot_predate_the_team_identity_bridge(tmp_path, monkeypatch):
    bridge_time = NOW + timedelta(seconds=35)
    monkeypatch.setattr(
        sfc, "_sequence_time", lambda meta: bridge_time if meta["url"] == URL else NOW
    )
    _, archive, digest = _archive(tmp_path, monkeypatch)
    check = _state(archive, "26169")["official_verification"]
    assert datetime.fromisoformat(check["checked_at"]) == bridge_time
    assert "本次核对：" not in digest  # The digest cutoff is NOW, before the bridge arrived.
