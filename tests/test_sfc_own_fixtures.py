"""T78: a 14-match row that no regular source carries stands as its own fixture, so the
league model can forecast it. It names stored teams only and never enters training."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from test_prediction_batch import CUTOFF
from test_prediction_batch import setup_batch as setup_batch
from zucai import sfc_club_names
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.base import SFC_FIXTURE_SOURCE, fixture_sources
from zucai.sources.c500 import SfcFixture, SfcSnapshot
from zucai.sources.sfc import _map_fixture, _snapshot_rows

KICKOFF = datetime(2026, 11, 20, 19, tzinfo=UTC)
LEAGUE = "法乙"
APPROVAL = dict(
    status="approved",
    approved_by="qw5563680",
    approved_utc="2026-11-19T12:00:00Z",
    approval_pr="https://github.com/qw5563680/FOOTBALL/pull/1",
)
TABLE = {(LEAGUE, "6597"): dict(label="圣红星", canonical="圣旺红星", **APPROVAL)}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sfc_club_names, "_table", lambda: TABLE)
    engine = get_engine(tmp_path / "games.db")
    init_db(engine)
    with Session(engine) as session:
        names = ("南锡", "第戎", "圣旺红星", "波城", "别的联赛队")
        teams = {name: Team(name=name, aliases=[]) for name in names}
        session.add_all(teams.values())
        session.flush()
        # Earlier results of this league (as football-data.co.uk supplies them).
        for n, (home, away, league) in enumerate(
            (
                ("南锡", "第戎", LEAGUE),
                ("圣旺红星", "波城", LEAGUE),
                ("别的联赛队", "南锡", "法甲"),
            )
        ):
            session.add(
                Match(
                    match_id=f"fd:history-{n}",
                    source="fd",
                    league=league,
                    home_team_id=teams[home].id,
                    away_team_id=teams[away].id,
                    status="finished",
                    kickoff_utc=KICKOFF - timedelta(days=30 + n),
                )
            )
        session.flush()
        yield session
    engine.dispose()


def _fixture(**changes):
    values = dict(
        seq=7,
        fixture_id="1416964",
        league=LEAGUE,
        home="南锡",
        away="第戎",
        kickoff_utc=KICKOFF,
        ended=False,
        home_id="1130",
        away_id="774",
    )
    values.update(changes)
    return SfcFixture(**values)


def _state(session):
    teams = [(t.id, t.name, list(t.aliases)) for t in session.query(Team).order_by(Team.id)]
    matches = [
        (m.match_id, m.source, m.league, m.home_team_id, m.away_team_id, m.kickoff_utc, m.status)
        for m in session.query(Match).order_by(Match.match_id)
    ]
    return teams, matches


def _collect(session, fixture=None, bridges=None):
    return _map_fixture(
        session, fixture or _fixture(), bridges or {}, mapped_only=True, own_fixtures=True
    )


def test_collection_makes_the_row_its_own_fixture_from_stored_teams_only(session):
    teams_before, matches_before = _state(session)
    match, mapping, candidates, evidence = _collect(session)
    assert (match.match_id, match.source, mapping, candidates) == (
        "c500:1416964",
        "c500",
        "matched",
        [],
    )
    assert evidence == dict(basis="sfc_own_fixture", home_id="1130", away_id="774")
    teams, matches = _state(session)
    # No team is created or renamed and no alias is added: one fixture row is the only write.
    assert teams == teams_before
    by_name = {name: team_id for team_id, name, _ in teams}
    assert [m for m in matches if m not in matches_before] == [
        (
            "c500:1416964",
            "c500",
            LEAGUE,
            by_name["南锡"],
            by_name["第戎"],
            "2026-11-20T19:00:00.000000Z",
            "scheduled",
        )
    ]
    # Collecting the same list again changes nothing more.
    again = _collect(session)
    assert again[0].match_id == "c500:1416964" and _state(session) == (teams, matches)


def test_a_later_reading_uses_the_stored_fixture_but_never_writes_one(session):
    before = _state(session)
    # Without a stored own fixture, a re-reading of the snapshot leaves the row unmatched.
    assert _map_fixture(session, _fixture(), {}, mapped_only=True)[:2] == (None, "unmatched")
    assert _state(session) == before
    _collect(session)
    stored = _state(session)
    match, mapping, _, evidence = _map_fixture(session, _fixture(), {}, mapped_only=True)
    assert (match.match_id, mapping, evidence["basis"]) == (
        "c500:1416964",
        "matched",
        "sfc_own_fixture",
    )
    assert _state(session) == stored


def test_an_approved_short_name_counts_and_is_recorded(session):
    fixture = _fixture(
        seq=10, fixture_id="1416966", home="圣红星", away="波城FC", home_id="6597", away_id="2329"
    )
    match, mapping, _, evidence = _collect(session, fixture)
    assert (match.match_id, mapping) == ("c500:1416966", "matched")
    assert evidence["basis"] == "sfc_own_fixture"
    assert evidence["names"] == dict(
        home=dict(provider_id="6597", label="圣红星", canonical="圣旺红星")
    )
    assert session.get(Team, match.home_team_id).name == "圣旺红星"
    assert session.get(Team, match.away_team_id).name == "波城"


@pytest.mark.parametrize(
    "changes",
    [
        dict(away="不认识"),  # a printed name that is no stored team
        dict(away="别的联赛队"),  # a stored team that has never played in this league
        dict(home="圣红星", home_id="1"),  # short name printed under another source team ID
        dict(league="法甲"),  # the teams are known, but not in the league the row names
        dict(away="南锡"),  # both sides resolve to one identity
    ],
)
def test_no_own_fixture_unless_both_sides_are_stored_teams_of_that_league(session, changes):
    before = _state(session)
    match, mapping, _, _ = _collect(session, _fixture(**changes))
    assert (match, mapping) == (None, "unmatched")
    assert _state(session) == before


def test_a_regular_fixture_always_wins_over_the_list(session):
    home, away = (session.query(Team).filter_by(name=name).one() for name in ("南锡", "第戎"))
    _collect(session)
    session.add(
        Match(
            match_id="apif:900",
            source="apif",
            league=LEAGUE,
            home_team_id=home.id,
            away_team_id=away.id,
            status="scheduled",
            kickoff_utc=KICKOFF,
        )
    )
    session.flush()
    for own_fixtures in (True, False):
        match, mapping, candidates, evidence = _map_fixture(
            session, _fixture(), {}, mapped_only=True, own_fixtures=own_fixtures
        )
        assert (match.match_id, mapping, candidates) == ("apif:900", "matched", ["apif:900"])
        assert evidence["basis"] == "exact_team_names"


def test_a_row_jc_sells_is_left_to_the_id_bridge(session):
    before = _state(session)
    bridge = dict(
        fixture_id="1416964",
        jc_id="jc:2099999",
        jc_num="周五009",
        league=LEAGUE,
        home="南锡",
        away="第戎",
        home_id="1130",
        away_id="774",
        kickoff_utc=KICKOFF.isoformat(),
    )
    # The bridge names a JC fixture that is not stored yet: the list does not stand in for it.
    match, mapping, _, _ = _collect(session, bridges={"1416964": bridge})
    assert (match, mapping) == (None, "unmatched")
    assert _state(session) == before


def test_a_moved_kickoff_is_followed_at_collection_and_refused_in_between(session):
    _collect(session)
    later = _fixture(kickoff_utc=KICKOFF + timedelta(hours=2))
    # A re-reading between collections does not write: the mismatch is a conflict to report.
    with pytest.raises(ValueError, match="身份冲突"):
        _map_fixture(session, later, {}, mapped_only=True)
    match, mapping, _, _ = _collect(session, later)
    assert (match.match_id, mapping) == ("c500:1416964", "matched")
    assert match.kickoff_utc == "2026-11-20T21:00:00.000000Z"
    assert len([m for m in _state(session)[1] if m[1] == "c500"]) == 1


def test_no_own_fixture_from_a_placeholder_kickoff(session):
    # 00:00 Beijing on a day of its own: the source has not printed the time yet.
    placeholder = datetime(2026, 11, 21, 16, tzinfo=UTC)
    issue = SfcSnapshot(
        "26999",
        None,
        [
            _fixture(kickoff_utc=placeholder),
            _fixture(
                seq=10,
                fixture_id="1416966",
                home="圣红星",
                away="波城FC",
                home_id="6597",
                away_id="2329",
            ),
        ],
        "deadline_anchored",
    )
    before = _state(session)
    rows = _snapshot_rows(session, issue, {}, mapped_only=True, own_fixtures=True)
    assert [(r["seq"], r["mapping"], r["match_id"]) for r in rows] == [
        (7, "unmatched", None),
        (10, "matched", "c500:1416966"),
    ]
    assert rows[0]["kickoff_basis"] == "placeholder"
    assert rows[1]["matched_identity"]["source"] == "c500"
    assert len(_state(session)[1]) == len(before[1]) + 1


def test_only_a_single_league_model_takes_the_lists_fixtures():
    assert fixture_sources(dict(sources=["fd", "jc"])) == ["fd", "jc", SFC_FIXTURE_SOURCE]
    assert fixture_sources(dict(sources=["jc"], pooled=True)) == ["jc"]
    # The earlier local workflow trained nothing on such rows but named the source itself.
    assert fixture_sources(dict(sources=["c500"])) == ["c500"]


def test_the_league_model_forecasts_an_own_fixture_and_training_never_reads_it(setup_batch):
    from zucai.predict.backtest import read_history
    from zucai.report.batch import predict_batch

    engine, path, model, _ = setup_batch
    kickoff = CUTOFF + timedelta(hours=1)
    with session_scope(engine) as session:
        teams = {t.name: t.id for t in session.query(Team)}
        session.add(
            Match(
                match_id="c500:77",
                source="c500",
                league="L",
                home_team_id=teams["A"],
                away_team_id=teams["B"],
                kickoff_utc=kickoff,
                status="scheduled",
            )
        )
        session.flush()
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1", "c500:77"],
            persist=False,
        )
        own, sold = (
            next(p for p in batch["predictions"] if p["match_id"] == key)
            for key in ("c500:77", "jc:1")
        )
        assert (own["status"], own["source"], own["model_name"]) == (
            "predicted",
            "c500",
            "dixon-coles",
        )
        # Same teams, same league, same kick-off as the JC-sold fixture: the same numbers.
        assert own["model_probabilities"] == sold["model_probabilities"]
        assert own["model_version"] == sold["model_version"]
        assert own["market"] is None and own["market_reference"] is None
        # The model's own description of what it was trained on is unchanged.
        assert batch["models"][0]["sources"] == ["jc"]
        # Training reads finished results of the model's own sources: give the league one
        # and see that the list's fixture is not among the rows read.
        session.add(
            Match(
                match_id="jc:finished",
                source="jc",
                league="L",
                home_team_id=teams["A"],
                away_team_id=teams["B"],
                kickoff_utc=CUTOFF - timedelta(days=2),
                status="finished",
            )
        )
        session.add(
            Result(
                match_id="jc:finished",
                home_goals=1,
                away_goals=0,
                available_utc=CUTOFF - timedelta(days=1),
                available_source="captured",
            )
        )
        session.flush()
        assert list(read_history(session, "L", "jc").match_id) == ["jc:finished"]
        assert list(read_history(session, "L", None).match_id) == ["jc:finished"]
    assert model.sources == ["jc"]


def test_report_and_digest_say_which_rows_come_from_the_list_itself():
    from zucai.report.sfc import own_fixture_note

    rows = [
        dict(seq=5, mapping_evidence=dict(basis="provider_fixture_and_team_ids")),
        dict(seq=6, mapping_evidence=dict(basis="sfc_own_fixture")),
        dict(seq=10, mapping_evidence=dict(basis="sfc_own_fixture")),
        dict(seq=11),
    ]
    note = own_fixture_note(rows)
    assert note.startswith("第 6、10 场：对阵与开球时间取自期次来源本身（第三方）。")
    assert "球队模型" in note and own_fixture_note(rows[:1] + rows[3:]) == ""
