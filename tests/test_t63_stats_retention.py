"""Retained-row regressions use the explicitly synthetic T10/T31/T63 samples."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, Odds, Result, ResultSourceLink, Team
from zucai.result_source_links import record_result_link
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.stats_link import link_fixtures
from zucai.stats_schedule import match_units, plan

FIX = Path(__file__).parents[1] / "data/fixtures"
SEED = json.loads((FIX / "t63/result-links.json").read_text())
VERIFIED = datetime.fromisoformat("2026-11-10T06:00:00Z")
NOW = datetime.fromisoformat("2026-11-10T09:15:00Z")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("retained-row regression must stay offline")

    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    engine = get_engine(tmp_path / "retained.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in SEED["teams"])
        session.flush()
        add_match(session, "jc")
    yield engine
    engine.dispose()


def add_match(session, source, *, key=None, kickoff=None):
    row = next(row for row in SEED["matches"] if row["match_id"] == source + ":1").copy()
    row["match_id"] = key or source + ":1"
    row["kickoff_utc"] = kickoff or "2026-11-10T10:00:00Z"
    match = Match(**row)
    session.add(match)
    session.flush()
    return match


def aliases():
    values = json.loads((FIX / "t31/aliases.json").read_text())
    for team in SEED["teams"][:2]:
        values["英超"][str(team["id"])]["canonical"] = team["name"]
    return values


def link(session, *, rows=None, formal_aliases=None):
    fixture = json.loads((FIX / "t31/fixture.json").read_text())
    fixture["fixture"].update(id=1, date="2026-11-10T10:00:00Z")
    fixture["fixture"].pop("timestamp")
    fixture["league"] = dict(id=39, season=2026)
    raw = dict(
        get="fixtures",
        parameters=dict(league=39, season=2026),
        errors=[],
        results=len(rows) if rows is not None else 1,
        paging=dict(current=1, total=1),
        response=rows if rows is not None else [fixture],
    )
    return link_fixtures(
        session,
        raw,
        league_id=39,
        season=2026,
        aliases=formal_aliases or aliases(),
        metadata=dict(
            captured_utc=NOW,
            raw_sha256=hashlib.sha256(json.dumps(raw).encode()).hexdigest(),
            raw_location="fixture://synthetic-t31-t63-retention",
        ),
    )


def proof(session, left, right, *, verified=VERIFIED):
    record_result_link(session, session.get(Match, left), session.get(Match, right), verified)


def add_index(session, match_id, fixture_id="1"):
    match = session.get(Match, match_id)
    index = ApifFixtureIndex(
        provider_fixture_id=fixture_id,
        match_id=match_id,
        league=match.league,
        provider_league_id=39,
        season=2026,
        kickoff_utc=match.kickoff_utc,
        home_provider_id=1,
        away_provider_id=2,
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
        captured_utc=VERIFIED,
        raw_sha256="a" * 64,
        raw_location="fixture://synthetic-t63-existing-index",
        parser_version="synthetic",
        evidence={"original": True},
    )
    session.add(index)
    session.flush()
    return index


def test_retained_import_keeps_the_original_statistics_target_and_scores(engine):
    values = json.loads((FIX / "t10/synthetic_ingest.json").read_text())["fixture"]
    values.update(
        provider_match_id=1,
        competition_code="39",
        league="英超",
        home_provider_id=1,
        away_provider_id=2,
        kickoff_utc=datetime.fromisoformat("2026-11-10T10:00:00Z"),
    )
    for field in ("full", "half"):
        values[field] = tuple(values[field])
    seen = datetime.fromisoformat("2026-11-10T15:00:00Z")
    with session_scope(engine) as session:
        # T10's canonical names survive the normal result-import normalization.
        formal = aliases()
        for team_id, name in ((1, "北港"), (2, "东岸")):
            team = session.get(Team, team_id)
            team.name = name
            formal["英超"][str(team_id)]["canonical"] = name
        jc = session.get(Match, "jc:1")
        jc.status = "finished"
        session.add(
            Result(
                match_id=jc.match_id,
                home_goals=0,
                away_goals=2,
                available_utc=seen,
                available_source="captured",
            )
        )
        session.flush()
        before = dict(session.execute(select(Match.__table__)).mappings().one())
        assert (
            import_apif_games(
                session,
                [ApiGame(**values)],
                seen=seen,
                assume=None,
                aliases=formal,
                retain_results=True,
                imported_utc=seen,
            )["inserted"]
            == 1
        )
        assert session.get(Match, "apif:1") is not None
        scores = [dict(row) for row in session.execute(select(Result.__table__)).mappings()]
        assert link(session, formal_aliases=formal)["linked"] == 1
        assert session.get(ApifFixtureIndex, "1").match_id == "jc:1"
        assert scores == [dict(row) for row in session.execute(select(Result.__table__)).mappings()]
        assert before == dict(
            session.execute(select(Match.__table__).where(Match.match_id == "jc:1"))
            .mappings()
            .one()
        )


@pytest.mark.parametrize(
    "sources,expected",
    [
        (("jc", "fd", "fdorg", "apif"), "jc:1"),
        (("fd", "fdorg", "apif"), "fd:1"),
        (("fdorg", "apif"), "fdorg:1"),
    ],
)
def test_proven_retained_rows_choose_the_original_source_priority(engine, sources, expected):
    with session_scope(engine) as session:
        if "jc" not in sources:
            session.delete(session.get(Match, "jc:1"))
        for source in sources:
            if source != "jc":
                add_match(session, source)
        for source in sources:
            if source != "apif":
                proof(session, "apif:1", source + ":1")
        before = [dict(row) for row in session.execute(select(Match.__table__)).mappings()]
        result = link(session)
        assert result["linked"] == 1 and result["rejected"] == 0
        assert session.get(ApifFixtureIndex, "1").match_id == expected
        assert link(session)["unchanged"] == 1
        assert before == [dict(row) for row in session.execute(select(Match.__table__)).mappings()]


def test_transitive_proof_includes_scheduled_nodes(engine):
    with session_scope(engine) as session:
        add_match(session, "apif", kickoff="2026-11-10T09:30:00Z")
        add_match(session, "fdorg")
        proof(session, "apif:1", "fdorg:1")
        proof(session, "fdorg:1", "jc:1")
        assert link(session)["linked"] == 1
        assert session.get(ApifFixtureIndex, "1").match_id == "jc:1"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("no_proof", "多对一"),
        ("unproven_third", "多对一"),
        ("same_source", "多对一"),
        ("stale_proof", "多对一"),
        ("reverse", "主客反向"),
        ("shared", "同窗冲突"),
    ],
)
def test_proof_does_not_hide_unproven_or_conflicting_candidates(engine, case, reason):
    with session_scope(engine) as session:
        paid = add_match(session, "apif")
        if case != "no_proof":
            proof(session, "apif:1", "jc:1")
        if case in {"unproven_third", "same_source", "reverse", "shared"}:
            other = add_match(
                session,
                "jc" if case == "same_source" else "fd",
                key="jc:2" if case == "same_source" else None,
            )
            if case == "reverse":
                other.home_team_id, other.away_team_id = 2, 1
            elif case == "shared":
                other.away_team_id = 3
        elif case == "stale_proof":
            paid.kickoff_utc = "2026-11-10T10:01:00Z"
        session.flush()
        result = link(session)
        assert result["linked"] == 0 and result["audit"][0]["reason"] == reason
        assert list(session.scalars(select(ApifFixtureIndex))) == []


def test_existing_index_remains_on_its_valid_proven_member(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        old = add_index(session, "apif:1")
        before = {
            column.name: copy.deepcopy(getattr(old, column.name))
            for column in old.__table__.columns
        }
        proof(session, "apif:1", "jc:1")
        result = link(session)
        assert result["unchanged"] == 1 and result["rejected"] == 0
        assert {
            column.name: getattr(old, column.name) for column in old.__table__.columns
        } == before


def test_different_fixture_cannot_claim_an_indexed_component_member(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        add_index(session, "apif:1", "2")
        proof(session, "apif:1", "jc:1")
        result = link(session)
        assert result["linked"] == 0 and result["audit"][0]["reason"] == "多对一"
        assert list(session.scalars(select(ApifFixtureIndex.match_id))) == ["apif:1"]


def test_conflicting_existing_indices_on_proven_members_are_not_accepted(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        add_index(session, "jc:1")
        add_index(session, "apif:1", "2")
        proof(session, "apif:1", "jc:1")
        result = link(session)
        assert result["unchanged"] == 0 and result["rejected"] == 1


def offset_component(session):
    session.get(Match, "jc:1").kickoff_utc = "2026-11-10T07:00:00Z"
    add_match(session, "fd", kickoff="2026-11-10T13:00:00Z")
    add_match(session, "fdorg")
    proof(session, "fdorg:1", "jc:1")
    proof(session, "fdorg:1", "fd:1")


def offset_fixture(fixture_id, hour):
    fixture = json.loads((FIX / "t31/fixture.json").read_text())
    fixture["fixture"].update(id=fixture_id, date=f"2026-11-10T{hour:02}:00:00Z")
    fixture["fixture"].pop("timestamp")
    fixture["league"] = dict(id=39, season=2026)
    return fixture


def test_component_claim_outside_fixture_window_still_blocks_new_index(engine):
    with session_scope(engine) as session:
        offset_component(session)
        add_index(session, "jc:1", "2")
        result = link(session, rows=[offset_fixture(1, 13)])
        assert result["linked"] == 0 and result["audit"][0]["reason"] == "多对一"
        assert result["audit"][0]["candidates"] == ["fd:1", "fdorg:1", "jc:1"]


def test_retained_api_fixture_id_cannot_authorize_a_different_provider_fixture(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        proof(session, "apif:1", "jc:1")
        result = link(session, rows=[offset_fixture(2, 10)])
        assert result["linked"] == 0 and result["audit"][0]["reason"] == "同窗冲突"
        assert list(session.scalars(select(ApifFixtureIndex))) == []


def test_separate_incoming_windows_cannot_claim_two_members_of_the_same_component(engine):
    with session_scope(engine) as session:
        offset_component(session)
        result = link(session, rows=[offset_fixture(1, 7), offset_fixture(2, 13)])
        assert result["linked"] == 0 and result["rejected"] == 2
        assert {row["reason"] for row in result["audit"]} == {"多对一"}
        assert list(session.scalars(select(ApifFixtureIndex))) == []


def test_proven_new_representative_cannot_retarget_an_unrelated_old_index(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        other = add_match(session, "fd", kickoff="2026-11-09T10:00:00Z")
        old = add_index(session, other.match_id)
        proof(session, "apif:1", "jc:1")
        result = link(session)
        assert result["rejected"] == 1 and result["unchanged"] == 0
        assert result["audit"][0]["detail"] == "既有索引禁止重指向"
        assert old.match_id == other.match_id


def test_corrupt_same_source_proof_component_fails_closed(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        other = add_match(session, "jc", key="jc:2")
        proof(session, "apif:1", "jc:1")
        original = session.scalar(select(ResultSourceLink))
        fields = {
            column.name: getattr(original, column.name) for column in original.__table__.columns
        }
        fields["counterpart_id"] = other.match_id
        session.add(ResultSourceLink(**fields))
        session.flush()
        with pytest.raises(ValueError, match="同来源"):
            link(session)
        assert list(session.scalars(select(ApifFixtureIndex))) == []


@pytest.mark.parametrize("source,transitive", [("apif", False), ("fdorg", False), ("fd", True)])
def test_proof_only_sale_relationship_schedules_lineups_for_existing_index(
    engine, source, transitive
):
    with session_scope(engine) as session:
        add_match(session, source)
        if transitive:
            add_match(session, "apif")
            proof(session, "apif:1", "fd:1")
            proof(session, "apif:1", "jc:1")
        else:
            proof(session, source + ":1", "jc:1")
        add_index(session, source + ":1")
        session.add(
            Odds(match_id="jc:1", pool="had", line=0, selection="h", price=2, snapshot_utc=VERIFIED)
        )
    rows = list(
        match_units(engine.url.database, plan("39", "2026", "lineups", NOW), NOW, daily=True)
    )
    assert [(row["fixture_id"], row["match_id"], row["phase"]) for row, _ in rows] == [
        ("1", source + ":1", "pregame")
    ]
    assert rows[0][0]["key"] == "apif:39:2026:lineups:1:pregame"
    with session_scope(engine) as session:
        assert all(match.counterpart_id is None for match in session.scalars(select(Match)))


@pytest.mark.parametrize("case", ["no_proof", "future_proof", "stale_proof", "no_sale"])
def test_unverified_or_unsold_relationship_does_not_schedule_lineups(engine, case):
    with session_scope(engine) as session:
        paid = add_match(session, "apif")
        if case != "no_proof":
            proof(
                session,
                "apif:1",
                "jc:1",
                verified=NOW + timedelta(seconds=1) if case == "future_proof" else VERIFIED,
            )
        if case == "stale_proof":
            paid.kickoff_utc = "2026-11-10T10:01:00Z"
        add_index(session, "apif:1")
        if case != "no_sale":
            session.add(
                Odds(
                    match_id="jc:1",
                    pool="had",
                    line=0,
                    selection="h",
                    price=2,
                    snapshot_utc=VERIFIED,
                )
            )
    assert (
        list(match_units(engine.url.database, plan("39", "2026", "lineups", NOW), NOW, daily=True))
        == []
    )


def test_other_fixture_id_cannot_obtain_pregame_eligibility_from_retained_api_proof(engine):
    with session_scope(engine) as session:
        add_match(session, "apif")
        proof(session, "apif:1", "jc:1")
        add_index(session, "apif:1", "2")
        session.add(
            Odds(match_id="jc:1", pool="had", line=0, selection="h", price=2, snapshot_utc=VERIFIED)
        )
    assert (
        list(match_units(engine.url.database, plan("39", "2026", "lineups", NOW), NOW, daily=True))
        == []
    )


def test_conflicting_component_indices_do_not_add_an_extra_pregame_request(engine):
    with session_scope(engine) as session:
        add_match(session, "fdorg")
        proof(session, "fdorg:1", "jc:1")
        add_index(session, "fdorg:1")
        add_index(session, "jc:1", "2")
        session.add(
            Odds(match_id="jc:1", pool="had", line=0, selection="h", price=2, snapshot_utc=VERIFIED)
        )
    rows = list(
        match_units(engine.url.database, plan("39", "2026", "lineups", NOW), NOW, daily=True)
    )
    assert {row["fixture_id"] for row, _ in rows} == {"2"}
