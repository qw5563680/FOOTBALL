import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from sqlalchemy import func, select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t07"
STAMP = datetime(2026, 11, 1, 15, tzinfo=UTC)


def parse(issue="26156"):
    from zucai.sources.c500 import C500Sfc

    return C500Sfc().parse((FIXTURES / f"{issue}.html").read_bytes())[0]


def test_real_issue_and_historical_missing_deadline():
    current, history = parse(), parse("26155")
    assert current.issue_no == "26156"
    assert current.sale_end_utc == datetime(2026, 11, 2, 14, tzinfo=UTC)
    assert len(current.fixtures) == 14
    first = current.fixtures[0]
    assert (first.seq, first.fixture_id, first.home, first.away, first.league) == (
        1,
        "1420554",
        "纽卡斯",
        "埃弗顿",
        "英超",
    )
    assert first.kickoff_utc == datetime(2026, 11, 2, 20, tzinfo=UTC)
    assert history.sale_end_utc is None
    assert history.fixtures[0].home == "利物浦"
    assert history.fixtures[0].kickoff_utc == datetime(2026, 11, 1, 16, 30, tzinfo=UTC)


@pytest.mark.parametrize("change", ["missing", "duplicate", "id", "date", "wrong_page"])
def test_parser_rejects_corrupt_issue(change):
    from lxml import html

    from zucai.sources.c500 import C500Sfc

    doc = html.fromstring((FIXTURES / "26156.html").read_bytes().decode("gb18030"))
    rows = doc.xpath("//tr[@data-cid]")
    if change == "missing":
        rows[-1].getparent().remove(rows[-1])
    if change == "duplicate":
        rows[-1].set("data-cid", "1")
    if change == "id":
        for a in rows[-1].xpath('.//a[contains(@href,"fenxi")]'):
            a.set("href", "invalid")
    if change == "date":
        rows[0].xpath('.//td[contains(@class,"td-endtime")]')[0].text = "待定"
    if change == "wrong_page":
        doc = html.fromstring("<html>access denied</html>")
    with pytest.raises(ValueError):
        C500Sfc().parse(html.tostring(doc, encoding="gb18030"))


def test_cross_year_uses_full_deadline_and_historical_ambiguity_fails():
    from zucai.sources.c500 import C500Sfc

    raw = (FIXTURES / "26156.html").read_bytes()
    new_year = raw.replace(b"2026-11-02 22:00:00", b"2026-12-31 22:00:00").replace(
        b"11-03", b"01-01"
    )
    issue = C500Sfc().parse(new_year)[0]
    assert issue.fixtures[0].kickoff_utc == datetime(2026, 12, 31, 20, tzinfo=UTC)
    historical = (
        (FIXTURES / "26155.html")
        .read_bytes()
        .replace(b"11-01", b"12-31")
        .replace(b"11-02", b"01-01")
    )
    with pytest.raises(ValueError):
        C500Sfc().parse(historical)


def test_requested_issue_mismatch_does_not_import(tmp_path, monkeypatch):
    from zucai.sources.sfc import fetch_sfc

    monkeypatch.chdir(tmp_path)
    with responses.RequestsMock() as http:
        http.get("https://trade.500.com/sfc/", body=(FIXTURES / "26156.html").read_bytes())
        with pytest.raises(ValueError, match="期号"):
            fetch_sfc("26155", db=tmp_path / "db")
    assert not (tmp_path / "db").exists()
    assert list((tmp_path / "data/raw").rglob("*.html"))


@pytest.mark.parametrize("month", [b"01", b"12"])
def test_unanchored_year_boundary_needs_a_full_date(month):
    from zucai.sources.c500 import C500Sfc

    raw = (
        (FIXTURES / "26155.html")
        .read_bytes()
        .replace(b"11-01", month + b"-01")
        .replace(b"11-02", month + b"-02")
    )
    with pytest.raises(ValueError, match="年份"):
        C500Sfc().parse(raw)


def test_cli_fetch_exports_verified_ids_for_t06_and_refuses_overwrite(tmp_path, monkeypatch):
    import csv

    from zucai.cli import main

    monkeypatch.chdir(tmp_path)
    args = [
        "fetch",
        "sfc",
        "--issue",
        "26156",
        "--db",
        str(tmp_path / "db"),
        "--matches-out",
        str(tmp_path / "matches.csv"),
    ]
    with responses.RequestsMock() as http:
        http.get("https://trade.500.com/sfc/", body=(FIXTURES / "26156.html").read_bytes())
        http.get("https://trade.500.com/jczq/", body=b'<table id="betTbThead"></table>')
        assert main(args) == 0
    with (tmp_path / "matches.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 14 and rows[0]["match_id"] == "c500:1420554"
    assert rows[0]["home"] == "纽卡斯" and rows[0]["source"] == "c500"
    before = (tmp_path / "matches.csv").read_bytes()
    assert main(args) == 1
    assert (tmp_path / "matches.csv").read_bytes() == before


def test_mapping_is_exact_idempotent_and_older_snapshot_cannot_overwrite(tmp_path, monkeypatch):
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    issue = parse()
    first = issue.fixtures[0]
    with session_scope(engine) as session:
        h = Team(name="纽卡斯尔", aliases=["纽卡斯"])
        a = Team(name="埃弗顿", aliases=[])
        session.add_all([h, a])
        session.flush()
        session.add(
            Match(
                match_id="jc:one",
                source="jc",
                league="英超",
                home_team_id=h.id,
                away_team_id=a.id,
                kickoff_utc=first.kickoff_utc,
                status="scheduled",
            )
        )
    with session_scope(engine) as session:
        record = ingest_issue(
            session, issue, observed=STAMP, url="https://trade.500.com/sfc/", raw_sha256="a" * 64
        )
        assert record.snapshot[0]["match_id"] == "jc:one"
        assert record.snapshot[0]["mapping"] == "matched"
        assert record.snapshot[1]["mapping"] == "unmatched"
    with session_scope(engine) as session:
        before = session.scalar(select(func.count()).select_from(Match))
        ingest_issue(
            session, issue, observed=STAMP + timedelta(seconds=1), url="url", raw_sha256="b" * 64
        )
        assert session.scalar(select(func.count()).select_from(Match)) == before == 14
        assert session.scalar(select(func.count()).select_from(SfcIssueMatch)) == 14
        assert session.get(Match, "jc:one").source == "jc"
    with pytest.raises(ValueError, match="旧"), session_scope(engine) as session:
        ingest_issue(session, issue, observed=STAMP, url="url", raw_sha256="c" * 64)
    with session_scope(engine) as session:
        assert session.get(SfcIssue, "26156").raw_sha256 == "b" * 64
    engine.dispose()


def test_ambiguous_candidates_are_not_silently_chosen(tmp_path, monkeypatch):
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    issue = parse()
    f = issue.fixtures[0]
    with session_scope(engine) as s:
        h = Team(name=f.home)
        a = Team(name=f.away)
        s.add_all([h, a])
        s.flush()
        for k in ["one", "two"]:
            s.add(
                Match(
                    match_id="jc:" + k,
                    source="jc",
                    league=f.league,
                    home_team_id=h.id,
                    away_team_id=a.id,
                    kickoff_utc=f.kickoff_utc,
                    status="scheduled",
                )
            )
    with session_scope(engine) as s:
        result = ingest_issue(s, issue, observed=STAMP, url="url", raw_sha256="a" * 64)
        assert result.snapshot[0]["match_id"] == "c500:1420554"
        assert result.snapshot[0]["mapping"] == "ambiguous"
    changed = replace(issue, fixtures=[replace(f, home=f.away, away=f.home), *issue.fixtures[1:]])
    with pytest.raises(ValueError, match="身份"), session_scope(engine) as s:
        ingest_issue(
            s, changed, observed=STAMP + timedelta(seconds=2), url="url", raw_sha256="b" * 64
        )
    engine.dispose()


def test_legacy_deadline_migration_preserves_rows_and_foreign_keys(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE sfc_issues(issue_no VARCHAR PRIMARY KEY, "
            "sale_end_utc VARCHAR(27) NOT NULL)"
        )
        c.execute("INSERT INTO sfc_issues VALUES ('26154', '2026-10-31T14:00:00.000000Z')")
    engine = get_engine(db)
    init_db(engine)
    init_db(engine)
    with session_scope(engine) as s:
        assert s.get(SfcIssue, "26154").sale_end_utc == "2026-10-31T14:00:00.000000Z"
        s.add(SfcIssue(issue_no="26155", sale_end_utc=None))
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_real_numeric_bridge_maps_abbreviated_teams_without_new_aliases(tmp_path, monkeypatch):
    from zucai.sources.c500 import C500FixtureBridge
    from zucai.sources.sfc import ingest_issue
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    with session_scope(engine) as s:
        games = SportteryCalculator().parse(
            (FIXTURES.parent / "sporttery_calculator_20261101.json").read_bytes()
        )
        save_schedule(s, games, STAMP)
        before = s.scalar(select(func.count()).select_from(Team))
        aliases = {t.id: t.aliases[:] for t in s.scalars(select(Team))}
    bridges = C500FixtureBridge().parse((FIXTURES / "jc-bridge.html").read_bytes())
    with session_scope(engine) as s:
        report = ingest_issue(
            s, parse(), observed=STAMP, url="url", raw_sha256="a" * 64, bridges=bridges
        )
        row = report.snapshot[12]
        assert row["match_id"] == "jc:2042438" and row["mapping"] == "matched"
        assert row["mapping_evidence"]["basis"] == "provider_fixture_and_team_ids"
        assert row["matched_identity"]["away"] == "赫尔火花"
        assert s.scalar(select(Team).where(Team.name == "国际图")) is None
        assert s.scalar(select(Team).where(Team.name == "赫火花")) is None
        for t in s.scalars(select(Team)):
            if t.id in aliases:
                assert t.aliases == aliases[t.id]
        assert s.scalar(select(func.count()).select_from(Team)) < before + 28
    with session_scope(engine) as s:
        old = s.get(SfcIssue, "26156").raw_sha256
    broken = [
        dict(b, home_id=b["away_id"], away_id=b["home_id"]) if b["fixture_id"] == "1489072" else b
        for b in bridges
    ]
    with pytest.raises(ValueError, match="ID"), session_scope(engine) as s:
        ingest_issue(
            s,
            parse(),
            observed=STAMP + timedelta(seconds=1),
            url="url",
            raw_sha256="b" * 64,
            bridges=broken,
        )
    with session_scope(engine) as s:
        assert s.get(SfcIssue, "26156").raw_sha256 == old
    engine.dispose()


def test_verified_counterpart_id_precedes_truncated_team_names(tmp_path, monkeypatch):
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    issue = parse()
    f = issue.fixtures[0]
    with session_scope(engine) as s:
        h = Team(name="纽卡斯尔")
        a = Team(name="埃弗顿")
        s.add_all([h, a])
        s.flush()
        s.add(
            Match(
                match_id="jc:known",
                source="jc",
                league=f.league,
                home_team_id=h.id,
                away_team_id=a.id,
                kickoff_utc=f.kickoff_utc,
                status="scheduled",
                counterpart_id="c500:" + f.fixture_id,
            )
        )
    with session_scope(engine) as s:
        result = ingest_issue(s, issue, observed=STAMP, url="url", raw_sha256="a" * 64)
        assert result.snapshot[0]["match_id"] == "jc:known"
        assert s.scalar(select(Team).where(Team.name == "纽卡斯")) is None
    engine.dispose()


def test_repeat_issue_keeps_archived_bridge_after_current_page_rolls_off(tmp_path, monkeypatch):
    from zucai.sources.c500 import C500FixtureBridge
    from zucai.sources.sfc import ingest_issue
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    bridges = C500FixtureBridge().parse((FIXTURES / "jc-bridge.html").read_bytes())
    for bridge in bridges:
        bridge["provenance"] = {
            "url": "https://trade.500.com/jczq/",
            "observed_utc": STAMP.isoformat(),
            "raw_sha256": "c" * 64,
        }
    with session_scope(engine) as s:
        save_schedule(
            s,
            SportteryCalculator().parse(
                (FIXTURES.parent / "sporttery_calculator_20261101.json").read_bytes()
            ),
            STAMP,
        )
        first = ingest_issue(
            s, parse(), observed=STAMP, url="url", raw_sha256="a" * 64, bridges=bridges
        )
        ids = [r["match_id"] for r in first.snapshot]
        team_count = s.scalar(select(func.count()).select_from(Team))
        match_count = s.scalar(select(func.count()).select_from(Match))
    with session_scope(engine) as s:
        second = ingest_issue(
            s,
            parse(),
            observed=STAMP + timedelta(days=1),
            url="url",
            raw_sha256="b" * 64,
            bridges=[],
        )
        assert [r["match_id"] for r in second.snapshot] == ids
        assert s.scalar(select(func.count()).select_from(Team)) == team_count
        assert s.scalar(select(func.count()).select_from(Match)) == match_count
        evidence = second.snapshot[12]["mapping_evidence"]
        assert evidence["provenance"]["observed_utc"] == STAMP.isoformat()
        assert evidence["reused_from_issue_snapshot"] is True
    engine.dispose()


@pytest.mark.parametrize("other_full_name_match", [False, True])
def test_bridge_arriving_later_keeps_existing_c500_identity(
    tmp_path, monkeypatch, other_full_name_match
):
    from zucai.sources.c500 import C500FixtureBridge
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    issue = parse()
    with session_scope(engine) as s:
        first = ingest_issue(s, issue, observed=STAMP, url="url", raw_sha256="a" * 64)
        ids = [r["match_id"] for r in first.snapshot]
        own = s.get(Match, ids[0])
        team_ids = own.home_team_id, own.away_team_id
        if other_full_name_match:
            full = Team(name="纽卡斯尔")
            s.add(full)
            s.flush()
            # A name/time match cannot override the explicit bridge's other JC ID.
            s.add(
                Match(
                    match_id="jc:other",
                    source="jc",
                    home_team_id=full.id,
                    away_team_id=own.away_team_id,
                    league=own.league,
                    kickoff_utc=own.kickoff_utc,
                    status="scheduled",
                )
            )
        before = s.scalar(select(func.count()).select_from(Team))
    bridges = C500FixtureBridge().parse((FIXTURES / "jc-bridge.html").read_bytes())
    with session_scope(engine) as s:
        second = ingest_issue(
            s,
            issue,
            observed=STAMP + timedelta(seconds=1),
            url="url",
            raw_sha256="b" * 64,
            bridges=bridges,
        )
        assert [r["match_id"] for r in second.snapshot] == ids
        own = s.get(Match, ids[0])
        assert own.counterpart_id == "jc:2042443"
        assert (own.home_team_id, own.away_team_id) == team_ids
        assert second.snapshot[0]["mapping"] == "unmatched"
        assert s.scalar(select(func.count()).select_from(Team)) == before
    engine.dispose()


def test_verified_swedish_league_alias_does_not_accept_other_competitions(tmp_path, monkeypatch):
    from zucai.sources.c500 import C500FixtureBridge
    from zucai.sources.sfc import ingest_issue

    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    issue = parse()
    fixture = issue.fixtures[10]
    bridges = C500FixtureBridge().parse((FIXTURES / "jc-bridge.html").read_bytes())
    with session_scope(engine) as s:
        h, a = Team(name="盖斯"), Team(name="索尔纳")
        s.add_all([h, a])
        s.flush()
        s.add(
            Match(
                match_id="jc:2042440",
                source="jc",
                league="瑞超",
                home_team_id=h.id,
                away_team_id=a.id,
                kickoff_utc=fixture.kickoff_utc,
                status="scheduled",
            )
        )
    with session_scope(engine) as s:
        result = ingest_issue(
            s, issue, observed=STAMP, url="url", raw_sha256="a" * 64, bridges=bridges
        )
        assert result.snapshot[10]["match_id"] == "jc:2042440"
        assert result.snapshot[10]["league"] == "瑞典超"
        assert result.snapshot[10]["matched_identity"]["league"] == "瑞超"
    changed = replace(
        issue, fixtures=[replace(f, league="瑞典杯") if f.seq == 11 else f for f in issue.fixtures]
    )
    with pytest.raises(ValueError, match="联赛"), session_scope(engine) as s:
        ingest_issue(
            s,
            changed,
            observed=STAMP + timedelta(seconds=1),
            url="url",
            raw_sha256="b" * 64,
            bridges=bridges,
        )
    engine.dispose()
