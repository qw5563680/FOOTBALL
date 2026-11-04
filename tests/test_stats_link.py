import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, Team
from zucai.stats_link import link_fixtures

FIX = Path(__file__).parents[1] / "data/fixtures/t31"


def source():
    fixture = json.loads((FIX / "fixture.json").read_text())
    raw = dict(
        get="fixtures",
        parameters={"league": "2", "season": "2024"},
        errors=[],
        results=1,
        paging={"current": 1, "total": 1},
        response=[fixture],
    )
    meta = dict(
        captured_utc="2026-11-04T12:00:00Z",
        raw_sha256=hashlib.sha256(json.dumps(raw).encode()).hexdigest(),
        raw_location="fixture://t31/fixture.json",
    )
    return raw, meta


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(tmp_path / "feeds.db")
    init_db(engine)
    data = json.loads((FIX / "store.json").read_text())
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in data["teams"])
        session.flush()
        session.add_all(Match(**m) for m in data["matches"])
    yield engine
    engine.dispose()


def link(session, raw=None, aliases=None):
    document, meta = source()
    return link_fixtures(
        session,
        raw or document,
        league_id=2,
        season=2024,
        aliases=aliases or json.loads((FIX / "aliases.json").read_text()),
        metadata=meta,
    )


def test_formal_same_id_links_cross_league_without_creating_entities(engine):
    with session_scope(engine) as session:
        before = (
            list(session.scalars(select(Team.id))),
            list(session.scalars(select(Match.match_id))),
        )
        result = link(session)
        assert result["linked"] == 1 and not result["audit"]
        index = session.get(ApifFixtureIndex, "102")
        assert index.match_id == "jc:1"
        assert index.evidence["home"]["source_leagues"] == ["英超"]
        assert link(session)["linked"] == 0
        assert before == (
            list(session.scalars(select(Team.id))),
            list(session.scalars(select(Match.match_id))),
        )


@pytest.mark.parametrize(
    "case,reason",
    [
        ("reverse", "主客反向"),
        ("many", "多对一"),
        ("temporary", "身份临时"),
        ("incomplete", "候选不全"),
        ("window", "同窗冲突"),
    ],
)
def test_link_conflicts_are_audited(engine, case, reason):
    aliases = json.loads((FIX / "aliases.json").read_text())
    with session_scope(engine) as session:
        match = session.get(Match, "jc:1")
        if case == "reverse":
            match.home_team_id, match.away_team_id = match.away_team_id, match.home_team_id
        elif case == "many":
            session.add(
                Match(
                    match_id="fdorg:1",
                    source="fdorg",
                    league=match.league,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    kickoff_utc=match.kickoff_utc,
                )
            )
        elif case == "temporary":
            aliases["英超"]["1"]["status"] = "proposed"
        elif case == "incomplete":
            match.kickoff_utc = "2024-10-03T01:00:00Z"
        else:
            session.add(Team(id=3, name="其他队", aliases=[]))
            session.flush()
            session.add(
                Match(
                    match_id="jc:other",
                    source="jc",
                    league=match.league,
                    home_team_id=1,
                    away_team_id=3,
                    kickoff_utc=match.kickoff_utc,
                )
            )
        session.flush()
        result = link(session, aliases=aliases)
        assert result["linked"] == 0
        assert result["audit"][0]["reason"] == reason
        assert list(session.scalars(select(ApifFixtureIndex))) == []


def test_many_provider_fixtures_cannot_claim_one_match_even_in_same_batch(engine):
    raw, _ = source()
    other = copy.deepcopy(raw["response"][0])
    other["fixture"]["id"] = 103
    raw["response"].append(other)
    raw["results"] = 2
    with session_scope(engine) as session:
        result = link(session, raw=raw)
        assert result["linked"] == 0 and len(result["audit"]) == 2
        assert {r["reason"] for r in result["audit"]} == {"多对一"}


def test_season_boundary_and_existing_index_cannot_be_retargeted(engine):
    with session_scope(engine) as session:
        assert link(session)["linked"] == 1
        raw, _ = source()
        raw["response"][0]["fixture"]["id"] = 103
        assert link(session, raw=raw)["audit"][0]["reason"] == "多对一"


def test_declared_european_season_must_contain_kickoff(engine):
    raw, _ = source()
    raw["response"][0]["fixture"]["date"] = "2026-10-02T19:00:00Z"
    del raw["response"][0]["fixture"]["timestamp"]
    with session_scope(engine) as session:
        session.get(Match, "jc:1").kickoff_utc = "2026-10-02T19:00:00Z"
        session.flush()
        result = link(session, raw=raw)
        assert result["linked"] == 0
        assert result["audit"][0]["reason"] == "候选不全"
