"""Manual fetching retains unknown national identities without fabricating CSV IDs."""

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import responses
from lxml import html

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sfc_state import export_sfc_state
from zucai.sources.c500 import C500Sfc
from zucai.sources.sfc import fetch_sfc, ingest_issue
from zucai.sources.sporttery import BEIJING

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
SFC_URL = "https://trade.500.com/sfc/"
BRIDGE_URL = "https://trade.500.com/jczq/"


def _national_page(*, home_id="99999999", home=None, away_id=None, away=None):
    """Use saved T07 HTML with one T58 row; the ID mutation is synthetic."""
    doc = html.fromstring((FIXTURES / "t07/26156.html").read_bytes().decode("gb18030"))
    original = json.loads((FIXTURES / "t58/26164-state.json").read_text())["issue"]["snapshot"][1]
    # Put the rejected row last so rollback must undo earlier manual insertions.
    row = doc.xpath("//tr[@data-cid]")[-1]
    row.xpath('.//td[contains(@class,"td-evt")]/a')[0].text = original["league"]
    row.xpath('.//td[contains(@class,"td-endtime")]')[0].text = (
        datetime.fromisoformat(original["source_kickoff_utc"])
        .astimezone(BEIJING)
        .strftime("%m-%d %H:%M")
    )
    for side, letter, provider_id, name in (
        ("home", "l", home_id, home),
        ("away", "r", away_id, away),
    ):
        link = row.xpath(f'.//a[@class="team-{letter}"]')[0]
        provider_id = provider_id or original["provider_" + side + "_id"]
        link.set("href", f"https://liansai.500.com/team/{provider_id}/")
        link.text = name or original[side]
    row.xpath('.//a[contains(@href,"/fenxi/shuju-")]')[0].set(
        "href", f"https://odds.500.com/fenxi/shuju-{original['fixture_id']}.shtml"
    )
    return html.tostring(doc, encoding="gb18030")


@pytest.mark.parametrize("export_csv", [False, True])
def test_manual_fetch_archives_all_rows_when_one_national_id_is_unknown(
    tmp_path, monkeypatch, caplog, export_csv
):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "manual.db"
    output = tmp_path / "inputs/matches.csv" if export_csv else None
    raw = _national_page()
    with responses.RequestsMock() as http:
        http.get(SFC_URL, body=raw)
        http.get(BRIDGE_URL, body=b'<table id="betTbThead"></table>')
        with caplog.at_level("INFO", logger="zucai.sources.sfc"):
            record = fetch_sfc("26156", db=db, matches_out=output)

    assert len(record.snapshot) == 14
    row = record.snapshot[-1]
    assert row["mapping"] == "unmatched"
    assert row["reason"] == "unmatched"
    assert row["match_id"] is None
    assert row["matched_identity"] is None
    assert row["candidate_ids"] == []
    assert row["mapping_evidence"] == {
        "basis": "national_team_name_missing",
        "home_id": "99999999",
        "away_id": "55",
        "missing_teams": [{"provider_id": "99999999", "label": "格鲁吉"}],
    }
    assert record.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert any(p.read_bytes() == raw for p in (tmp_path / "data/raw").rglob("*.html"))
    archive = tmp_path / "archive"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 13
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 26
        assert connection.execute("SELECT COUNT(*) FROM sfc_issue_matches").fetchone()[0] == 13
        assert (
            connection.execute(
                "SELECT match_id FROM matches WHERE match_id='c500:1398632'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM teams WHERE name IN ('格鲁吉', '格鲁吉亚', '匈牙利')"
            ).fetchall()
            == []
        )
        assert export_sfc_state(connection, archive) == ["state/sfc/26156.json"]
    saved = json.loads((archive / "state/sfc/26156.json").read_text())
    assert saved["issue"]["snapshot"] == record.snapshot
    assert len(saved["matches"]) == 13
    if export_csv:
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 13
        assert set(rows[0]) == {"match_id", "home", "away", "league", "source", "kickoff_utc"}
        assert {r["match_id"] for r in rows} == {
            r["match_id"] for r in record.snapshot if r["match_id"] is not None
        }
        assert all(all(r.values()) for r in rows)
        assert "已导出13场比赛清单；未解析身份1场" in caplog.text


@pytest.mark.parametrize(
    "identity",
    [
        {"home_id": "77", "home": "错误队名"},
        {"away": "错误队名"},
        {"home_id": "77", "home": "错误队名", "away_id": "88888888"},
    ],
    ids=["known-home-conflict", "unknown-home-away-conflict", "home-conflict-unknown-away"],
)
def test_manual_fetch_known_id_name_conflict_rolls_back_without_csv(
    tmp_path, monkeypatch, identity
):
    monkeypatch.chdir(tmp_path)
    db, output = tmp_path / "manual.db", tmp_path / "matches.csv"
    with responses.RequestsMock() as http:
        http.get(SFC_URL, body=_national_page(**identity))
        http.get(BRIDGE_URL, body=b'<table id="betTbThead"></table>')
        with pytest.raises(ValueError, match="球队编号与期次原队名冲突"):
            fetch_sfc("26156", db=db, matches_out=output)
    assert not output.exists()
    with sqlite3.connect(db) as connection:
        for table in ("teams", "matches", "sfc_issues", "sfc_issue_matches"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_manual_fetch_keeps_evidence_for_both_unknown_national_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, output = tmp_path / "manual.db", tmp_path / "matches.csv"
    with responses.RequestsMock() as http:
        http.get(SFC_URL, body=_national_page(away_id="88888888"))
        http.get(BRIDGE_URL, body=b'<table id="betTbThead"></table>')
        record = fetch_sfc("26156", db=db, matches_out=output)
    row = record.snapshot[-1]
    assert row["mapping"] == "unmatched"
    assert row["match_id"] is None
    assert row["matched_identity"] is None
    assert row["mapping_evidence"]["basis"] == "national_team_name_missing"
    assert row["mapping_evidence"]["missing_teams"] == [
        {"provider_id": "99999999", "label": "格鲁吉"},
        {"provider_id": "88888888", "label": "匈牙利"},
    ]
    with output.open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 13
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 13
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 26


@pytest.mark.parametrize("linked", [False, True], ids=["fallback", "counterpart"])
@pytest.mark.parametrize("conflict", [False, True], ids=["both-unknown", "away-conflict"])
def test_cloud_national_identity_checks_keep_conflicts_distinct_from_missing_names(
    tmp_path, linked, conflict
):
    raw = _national_page(away="错误队名") if conflict else _national_page(away_id="88888888")
    issue = C500Sfc().parse(raw)[0]
    engine = get_engine(tmp_path / "cloud.db")
    init_db(engine)
    with session_scope(engine) as session:
        if linked:
            home, away = Team(name="格鲁吉亚"), Team(name="匈牙利")
            session.add_all([home, away])
            session.flush()
            session.add(
                Match(
                    match_id="apif:1528973",
                    source="apif",
                    league="欧国联",
                    home_team_id=home.id,
                    away_team_id=away.id,
                    kickoff_utc=issue.fixtures[-1].kickoff_utc,
                    status="scheduled",
                    counterpart_id="c500:1398632",
                )
            )
            session.flush()
        record = ingest_issue(
            session,
            issue,
            observed=datetime.fromisoformat("2026-11-13T12:00:00+00:00"),
            url=SFC_URL,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            mapped_only=True,
        )
        row = record.snapshot[-1]
        assert row["match_id"] is None
        assert row["matched_identity"] is None
        if conflict:
            assert row["mapping"] == "ambiguous"
            assert row["mapping_evidence"]["basis"] == "conflicting_identity"
            assert "球队编号与期次原队名冲突" in row["mapping_evidence"]["detail"]
        else:
            assert row["mapping"] == "unmatched"
            assert row["mapping_evidence"]["basis"] == "national_team_name_missing"
            assert row["mapping_evidence"]["missing_teams"] == [
                {"provider_id": "99999999", "label": "格鲁吉"},
                {"provider_id": "88888888", "label": "匈牙利"},
            ]
    engine.dispose()


@pytest.mark.parametrize("mapped_only", [False, True], ids=["manual", "cloud"])
@pytest.mark.parametrize("binding", ["own", "counterpart"])
@pytest.mark.parametrize("conflict", [None, "kickoff", "league", "known-home", "known-away"])
def test_unknown_national_identity_does_not_hide_existing_match_conflicts(
    tmp_path, mapped_only, binding, conflict
):
    raw = (
        _national_page(home_id="77", away_id="88888888")
        if conflict == "known-home"
        else _national_page()
    )
    issue = C500Sfc().parse(raw)[0]
    engine = get_engine(tmp_path / "existing.db")
    init_db(engine)
    own = binding == "own"
    with session_scope(engine) as session:
        home, away, other = Team(name="格鲁吉亚"), Team(name="匈牙利"), Team(name="葡萄牙")
        session.add_all([home, away, other])
        session.flush()
        session.add(
            Match(
                match_id="c500:1398632" if own else "apif:1528973",
                source="c500" if own else "apif",
                league="友谊赛" if conflict == "league" else "欧国联",
                home_team_id=other.id if conflict == "known-home" else home.id,
                away_team_id=other.id if conflict == "known-away" else away.id,
                kickoff_utc=issue.fixtures[-1].kickoff_utc
                + timedelta(hours=1 if conflict == "kickoff" else 0),
                status="scheduled",
                counterpart_id=None if own else "c500:1398632",
            )
        )

    def ingest(session):
        return ingest_issue(
            session,
            issue,
            observed=datetime.fromisoformat("2026-11-13T12:00:00+00:00"),
            url=SFC_URL,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            mapped_only=mapped_only,
        )

    if conflict and not mapped_only:
        with pytest.raises(ValueError, match="身份冲突|球队、主客或原始开球时间不一致"):
            with session_scope(engine) as session:
                ingest(session)
    else:
        with session_scope(engine) as session:
            row = ingest(session).snapshot[-1]
            assert row["match_id"] is None
            assert row["matched_identity"] is None
            assert row["mapping"] == ("ambiguous" if conflict else "unmatched")
            assert row["mapping_evidence"]["basis"] == (
                "conflicting_identity" if conflict else "national_team_name_missing"
            )
    engine.dispose()


def test_existing_manual_national_short_names_keep_their_identity(tmp_path):
    raw = _national_page(home_id="77")
    issue = C500Sfc().parse(raw)[0]
    engine = get_engine(tmp_path / "existing-short-name.db")
    init_db(engine)
    with session_scope(engine) as session:
        home, away = Team(name="格鲁吉"), Team(name="匈牙利")
        session.add_all([home, away])
        session.flush()
        match = Match(
            match_id="c500:1398632",
            source="c500",
            league="欧国联",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=issue.fixtures[-1].kickoff_utc.isoformat(),
            status="scheduled",
        )
        session.add(match)
        session.flush()
        record = ingest_issue(
            session,
            issue,
            observed=datetime.fromisoformat("2026-11-13T12:00:00+00:00"),
            url=SFC_URL,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
        assert record.snapshot[-1]["match_id"] == match.match_id
        assert record.snapshot[-1]["matched_identity"]["home"] == "格鲁吉"
        assert match.home_team_id == home.id
    engine.dispose()


def test_manual_fetch_existing_csv_is_preserved_before_any_fetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, output = tmp_path / "manual.db", tmp_path / "matches.csv"
    output.write_bytes(b"existing frozen matches\n")
    with pytest.raises(ValueError, match="比赛清单已存在"):
        fetch_sfc("26156", db=db, matches_out=output)
    assert output.read_bytes() == b"existing frozen matches\n"
    assert not db.exists()
    assert not (tmp_path / "data/raw").exists()


def test_manual_fetch_csv_write_failure_removes_partial_file_and_rolls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, output = tmp_path / "manual.db", tmp_path / "matches.csv"

    def failed_writerows(self, rows):
        self.writerow(next(iter(rows)))
        raise OSError("simulated output write failure")

    monkeypatch.setattr(csv.DictWriter, "writerows", failed_writerows)
    with responses.RequestsMock() as http:
        http.get(SFC_URL, body=_national_page())
        http.get(BRIDGE_URL, body=b'<table id="betTbThead"></table>')
        with pytest.raises(OSError, match="simulated output write failure"):
            fetch_sfc("26156", db=db, matches_out=output)
    assert not output.exists()
    with sqlite3.connect(db) as connection:
        for table in ("teams", "matches", "sfc_issues", "sfc_issue_matches"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
