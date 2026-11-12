"""T54 exact JC aliases; synthetic identities on archived offline source shapes."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Result, Team
from zucai.sources.base import RawStore
from zucai.sources.results import (
    GameResult,
    ResultRowsRejected,
    SportteryResults,
    apply_result,
    fetch_results,
)
from zucai.sources.sporttery import API, JcMatch, fetch_schedule, save_schedule

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
CAPTURED = datetime(2026, 11, 12, 3, 30, tzinfo=UTC)
KICKOFF = datetime(2026, 11, 12, 19, 45, tzinfo=UTC)
SEEN = datetime(2026, 11, 13, 3, 30, tzinfo=UTC)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "isolated/feeds.db"
    monkeypatch.setenv("ZUCAI_DB", str(path))
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(id=1149, name="阿尔巴尼亚", aliases=["apif:欧国联:team:778"]),
                Team(id=1148, name="芬兰", aliases=["apif:欧国联:team:1099"]),
            ]
        )
    yield engine
    engine.dispose()


def schedule(home="阿尔巴尼", league="欧国联", match_id="jc:2042694"):
    return JcMatch(match_id, date(2026, 11, 12), "周四003", league, home, "芬兰", KICKOFF, [])


def result(home="阿尔巴尼", match_id="jc:2042694"):
    return GameResult(
        match_id,
        "欧国联",
        "周四003",
        home,
        "芬兰",
        KICKOFF.date(),
        "finished",
        (1, 0),
        (0, 0),
        KICKOFF,
    )


def candidate_file(engine):
    return Path(engine.url.database).parent / "state/alias_candidates/jc.json"


def test_schedule_reuses_approved_canonical_and_does_not_attach_global_short_alias(database):
    with session_scope(database) as session:
        save_schedule(session, [schedule()], CAPTURED)
        assert session.get(Match, "jc:2042694").home_team_id == 1149
        assert len(session.scalars(select(Team)).all()) == 2
        assert session.get(Team, 1149).aliases == ["apif:欧国联:team:778"]


def test_alias_is_scoped_to_exact_league_and_exact_short_name(database):
    with session_scope(database) as session:
        save_schedule(session, [schedule()], CAPTURED)
        save_schedule(session, [schedule(league="另一联赛", match_id="jc:other")], CAPTURED)
        save_schedule(session, [schedule(home="阿尔巴", match_id="jc:prefix")], CAPTURED)
        other = session.get(Match, "jc:other")
        prefix = session.get(Match, "jc:prefix")
        assert other.home_team_id != 1149
        assert prefix.home_team_id not in {1149, other.home_team_id}


def schedule_body(home):
    body = json.loads((FIXTURES / "sporttery_calculator_20261101.json").read_bytes())
    group = body["value"]["matchInfoList"][0]
    row = group["subMatchList"][0]
    row.update(
        matchId=2042694,
        matchNumStr="周四003",
        leagueAbbName="欧国联",
        homeTeamAbbName=home,
        awayTeamAbbName="芬兰",
        matchDate="2026-11-13",
        matchTime="03:45",
    )
    group.update(businessDate="2026-11-12", subMatchList=[row])
    body["value"]["matchInfoList"] = [group]
    return body


def test_unknown_schedule_team_is_distinct_and_candidate_is_persisted_idempotently(
    database, monkeypatch
):
    save = RawStore.save_response
    monkeypatch.setattr(
        RawStore, "save_response", lambda self, *a: save(self, *a, captured_at=CAPTURED)
    )
    with responses.RequestsMock() as api:
        api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=schedule_body("阿尔巴"))
        assert fetch_schedule()[0] == 1
        path = candidate_file(database)
        assert path.exists(), "A newly created JC name must be queued beside its database"
        before = path.read_bytes()
        assert fetch_schedule()[0] == 1
        assert path.read_bytes() == before
    with session_scope(database) as session:
        match = session.get(Match, "jc:2042694")
        assert match.home_team_id != 1149
        candidate = json.loads(before)["欧国联"]["阿尔巴"]
        assert candidate["status"] == "pending"
        assert candidate["canonical"] is None
        assert candidate["team_id"] == match.home_team_id
        assert candidate["evidence"] == [
            {
                "match_id": "jc:2042694",
                "side": "home",
                "kickoff_utc": "2026-11-12T19:45:00.000000Z",
                "captured_utc": "2026-11-12T03:30:00.000000Z",
            }
        ]
    assert not Path("data/state/alias_candidates/jc.json").exists()


def test_existing_jc_identity_conflict_requires_explicit_repair(database):
    with session_scope(database) as session:
        session.add(Team(id=1156, name="阿尔巴尼", aliases=[]))
        session.flush()
        upsert_match(
            session,
            match_id="jc:2042694",
            source="jc",
            league="欧国联",
            home_team_id=1156,
            away_team_id=1148,
            kickoff_utc=KICKOFF,
            status="scheduled",
        )
    with pytest.raises(ValueError, match="身份"):
        with session_scope(database) as session:
            save_schedule(session, [schedule()], CAPTURED)


def test_result_uses_approved_canonical_even_when_old_short_team_is_retained(database):
    with session_scope(database) as session:
        session.add(Team(id=1156, name="阿尔巴尼", aliases=[]))
        upsert_match(
            session,
            match_id="jc:2042694",
            source="jc",
            league="欧国联",
            home_team_id=1149,
            away_team_id=1148,
            kickoff_utc=KICKOFF,
            status="scheduled",
        )
    with session_scope(database) as session:
        assert apply_result(session, result(), SEEN) == "captured"
        assert session.get(Match, "jc:2042694").home_team_id == 1149
        assert session.get(Result, "jc:2042694").home_goals == 1
        assert session.get(Team, 1156).name == "阿尔巴尼"


def test_unknown_results_team_candidate_is_committed_but_rejected_row_is_not(database, monkeypatch):
    # Bypass only the network reader; exercise parsing-independent result import,
    # per-row savepoints, identity checks and on-disk candidate persistence.
    games = [result("阿尔巴", "jc:accepted"), replace(result("坏队", "jc:rejected"), full=None)]
    monkeypatch.setattr(
        SportteryResults, "day_results", lambda self, day: [(g, SEEN) for g in games]
    )
    with pytest.raises(ResultRowsRejected):
        fetch_results(KICKOFF.date(), KICKOFF.date())
    with session_scope(database) as session:
        match = session.get(Match, "jc:accepted")
        assert match.home_team_id != 1149
        assert session.get(Match, "jc:rejected") is None
        assert session.scalar(select(Team).where(Team.name == "坏队")) is None
    path = candidate_file(database)
    assert path.exists(), "Successful result rows must persist new-name candidates"
    candidates = json.loads(path.read_bytes())["欧国联"]
    assert set(candidates) == {"阿尔巴"}
    assert candidates["阿尔巴"]["evidence"][0]["match_id"] == "jc:accepted"


def test_unapproved_alias_does_not_reuse_its_proposed_canonical(database):
    aliases = {"欧国联": {"阿尔巴": {"canonical": "阿尔巴尼亚", "status": "pending"}}}
    with session_scope(database) as session:
        save_schedule(session, [schedule("阿尔巴")], CAPTURED, aliases=aliases)
        assert session.get(Match, "jc:2042694").home_team_id != 1149


def test_approved_canonical_with_conflicting_existing_alias_is_rejected(database):
    with session_scope(database) as session:
        session.add(Team(name="另一球队", aliases=["阿尔巴尼亚"]))
    with pytest.raises(ValueError, match="身份"):
        with session_scope(database) as session:
            save_schedule(session, [schedule()], CAPTURED)


def test_malformed_approved_alias_fails_closed(database):
    with pytest.raises(ValueError, match="规范名"):
        with session_scope(database) as session:
            save_schedule(
                session,
                [schedule()],
                CAPTURED,
                aliases={"欧国联": {"阿尔巴尼": {"status": "approved", "canonical": ""}}},
            )


def test_rollback_does_not_publish_candidates_for_uncommitted_schedule_teams(database, monkeypatch):
    with session_scope(database) as session:
        upsert_match(
            session,
            match_id="jc:2042694",
            source="jc",
            league="欧国联",
            home_team_id=1149,
            away_team_id=1148,
            kickoff_utc=KICKOFF,
            status="scheduled",
        )
    save = RawStore.save_response
    monkeypatch.setattr(
        RawStore, "save_response", lambda self, *a: save(self, *a, captured_at=CAPTURED)
    )
    with responses.RequestsMock() as api:
        api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=schedule_body("阿尔巴"))
        with pytest.raises(ValueError, match="身份"):
            fetch_schedule()
    assert not candidate_file(database).exists()
    with session_scope(database) as session:
        assert session.get(Match, "jc:2042694").home_team_id == 1149
        assert session.scalar(select(Team).where(Team.name == "阿尔巴")) is None


@pytest.mark.parametrize(
    "failure", ["disk", "candidate_identity", "commit", "late_write", "late_write_absent"]
)
def test_candidate_and_team_creation_roll_back_together(database, monkeypatch, failure):
    from zucai.sources import sporttery_aliases

    path = candidate_file(database)
    path.parent.mkdir(parents=True)
    original = b"{}\n"
    if failure == "candidate_identity":
        original = json.dumps({"欧国联": {"阿尔巴": {"team_id": 1, "evidence": []}}}).encode()
    if failure != "late_write_absent":
        path.write_bytes(original)

    def fail(*args, **kwargs):
        raise OSError("injected commit or candidate write failure")

    if failure == "disk":
        monkeypatch.setattr(sporttery_aliases, "_atomic_write", fail)
    elif failure.startswith("late_write"):
        write = sporttery_aliases.save_candidates

        def write_then_fail(*args):
            write(*args)
            fail()

        monkeypatch.setattr(sporttery_aliases, "save_candidates", write_then_fail)
    elif failure == "commit":
        event.listen(Session, "before_commit", fail)
    save = RawStore.save_response
    monkeypatch.setattr(
        RawStore, "save_response", lambda self, *a: save(self, *a, captured_at=CAPTURED)
    )
    try:
        with responses.RequestsMock() as api:
            api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=schedule_body("阿尔巴"))
            with pytest.raises(ValueError if failure == "candidate_identity" else OSError):
                fetch_schedule()
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail)
    if failure == "late_write_absent":
        assert not path.exists()
    else:
        assert path.read_bytes() == original
    with session_scope(database) as session:
        assert session.get(Match, "jc:2042694") is None
        assert session.scalar(select(Team).where(Team.name == "阿尔巴")) is None
