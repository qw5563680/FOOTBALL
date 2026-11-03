"""Offline cloud SFC collection and portable state checks."""

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses
from sqlalchemy import func, select

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team
from zucai.sources.c500 import URL, C500FixtureBridge, C500Sfc
from zucai.sources.sfc import collect_current_sfc, ingest_issue

RAW = (Path(__file__).parents[1] / "data/fixtures/t07/26156.html").read_bytes()
BRIDGE_RAW = (Path(__file__).parents[1] / "data/fixtures/t26/full-bridge.html").read_bytes()


def database(tmp_path):
    path = tmp_path / "data/feed.db"
    path.parent.mkdir()
    engine = get_engine(path)
    init_db(engine)
    return path, engine


def test_legacy_issue_without_snapshot_is_preserved_but_not_exported(tmp_path, caplog):
    path, engine = database(tmp_path)
    number = C500Sfc().parse(RAW)[0].issue_no
    with session_scope(engine) as session:
        session.add(SfcIssue(issue_no=number, sale_end_utc=None))
    engine.dispose()
    before = path.read_bytes()
    meta = export_feeds(path, tmp_path / "archive", date(2026, 11, 1), "legacy")
    assert path.read_bytes() == before
    assert not any(key.startswith("state/sfc/") for key in meta["files"])
    assert number in caplog.text and "missing frozen snapshot" in caplog.text


def test_unresolved_roundtrip_and_corrupt_state_preserves_destination(tmp_path):
    path, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    with session_scope(engine) as session:
        for _ in range(2):
            ingest_issue(
                session,
                issue,
                observed=datetime(2026, 11, 1, tzinfo=UTC),
                url=C500Sfc.url,
                raw_sha256="a" * 64,
                mapped_only=True,
            )
        assert session.scalar(select(func.count()).select_from(Team)) == 0
        assert session.scalar(select(func.count()).select_from(Match)) == 0
        assert session.scalar(select(func.count()).select_from(SfcIssueMatch)) == 0
    engine.dispose()
    archive = tmp_path / "archive"
    meta = export_feeds(path, archive, date(2026, 11, 1), "test")
    assert "state/sfc/26156.json" in meta["files"]
    dest = tmp_path / "restored/feed.db"
    restore_feeds(archive, dest)
    restored = get_engine(dest)
    with session_scope(restored) as session:
        assert len(session.get(SfcIssue, "26156").snapshot) == 14
        assert all(r["match_id"] is None for r in session.get(SfcIssue, "26156").snapshot)
    restored.dispose()
    previous = dest.read_bytes()
    state = archive / "state/sfc/26156.json"
    payload = json.loads(state.read_text())
    payload["issue"]["snapshot"][0]["seq"] = 2
    state.write_text(json.dumps(payload))
    from zucai.cloud_feeds import digest

    meta["files"]["state/sfc/26156.json"] = digest(state)
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        restore_feeds(archive, dest)
    assert dest.read_bytes() == previous


@responses.activate
def test_current_two_requests_and_audit_dedup(tmp_path, monkeypatch):
    path, engine = database(tmp_path)
    engine.dispose()
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, C500Sfc.url, body=RAW, status=200)
    responses.add(responses.GET, URL, body=BRIDGE_RAW, status=200)
    assert collect_current_sfc(path)["status"] == "complete"
    responses.add(responses.GET, C500Sfc.url, body=RAW + b" ", status=200)
    assert collect_current_sfc(path)["status"] == "complete"
    assert [c.request.url for c in responses.calls] == [C500Sfc.url, URL] * 2
    audit = path.parent / "state/audit/sfc_unmatched.csv"
    assert len(audit.read_text().splitlines()) == 15
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM teams").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM matches").fetchone()[0] == 0


@pytest.mark.parametrize("canonical_label", [False, True])
@responses.activate
def test_bridge_maps_all_fourteen_without_changing_identities(
    tmp_path, monkeypatch, canonical_label
):
    path, engine = database(tmp_path)
    bridges = C500FixtureBridge().parse(BRIDGE_RAW)
    with session_scope(engine) as session:
        for bridge in bridges:
            home, away = Team(name=bridge["home"]), Team(name=bridge["away"])
            session.add_all([home, away])
            session.flush()
            session.add(
                Match(
                    match_id=bridge["jc_id"],
                    source="jc",
                    league="英格兰超级联赛"
                    if canonical_label and bridge == bridges[0]
                    else bridge["league"],
                    home_team_id=home.id,
                    away_team_id=away.id,
                    kickoff_utc=bridge["kickoff_utc"],
                    status="scheduled",
                )
            )
    engine.dispose()
    with sqlite3.connect(path) as db:
        before = [
            db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("teams", "matches")
        ]
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, C500Sfc.url, body=RAW)
    responses.add(responses.GET, URL, body=BRIDGE_RAW)
    result = collect_current_sfc(path)
    assert result["status"] == "complete" and result["mapped"] == 14
    assert result["unmatched"] == 0 and result["requests"] == 2
    with sqlite3.connect(path) as db:
        assert [
            db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("teams", "matches")
        ] == before
        rows = json.loads(db.execute("SELECT snapshot FROM sfc_issues").fetchone()[0])
        assert all(r["mapping"] == "matched" for r in rows)
        assert all(r["mapping_evidence"]["provenance"]["url"] == URL for r in rows)
        assert all(r["mapping_evidence"]["provenance"]["raw_sha256"] for r in rows)
        assert db.execute("SELECT count(*) FROM sfc_issue_matches").fetchone()[0] == 14


@pytest.mark.parametrize("status", [302, 503])
@responses.activate
def test_bridge_failure_never_redirects_or_publishes_issue(tmp_path, monkeypatch, status):
    path, engine = database(tmp_path)
    engine.dispose()
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, C500Sfc.url, body=RAW)
    responses.add(responses.GET, URL, status=status, headers={"Location": URL + "?date=2026-11-01"})
    result = collect_current_sfc(path)
    assert result["status"] == "partial" and result["requests"] == 2
    assert [c.request.url for c in responses.calls] == [C500Sfc.url, URL]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM sfc_issues").fetchone()[0] == 0


@pytest.mark.parametrize("counterpart", [True, False])
@pytest.mark.parametrize("bridge_present", [True, False])
def test_linked_candidates_require_counterpart_or_same_identity(
    tmp_path, counterpart, bridge_present
):
    path, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    fixture = issue.fixtures[0]
    bridges = C500FixtureBridge().parse(BRIDGE_RAW)
    jc_id = bridges[0]["jc_id"]
    own_id = "c500:" + fixture.fixture_id
    with session_scope(engine) as session:
        home, away = Team(name=fixture.home), Team(name=fixture.away)
        other_home, other_away = Team(name="另一主队"), Team(name="另一客队")
        session.add_all([home, away, other_home, other_away])
        session.flush()
        for mid, h, a, link in [
            (own_id, home.id, away.id, "fdorg:same"),
            (jc_id, home.id, away.id, own_id),
            ("fdorg:same", other_home.id, other_away.id, jc_id if counterpart else None),
        ]:
            session.add(
                Match(
                    match_id=mid,
                    source=mid.split(":")[0],
                    league=fixture.league,
                    home_team_id=h,
                    away_team_id=a,
                    kickoff_utc=fixture.kickoff_utc,
                    counterpart_id=link,
                )
            )
        session.flush()
        record = ingest_issue(
            session,
            issue,
            observed=datetime(2026, 11, 1, tzinfo=UTC),
            url=C500Sfc.url,
            raw_sha256="a" * 64,
            bridges=bridges if bridge_present else (),
            mapped_only=True,
        )
        assert record.snapshot[0]["mapping"] == ("matched" if counterpart else "ambiguous")
        assert record.snapshot[0]["match_id"] == (jc_id if counterpart else None)
    engine.dispose()


@pytest.mark.parametrize("linked", [False, True])
def test_duplicate_sources_prefer_jc(tmp_path, linked):
    path, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    fixture = issue.fixtures[0]
    with session_scope(engine) as session:
        home, away = Team(name=fixture.home), Team(name=fixture.away)
        session.add_all([home, away])
        session.flush()
        for source in ("fdorg", "fd", "jc"):
            session.add(
                Match(
                    match_id=source + ":same",
                    source=source,
                    league=fixture.league,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    kickoff_utc=fixture.kickoff_utc,
                    counterpart_id="c500:" + fixture.fixture_id if linked else None,
                )
            )
        session.flush()
        record = ingest_issue(
            session,
            issue,
            observed=datetime(2026, 11, 1, tzinfo=UTC),
            url=C500Sfc.url,
            raw_sha256="a" * 64,
            mapped_only=True,
        )
        assert record.snapshot[0]["mapping"] == "matched"
        assert record.snapshot[0]["match_id"] == "jc:same"
    engine.dispose()


@responses.activate
def test_redirect_is_partial_and_never_followed(tmp_path, monkeypatch):
    path, engine = database(tmp_path)
    engine.dispose()
    monkeypatch.chdir(tmp_path)
    responses.add(
        responses.GET, C500Sfc.url, status=302, headers={"Location": "https://trade.500.com/jczq/"}
    )
    result = collect_current_sfc(path)
    assert result["status"] == "partial"
    assert len(responses.calls) == 1
    assert all(k in result for k in ("inserted", "requests", "errors", "rejected_rows"))


def test_mapped_and_ambiguous_keep_existing_identities(tmp_path):
    path, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    first, second = issue.fixtures[:2]
    with session_scope(engine) as session:
        for fixture, count in ((first, 1), (second, 2)):
            home, away = Team(name=fixture.home), Team(name=fixture.away)
            session.add_all([home, away])
            session.flush()
            for n in range(count):
                session.add(
                    Match(
                        match_id=f"jc:{fixture.seq}-{n}",
                        source="jc",
                        league=fixture.league,
                        home_team_id=home.id,
                        away_team_id=away.id,
                        kickoff_utc=fixture.kickoff_utc,
                        status="scheduled",
                    )
                )
        session.flush()
        record = ingest_issue(
            session,
            issue,
            observed=datetime(2026, 11, 1, tzinfo=UTC),
            url=C500Sfc.url,
            raw_sha256="a" * 64,
            mapped_only=True,
        )
        assert record.snapshot[0]["match_id"] == "jc:1-0"
        assert record.snapshot[1]["mapping"] == "ambiguous"
        assert record.snapshot[1]["match_id"] is None
        assert record.snapshot[1]["candidate_ids"] == ["jc:2-0", "jc:2-1"]
        assert session.scalar(select(func.count()).select_from(Team)) == 4
        assert session.scalar(select(func.count()).select_from(Match)) == 3
        assert session.scalar(select(func.count()).select_from(SfcIssueMatch)) == 1
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(path, archive, date(2026, 11, 1), "mapped")
    restore_feeds(archive, tmp_path / "other/feed.db")


@pytest.mark.parametrize("failure", ["audit_write", "audit_publish", "commit"])
@responses.activate
def test_collection_failure_rolls_back_issue_links_and_audit(tmp_path, monkeypatch, failure):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    path, engine = database(tmp_path)
    fixture = C500Sfc().parse(RAW)[0].fixtures[0]
    with session_scope(engine) as session:
        home, away = Team(name=fixture.home), Team(name=fixture.away)
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id="jc:existing",
                source="jc",
                league=fixture.league,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=fixture.kickoff_utc,
                status="scheduled",
            )
        )
    audit = path.parent / "state/audit/sfc_unmatched.csv"
    audit.parent.mkdir(parents=True)
    previous = (
        b"issue_no,seq,fixture_id,reason,candidates,raw_sha256\n26155,1,old,unmatched,[],first\n"
    )
    audit.write_bytes(previous)
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, C500Sfc.url, body=RAW, status=200)
    responses.add(responses.GET, URL, body=BRIDGE_RAW, status=200)
    original_open, original_replace = Path.open, Path.replace

    def fail_open(self, *args, **kwargs):
        if self == audit.with_suffix(".tmp"):
            raise OSError("injected audit write failure")
        return original_open(self, *args, **kwargs)

    def fail_replace(self, *args, **kwargs):
        if self == audit.with_suffix(".tmp"):
            raise OSError("injected audit publish failure")
        return original_replace(self, *args, **kwargs)

    def fail_commit(session):
        raise RuntimeError("injected commit failure")

    if failure == "audit_write":
        monkeypatch.setattr(Path, "open", fail_open)
    elif failure == "audit_publish":
        monkeypatch.setattr(Path, "replace", fail_replace)
    else:
        event.listen(Session, "before_commit", fail_commit)
    try:
        result = collect_current_sfc(path)
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail_commit)
    assert result["status"] == "partial"
    assert result["linked_existing"] == 0
    assert audit.read_bytes() == previous
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(SfcIssue)) == 0
        assert session.scalar(select(func.count()).select_from(SfcIssueMatch)) == 0
        assert session.scalar(select(func.count()).select_from(Match)) == 1
    engine.dispose()
