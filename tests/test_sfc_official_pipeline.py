"""Offline SFC collection, portable archive, restore and digest regression for period 26171."""

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests
import responses
from lxml import html
from responses import matchers

from sfc_publication_support import prepare_remote
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, Team
from zucai.report.render import _json_bytes
from zucai.report.sfc import build_sfc_report, write_sfc_report
from zucai.sfc_reports import read_report
from zucai.sfc_sequence import known_official
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
    instant = NOW

    @classmethod
    def now(cls, tz=None):
        assert tz is not None
        return cls.instant.astimezone(tz)


def _mock_provider(http):
    for params, number in (({}, "26171"), ({"expect": "26169"}, "26169")):
        http.get(
            C500Sfc.url,
            body=(SAMPLES / f"c500_{number}.html").read_bytes(),
            match=[matchers.query_param_matcher(params)],
        )
    http.get(URL, body=(FIXTURES / "t26/full-bridge.html").read_bytes())


def _setup_replay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(base, "datetime", ReplayClock)
    monkeypatch.setattr(sfc, "datetime", ReplayClock)
    monkeypatch.setattr(base.RateLimiter, "wait", lambda self: None)
    monkeypatch.setattr(sfc_official, "INDEXES", (INDEX,))


def _archive(tmp_path, monkeypatch, discover=None):
    _setup_replay(tmp_path, monkeypatch)
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


def _mock_replay(http, *, revision=None):
    _mock_provider(http)
    weekly = (SAMPLES / "wenzhou_20261118.html").read_bytes()
    index = (SAMPLES / "wenzhou_index.html").read_text(encoding="utf-8")
    if revision is not None:
        publication_day, code, readable = revision
        link = f"https://www.wztc.cn/system/2026/11/{publication_day}/{code}.shtml"
        title = "关于调整足球彩票第26169期竞猜场次的通知"
        table = html.fromstring(weekly.decode("gb18030")).xpath("//table")[3]
        appendix = html.tostring(table, encoding="unicode")
        appendix = appendix.replace("2026-11-24 22:00", "2026-11-24 21:30") if readable else ""
        http.get(
            link,
            body=(
                f"<html><h1>{title}</h1><time>2026-11-{publication_day} 12:00:00</time>"
                '<div id="artibody">'
                "<p>足球彩票胜负游戏（14场和任选9场）第26169期</p>"
                f"{appendix}<p>国家体育总局体育彩票管理中心 2026年11月{publication_day}日</p>"
                "</div></html>"
            ).encode(),
        )
        index = index.replace("</body>", f'<a href="{link}">{title}</a></body>')
    http.get("https://www.wztc.cn/robots.txt", status=404)
    http.get(INDEX, body=index.encode())
    http.get(WENZHOU, body=weekly)


def _collect_replay(database, *, revision=None, previous_schedule=None):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _mock_replay(http, revision=revision)
        result = sfc.collect_current_sfc(database, previous_schedule=previous_schedule)
        requested = [call.request.url for call in http.calls]
    assert result["status"] == "complete", result["errors"]
    return result, requested


def _freeze_issue(db, archive):
    timestamp = NOW.isoformat()
    batch = dict(
        schema_version=1,
        batch_id="offline-replay",
        mode="prematch",
        probability_type="P2",
        validation_status="experimental_unadopted",
        bet_allowed=False,
        generated_utc=timestamp,
        as_of_utc=timestamp,
        models=[],
        predictions=[],
        input={},
    )
    engine = get_engine(db)
    try:
        with session_scope(engine) as session:
            report = build_sfc_report(
                session,
                session.get(SfcIssue, "26169"),
                batch,
                generated=NOW,
                forecast_sha256=hashlib.sha256(_json_bytes(batch)).hexdigest(),
                allow_missing=True,
            )
    finally:
        engine.dispose()
    directory = "sfc/26169/history/first"
    write_sfc_report(report, archive / directory)
    for filename in ("sfc.json", "manifest.json", "report.md", "report.csv"):
        (archive / f"sfc/26169/{filename}").write_bytes(
            (archive / directory / filename).read_bytes()
        )
    return directory


def _read_check(db, number):
    with sqlite3.connect(db) as connection:
        end, check = connection.execute(
            "SELECT sale_end_utc, official_verification FROM sfc_issues WHERE issue_no=?", (number,)
        ).fetchone()
    return end, json.loads(check)


def test_older_weekly_notice_cannot_revert_adjustment_after_restore(tmp_path, monkeypatch):
    _setup_replay(tmp_path, monkeypatch)
    sequence_inputs = []
    select_issue = sfc.sequence

    def record_selection(windows, at):
        rows = list(windows)
        state = select_issue(rows, at)
        sequence_inputs.append(({r["issue_no"]: r for r in rows}, state))
        return state

    monkeypatch.setattr(sfc, "sequence", record_selection)
    original_db = tmp_path / "data/feed.db"
    archive = tmp_path / "archive"
    restored_db = tmp_path / "restored/feed.db"
    adjusted = "2026-11-24T13:30:00+00:00"
    amendment = "https://www.wztc.cn/system/2026/11/22/105874265.shtml"
    second_at = NOW + timedelta(minutes=10)
    previous = None
    frozen_evidence = {}

    for run, moment, revision, database in (
        ("first", NOW, ("22", "105874265", True), original_db),
        ("second", second_at, None, restored_db),
    ):
        monkeypatch.setattr(ReplayClock, "instant", moment)
        result, requested = _collect_replay(database, revision=revision, previous_schedule=previous)
        assert (amendment in requested) == (run == "first")
        assert WENZHOU in requested
        windows, state = sequence_inputs[-1]
        provider_end, check = _read_check(database, "26169")
        selected = result["official_schedule"]["26169"]
        assert result["due_issue_no"] == state["current"] == "26169"
        assert windows["26169"]["sale_end_utc"] == adjusted
        assert selected["sale_end_utc"] == adjusted and selected["source_url"] == amendment
        assert datetime.fromisoformat(provider_end) == datetime(2026, 11, 24, 14, tzinfo=UTC)
        assert known_official(check)["sale_end_utc"] == adjusted
        assert known_official(check)["source_url"] == amendment
        assert known_official(check)["observed_utc"] == "2026-11-22T16:00:00.000000Z"
        if run == "first":
            assert check["status"] == "conflict" and check["official"]["kind"] == "amendment"
        else:
            assert check["status"] == "pending" and not check["official"]
            assert (
                check["last_known_official"]["raw_sha256"]
                == frozen_evidence["official"]["raw_sha256"]
            )

        export_feeds(database, archive, BEIJING_DAY, run, collection={"sfc": result})
        assert _state(archive, "26169")["official_verification"] == check
        assert (
            json.loads((archive / "feed_manifest.json").read_bytes())["collection"]["sfc"][
                "official_schedule"
            ]["26169"]
            == selected
        )
        if run == "first":
            frozen_dir = _freeze_issue(database, archive)
            prepare_remote(archive, NOW.isoformat())
            frozen = {p: p.read_bytes() for p in (archive / frozen_dir).iterdir()}
            frozen_evidence = read_report(archive, frozen_dir)["source"]["official_verification"]
            assert frozen_evidence == check
            previous_manifest = restore_feeds(archive, restored_db)
            previous = previous_manifest["collection"]["sfc"]["official_schedule"]
        digest = generate(archive, tmp_path / "digest", now=moment, run_id=run)
        rendered = digest.read_text(encoding="utf-8")
        headline = "官方公告停售" if run == "first" else "上次官方公告停售"
        assert f"期号：26169；{headline}：2026-11-24 21:30:00" in rendered
        label = "冲突" if run == "first" else "待核"
        assert f"官方核对：{label}" in rendered and amendment in rendered.replace("\\.", ".")
        assert "第三方所报截止：2026-11-24 22:00:00" in rendered
        assert "报告冻结时的官方核对：冲突" in rendered
        print(
            f"{run} {moment.isoformat()} collection={result['status']} current={state['current']} "
            f"sequence={windows['26169']['sale_end_utc']} "
            f"db={known_official(check)['sale_end_utc']} archive={selected['sale_end_utc']}"
        )
        for line in rendered.splitlines():
            if line.startswith(("期号：", "官方核对：", "公告来源：", "第三方所报截止：")):
                print(line)

    assert {p: p.read_bytes() for p in frozen} == frozen
    assert read_report(archive, frozen_dir)["source"]["official_verification"] == frozen_evidence
    boundary = generate(
        archive,
        tmp_path / "later",
        now="2026-11-24T13:45:00Z",
        run_id="after-adjusted-deadline",
    ).read_text(encoding="utf-8")
    assert "期号：26170" in boundary and "期号：26169" not in boundary
    print("历史冻结报告原件逐字节一致；11月24日 21:45 日报展示 26170")


def test_newer_unreadable_amendment_keeps_its_pending_status(tmp_path, monkeypatch):
    _setup_replay(tmp_path, monkeypatch)
    db = tmp_path / "feed.db"
    first, _ = _collect_replay(db, revision=("22", "105874265", True))
    assert first["official_schedule"]["26169"]["sale_end_utc"] == "2026-11-24T13:30:00+00:00"
    later = NOW + timedelta(hours=13)
    monkeypatch.setattr(ReplayClock, "instant", later)
    second, _ = _collect_replay(
        db, revision=("23", "105874266", False), previous_schedule=first["official_schedule"]
    )
    selected = second["official_schedule"]["26169"]
    assert "sale_end_utc" not in selected and selected["notice_date"] == "2026-11-23"
    _, check = _read_check(db, "26169")
    assert check["status"] == "pending" and "last_known_official" not in check
    assert check["official"]["source_url"].endswith("/105874266.shtml")
    archive = tmp_path / "archive"
    export_feeds(db, archive, BEIJING_DAY, "unreadable", collection={"sfc": second})
    rendered = generate(archive, tmp_path / "digest", now=later).read_text(encoding="utf-8")
    assert "官方核对：待核。官方售期不完整" in rendered
    assert "期号：26169；第三方所报截止：2026-11-24 22:00:00" in rendered
    assert "105874266" in rendered


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
