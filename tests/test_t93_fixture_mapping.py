from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db
from zucai.models import Match, Team
from zucai.report.sfc import _unmapped_reason
from zucai.sources.c500 import SfcFixture, SfcSnapshot
from zucai.sources.sfc import _snapshot_rows, refresh_snapshot

LEAGUE = "欧罗巴"
KICKOFF = datetime(2026, 11, 26, 20, tzinfo=UTC)


@pytest.fixture
def corpus(tmp_path):
    engine = get_engine(tmp_path / "t93-fixtures.db")
    init_db(engine)
    try:
        with Session(engine) as session:
            teams = {name: Team(name=name) for name in ("测试甲", "测试乙", "测试丙")}
            session.add_all(teams.values())
            session.flush()
            _add_match(
                session,
                teams,
                "apif:earlier",
                kickoff=KICKOFF - timedelta(days=30),
                status="finished",
            )
            _add_match(
                session,
                teams,
                "fd:other-league",
                away="测试丙",
                league="英超",
                kickoff=KICKOFF - timedelta(days=20),
                status="finished",
            )
            yield session, teams
    finally:
        engine.dispose()


def _add_match(
    session,
    teams,
    key,
    *,
    home="测试甲",
    away="测试乙",
    league=LEAGUE,
    kickoff=KICKOFF,
    status="scheduled",
):
    session.add(
        Match(
            match_id=key,
            source=key.split(":")[0],
            league=league,
            home_team_id=teams[home].id,
            away_team_id=teams[away].id,
            kickoff_utc=kickoff,
            status=status,
        )
    )
    session.flush()


def _source_fixture(away="测试乙"):
    fixture = SfcFixture(
        seq=5,
        fixture_id="100007",
        home="测试甲",
        away=away,
        league=LEAGUE,
        kickoff_utc=KICKOFF,
        ended=False,
        home_id="1",
        away_id="2",
    )
    return SfcSnapshot("26999", KICKOFF - timedelta(hours=1), [fixture], "sale_end_year")


@pytest.mark.parametrize(
    "case,chosen",
    [
        ("exact", "jc:100"),
        ("clock", "apif:100"),
        ("sides", "apif:100"),
        ("league", "apif:100"),
        ("ambiguous", "apif:100"),
        ("archived_identity_conflict", "apif:100"),
    ],
)
def test_late_jc_row_replaces_only_a_unique_exact_counterpart(corpus, case, chosen):
    session, teams = corpus
    _add_match(session, teams, "apif:100")
    issue = _source_fixture()
    snapshot = _snapshot_rows(session, issue, {}, mapped_only=True, own_fixtures=True)
    assert snapshot[0]["match_id"] == "apif:100"
    if case == "archived_identity_conflict":
        snapshot[0]["matched_identity"]["home"] = "另一场"
    frozen = deepcopy(snapshot)
    _add_match(
        session,
        teams,
        "jc:100",
        kickoff=KICKOFF + timedelta(minutes=1)
        if case == "clock"
        else KICKOFF.astimezone(timezone(timedelta(hours=8))),
        home="测试乙" if case == "sides" else "测试甲",
        away="测试甲" if case == "sides" else "测试乙",
        league="欧冠" if case == "league" else LEAGUE,
    )
    if case == "ambiguous":
        _add_match(session, teams, "jc:101")
    state = SimpleNamespace(issue_no=issue.issue_no, date_basis=issue.date_basis, snapshot=snapshot)
    before = session.execute(select(Match.match_id, Match.counterpart_id)).all()
    rows = refresh_snapshot(session, state)
    assert rows[0]["match_id"] == chosen
    assert rows[0]["matched_identity"]["source"] == chosen.split(":")[0]
    if case == "exact":
        assert rows[0]["mapping_evidence"]["jc_preferred_over"] == "apif:100"
    assert refresh_snapshot(session, state) == rows
    assert state.snapshot == frozen
    assert session.execute(select(Match.match_id, Match.counterpart_id)).all() == before


@pytest.mark.parametrize(
    "away,reason",
    [
        ("测试乙", None),
        ("测试丙", "客队在库内没有该赛事比赛记录"),
        ("未识别", "客队未对应到库内已有球队"),
    ],
)
def test_cup_own_fixture_needs_two_known_teams_in_its_league(corpus, away, reason):
    session, _ = corpus
    before_teams = session.execute(select(Team.id, Team.name)).all()
    before_matches = set(session.scalars(select(Match.match_id)))
    issue = _source_fixture(away)
    [row] = _snapshot_rows(session, issue, {}, mapped_only=True, own_fixtures=True)
    assert session.execute(select(Team.id, Team.name)).all() == before_teams
    after_matches = set(session.scalars(select(Match.match_id)))
    if reason is None:
        assert row["mapping"] == "matched" and row["match_id"] == "c500:100007"
        assert row["matched_identity"]["league"] == LEAGUE
        assert row["mapping_evidence"]["basis"] == "sfc_own_fixture"
        assert after_matches - before_matches == {"c500:100007"}
    else:
        assert row["mapping"] == "unmatched" and row["match_id"] is None
        assert reason in _unmapped_reason(row, row["reason"])
        state = SimpleNamespace(
            issue_no=issue.issue_no, date_basis=issue.date_basis, snapshot=[row]
        )
        [refreshed] = refresh_snapshot(session, state)
        assert reason in _unmapped_reason(refreshed, refreshed["reason"])
        assert after_matches == before_matches
