"""Okooo team evidence is separate from 500.com and bound to the capture cutoff."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.okooo_probe import inspect_page
from zucai.sfc_verification import compare_issue
from zucai.sources.c500 import SfcFixture, SfcSnapshot
from zucai.sources.sfc import ingest_issue, refresh_snapshot

OBSERVED = datetime.fromisoformat("2026-11-23T16:42:00+00:00")
URL = "https://www.okooo.com/zucai/26169/"
APPROVAL = {
    "canonical": "加拉塔萨雷",
    "official": "加拉塔萨雷",
    "status": "approved",
    "approved_by": "qw5563680",
    "approved_utc": "2026-11-23T16:50:00Z",
    "approval_pr": "https://github.com/qw5563680/FOOTBALL/pull/1",
    "evidence": "https://example.com/fixture",
}


def _fixture():
    path = Path(__file__).parents[1] / "data/fixtures/okooo/26169.html"
    page = inspect_page(path.read_bytes(), "26169", "sfc", OBSERVED.isoformat())
    assert page["read_status"] == "complete"
    return SfcSnapshot(
        "26169",
        datetime.fromisoformat(page["sale_end_utc"]),
        [
            SfcFixture(
                row["seq"],
                row["okooo_fixture_id"],
                row["league"],
                row["home"],
                row["away"],
                datetime.fromisoformat(row["kickoff_beijing"]),
                False,
            )
            for row in page["rows"]
        ],
        "explicit_provider",
    )


def _match(session, fixture, match_id, home, away, *, aliases=(), counterpart=None):
    teams = []
    for name in (home, away):
        team = session.scalar(select(Team).where(Team.name == name))
        if team is None:
            team = Team(name=name, aliases=list(aliases) if name == home else [])
            session.add(team)
            session.flush()
        teams.append(team)
    session.add(
        Match(
            match_id=match_id,
            source=match_id.partition(":")[0],
            league=fixture.league,
            home_team_id=teams[0].id,
            away_team_id=teams[1].id,
            kickoff_utc=fixture.kickoff_utc,
            status="scheduled",
            counterpart_id=counterpart,
        )
    )


def test_unapproved_label_and_unrelated_500_id_cannot_claim_the_fixture(tmp_path):
    issue = _fixture()
    second, third, fourth = issue.fixtures[1:4]
    engine = get_engine(tmp_path / "fixture.db")
    init_db(engine)
    with session_scope(engine) as session:
        _match(session, second, "jc:2", "加拉塔萨雷", "维拉", aliases=("加拉塔",))
        _match(session, third, "fdorg:3", third.home, third.away)
        _match(
            session,
            fourth,
            "jc:4",
            "陌生主队",
            "陌生客队",
            counterpart="c500:" + fourth.fixture_id,
        )
        record = ingest_issue(
            session, issue, observed=OBSERVED, url=URL, raw_sha256="a" * 64, mapped_only=True
        )
        rows = refresh_snapshot(session, record)
        assert len(rows) == 14
        assert rows[1]["match_id"] is None and rows[1]["mapping"] == "unmatched"
        assert rows[1]["mapping_evidence"]["provider"] == "okooo.com"
        assert rows[2]["match_id"] == "fdorg:3"
        assert rows[3]["match_id"] is None and rows[3]["mapping"] == "unmatched"
        assert (rows[1]["home"], rows[1]["provider_home_id"]) == ("加拉塔", None)
        assert len(list(session.scalars(select(Match)))) == 3
    engine.dispose()


def test_approved_labels_follow_capture_and_exact_competition(tmp_path, monkeypatch):
    from zucai import sfc_okooo_names

    issue = _fixture()
    second = issue.fixtures[1]
    engine = get_engine(tmp_path / "fixture.db")
    init_db(engine)
    monkeypatch.setattr(sfc_okooo_names, "_table", lambda: {(second.league, second.home): APPROVAL})
    with session_scope(engine) as session:
        _match(session, second, "jc:2", "加拉塔萨雷", second.away)
        _match(session, replace(second, league="英冠"), "fdorg:2", "加拉塔萨雷", second.away)
        record = ingest_issue(
            session, issue, observed=OBSERVED, url=URL, raw_sha256="a" * 64, mapped_only=True
        )
        assert refresh_snapshot(session, record)[1]["match_id"] is None
        record = ingest_issue(
            session,
            issue,
            observed=OBSERVED + timedelta(minutes=10),
            url=URL,
            raw_sha256="b" * 64,
            mapped_only=True,
        )
        row = record.snapshot[1]
        assert row["match_id"] == "jc:2"
        assert row["fixture_id"] == second.fixture_id
        assert row["home"] == second.home
        assert row["mapping_evidence"]["names"]["home"]["approval_pr"] == APPROVAL["approval_pr"]
    engine.dispose()


def test_official_comparison_respects_okooo_cutoff_and_preserves_conflicts(monkeypatch):
    from zucai import sfc_okooo_names, sfc_verification

    issue = _fixture()
    rows = [
        dict(seq=f.seq, home=f.home, away=f.away, league=f.league, fixture_id=f.fixture_id)
        for f in issue.fixtures
    ]
    second = rows[1]
    monkeypatch.setattr(
        sfc_okooo_names, "_table", lambda: {(second["league"], second["home"]): APPROVAL}
    )
    monkeypatch.setattr(
        sfc_verification, "club_name", lambda *_: pytest.fail("Okooo consulted 500 names")
    )
    notice = {"fixtures": deepcopy(rows), "sale_end_utc": issue.sale_end_utc.isoformat()}
    notice["fixtures"][1]["home"] = APPROVAL["official"]
    check = compare_issue(
        issue.issue_no, issue.sale_end_utc, rows, notice, OBSERVED, provider="okooo.com"
    )
    assert check["provider"] == "okooo.com" and check["status"] == "pending"
    checked = OBSERVED + timedelta(minutes=10)
    check = compare_issue(
        issue.issue_no, issue.sale_end_utc, rows, notice, checked, provider="okooo.com"
    )
    assert check["status"] == "verified"
    assert [(a["seq"], a["field"], a["provider"]) for a in check["aliases"]] == [
        (2, "home", second["home"])
    ]
    notice["fixtures"][1]["home"], notice["fixtures"][1]["away"] = (
        notice["fixtures"][1]["away"],
        notice["fixtures"][1]["home"],
    )
    check = compare_issue(
        issue.issue_no, issue.sale_end_utc, rows, notice, checked, provider="okooo.com"
    )
    assert check["status"] == "conflict"
    assert {(d["seq"], d["field"]) for d in check["differences"]} == {(2, "home"), (2, "away")}


def test_repository_empty_and_pending_or_unapproved_rows_cannot_enable_aliases(tmp_path):
    from zucai import sfc_okooo_names

    assert not sfc_okooo_names.load()
    path = tmp_path / "names.json"
    document = {"version": 1, "leagues": {"欧冠": {"加拉塔": {"status": "proposed"}}}}
    path.write_text(json.dumps(document), encoding="utf-8")
    assert not sfc_okooo_names.load(path)
    for entry in (
        {k: v for k, v in APPROVAL.items() if k != "evidence"},
        dict(APPROVAL, approved_by="someone-else"),
        dict(APPROVAL, approved_utc="2026-11-23T16:50:00"),
    ):
        document["leagues"]["欧冠"]["加拉塔"] = entry
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ValueError):
            sfc_okooo_names.load(path)
