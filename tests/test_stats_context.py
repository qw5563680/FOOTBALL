"""Verified archived fixture contexts and actual capture clocks, entirely offline."""

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, MatchLineup, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_context import refresh_context
from zucai.stats_link import link_fixtures
from zucai.stats_storage import ingest_apif, read_audit

FIX = Path(__file__).parents[1] / "data/fixtures"
DOCUMENT = json.loads((FIX / "t32/daily_context.json").read_text())
ALIASES = json.loads((FIX / "t31/aliases.json").read_text())


def page():
    return copy.deepcopy(DOCUMENT["date_page"])


def initial():
    doc = page()
    doc["parameters"] = {"league": "2", "season": "2024"}
    doc["response"][0]["fixture"]["status"]["short"] = "NS"
    return doc


@pytest.fixture
def database(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("context refresh must only use archived fixtures")

    monkeypatch.setattr("requests.sessions.Session.request", forbid_network)
    config = tmp_path / "config/team_aliases"
    config.mkdir(parents=True)
    (config / "api_football.json").write_text(json.dumps(ALIASES))
    monkeypatch.setattr("zucai.stats_context.CONFIG_ROOT", config.parent)
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    store = json.loads((FIX / "t31/store.json").read_text())
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in store["teams"])
        session.flush()
        session.add_all(Match(**row) for row in store["matches"])
        session.flush()
        assert (
            link_fixtures(
                session,
                initial(),
                league_id=2,
                season=2024,
                aliases=ALIASES,
                metadata=dict(
                    captured_utc="2024-10-02T17:00:00Z",
                    raw_sha256="a" * 64,
                    raw_location="fixture://initial",
                ),
            )["linked"]
            == 1
        )
    engine.dispose()
    return path


def save(tmp_path, doc=None, *, url=None, captured="2024-10-02T20:00:00Z"):
    return RawStore(tmp_path / "raw").save(
        "api-football",
        "fixtures",
        json.dumps(doc or page()).encode(),
        "json",
        captured_at=instant(captured),
        url=url or "https://v3.football.api-sports.io/fixtures?date=2024-10-02",
        status_code=200,
    )


def refresh(database, tmp_path):
    return refresh_context(database, tmp_path / "raw", lambda path: "fixture://" + path.name)


def evidence(database):
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            return copy.deepcopy(session.get(ApifFixtureIndex, "102").evidence)
    finally:
        engine.dispose()


def test_verified_date_page_refreshes_context_and_provenance_idempotently(database, tmp_path):
    path = save(tmp_path)
    meta = json.loads(path.with_suffix(".json.meta.json").read_text())
    assert refresh(database, tmp_path) == 1
    result = evidence(database)
    assert result["fixture"] == page()["response"][0]
    assert instant(result["fixture_captured_utc"]) == instant(meta["fetched_at_utc"])
    assert result["fixture_raw_sha256"] == meta["sha256"]
    assert result["fixture_raw_location"] == "fixture://" + path.name
    before = database.read_bytes()
    assert refresh(database, tmp_path) == 0
    assert database.read_bytes() == before


def test_date_page_can_link_existing_match_and_reports_change(database, tmp_path):
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.delete(session.get(ApifFixtureIndex, "102"))
    engine.dispose()
    save(tmp_path)
    assert refresh(database, tmp_path) == 1
    assert evidence(database)["fixture"] == page()["response"][0]


@pytest.mark.parametrize(
    "url",
    [
        "http://v3.football.api-sports.io/fixtures?date=2024-10-02",
        "https://v3.football.api-sports.io.evil/fixtures?date=2024-10-02",
        "https://user@v3.football.api-sports.io/fixtures?date=2024-10-02",
        "https://v3.football.api-sports.io/fixtures?date=2024-10-02#fragment",
        "https://v3.football.api-sports.io/injuries?date=2024-10-02",
        "https://v3.football.api-sports.io/fixtures?date=2024-10-03",
        "https://v3.football.api-sports.io/fixtures?date=2024-10-02&date=2024-10-02",
        "https://v3.football.api-sports.io/fixtures?date=2024-10-02&key=unexpected",
    ],
)
def test_untrusted_original_url_or_parameter_scope_cannot_refresh(database, tmp_path, url):
    save(tmp_path, url=url)
    before = database.read_bytes()
    with pytest.raises(ValueError):
        refresh(database, tmp_path)
    assert database.read_bytes() == before


def test_sha_and_exact_envelope_parameter_validation(database, tmp_path):
    path = save(tmp_path)
    path.write_text(json.dumps(initial()))
    before = database.read_bytes()
    with pytest.raises(ValueError, match="哈希"):
        refresh(database, tmp_path)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "cross_league_duplicate",
        "reverse",
        "date_scope",
        "ambiguous_match",
        "corrupt_index_team",
    ],
)
def test_rejected_identity_observation_never_refreshes_existing_context(database, tmp_path, case):
    doc = page()
    if case in {"duplicate", "cross_league_duplicate"}:
        other = copy.deepcopy(doc["response"][0])
        if case == "cross_league_duplicate":
            other["league"]["id"] = 39
        doc["response"].append(other)
        doc["results"] = 2
    elif case == "reverse":
        row = doc["response"][0]
        row["teams"]["home"], row["teams"]["away"] = row["teams"]["away"], row["teams"]["home"]
    elif case == "date_scope":
        doc["response"][0]["fixture"]["date"] = "2024-10-03T19:00:00Z"
    elif case == "corrupt_index_team":
        engine = get_engine(database)
        with session_scope(engine) as session:
            session.add(Team(id=3, name="Unrelated existing team", aliases=[]))
            session.flush()
            session.get(ApifFixtureIndex, "102").home_team_id = 3
        engine.dispose()
    else:
        engine = get_engine(database)
        with session_scope(engine) as session:
            match = session.get(Match, "jc:1")
            session.add(
                Match(
                    match_id="jc:ambiguous",
                    source="jc",
                    league=match.league,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    kickoff_utc=match.kickoff_utc,
                )
            )
        engine.dispose()
    save(tmp_path, doc)
    before = evidence(database)
    assert refresh(database, tmp_path) == 0
    assert evidence(database) == before
    audit = read_audit(database.parent / "state/audit/stats_link_audit.csv")
    assert audit and all(row["outcome"] == "rejected" for row in audit)


@pytest.mark.parametrize("hours,allowed", [(-3, True), (3, True), (4, False)])
def test_refresh_respects_index_three_hour_window(database, tmp_path, hours, allowed):
    from datetime import timedelta

    doc = page()
    kickoff = instant(doc["response"][0]["fixture"]["date"]) + timedelta(hours=hours)
    doc["response"][0]["fixture"]["date"] = kickoff.isoformat()
    engine = get_engine(database)
    with session_scope(engine) as session:
        # Keep the Match candidate valid to independently exercise the incumbent index guard.
        session.get(Match, "jc:1").kickoff_utc = kickoff.isoformat()
    engine.dispose()
    save(tmp_path, doc)
    before = evidence(database)
    assert refresh(database, tmp_path) == int(allowed)
    if not allowed:
        assert evidence(database) == before


@pytest.mark.parametrize("captured", ["2024-10-02T16:00:00Z", "2024-10-02T17:00:00Z"])
def test_old_or_same_timestamp_conflicting_context_is_preserved(database, tmp_path, captured):
    before = evidence(database)
    save(tmp_path, captured=captured)
    assert refresh(database, tmp_path) == 0
    assert evidence(database) == before


@pytest.mark.parametrize(
    "captured,expected", [("2024-10-02T18:15:00Z", True), ("2024-10-02T19:05:00Z", False)]
)
def test_lineups_storage_uses_actual_capture_not_pregame_planning_time(
    database, tmp_path, captured, expected
):
    planned_at = instant("2024-10-02T18:15:00Z")
    assert planned_at < instant(initial()["response"][0]["fixture"]["date"])
    path = RawStore(tmp_path / "raw").save(
        "api-football",
        "lineups",
        (FIX / "t30/lineups.json").read_bytes(),
        "json",
        captured_at=instant(captured),
        url="https://v3.football.api-sports.io/fixtures/lineups?fixture=102",
        status_code=200,
    )
    result = ingest_apif(database, path, endpoint="lineups")
    assert result["inserted"] > 0 and result["rejected"] == 0
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            rows = list(session.scalars(select(MatchLineup)))
            assert rows and all(row.captured_before_kickoff is expected for row in rows)
            assert all(instant(row.captured_utc) == instant(captured) for row in rows)
    finally:
        engine.dispose()
