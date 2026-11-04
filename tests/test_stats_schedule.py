"""Pure scheduling checks use only the small T32 runtime fixture."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from zucai.stats_schedule import (
    coverage,
    due,
    empty_cursor,
    fd_units,
    index_units,
    injury_units,
    plan,
    record,
    resume_units,
    scope_key,
    unit,
    unsupported,
)

NOW = datetime.fromisoformat(
    json.loads((Path(__file__).parents[1] / "data/fixtures/t32/runtime.json").read_text())["now"]
)


def test_scope_order_and_three_entry_paths():
    selection = plan("39", "2022,2024", "players,fd,index,statistics,events,lineups", NOW)
    assert selection["endpoints"] == ["index", "fd", "statistics", "events", "lineups", "players"]
    assert [(r["league_id"], r["season"]) for r in index_units(selection)] == [
        (39, 2022),
        (39, 2024),
    ]
    assert [(r["div"], r["fd_season"]) for r in fd_units(selection)] == [
        ("E0", "2223"),
        ("E0", "2425"),
    ]
    assert len(list(fd_units(plan("E0,E1", "2024", "fd", NOW)))) == 2
    assert len(list(index_units(plan(None, None, "index", NOW)))) == 40


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(codes="1"),
        dict(seasons="2021"),
        dict(seasons="2024,2024"),
        dict(endpoints="odds"),
        dict(codes="39,39"),
    ],
)
def test_invalid_plan_refused(kwargs):
    values = dict(codes="39", seasons="2024", endpoints="statistics")
    values.update(kwargs)
    with pytest.raises(ValueError):
        plan(**values, now=NOW)


def test_empty_at_most_two_retries_seven_days_apart_and_success_skips():
    cursor = empty_cursor()
    item = unit("statistics", 39, 2024, "102", league="英超")
    assert due(item, cursor, NOW)
    for i in range(3):
        at = NOW + timedelta(days=7 * i)
        assert due(item, cursor, at)
        record(cursor, item, "empty", at, requested=True, received=True)
        assert not due(item, cursor, at + timedelta(days=6, hours=23))
    assert not due(item, cursor, NOW + timedelta(days=100))
    assert cursor["records"][item["key"]]["attempts"] == 3
    record(cursor, item, "ok", NOW, received=True)
    assert not due(item, cursor, NOW + timedelta(days=100))


def test_unsupported_scope_and_qualification_only():
    cursor = empty_cursor()
    item = unit("players", 3, 2026, "102", league="欧罗巴")
    assert unsupported(item, cursor, {"league": {"round": "1st Qualifying Round"}})
    assert unsupported(item, cursor, {"league": {"round": "League Stage - 1"}}) is None
    cursor["unsupported"][scope_key(item)] = "权限错误待人工复核"
    assert unsupported(item, cursor) == "权限错误待人工复核"
    items = list(injury_units(plan(None, "2024", "injuries", NOW)))
    assert sum(unsupported(r, empty_cursor()) is None for r in items) == 11


def test_coverage_preserves_missing_and_zero_and_failed_not_valid():
    cursor = empty_cursor()
    for i, status in enumerate(("ok", "empty", "error", "unsupported", "pending")):
        item = unit("statistics", 39, 2024, str(i + 1), league="英超")
        record(
            cursor,
            item,
            status,
            NOW,
            requested=i < 3,
            received=i < 3,
            null_fields=2 if status == "empty" else 0,
        )
    row = coverage(cursor, NOW)["rows"][0]
    assert [
        row[key] for key in ("expected", "fetched", "valid", "null", "failed", "unsupported")
    ] == [5, 3, 1, 1, 1, 1]
    assert row["requests"] == 3 and row["null_fields"] == 2


def test_resume_prioritizes_unfinished_within_endpoint_without_erasing_receipts():
    cursor = empty_cursor()
    items = [
        (unit(endpoint, 39, 2024, fixture, league="英超"), None)
        for endpoint in ("statistics", "events")
        for fixture in ("101", "102", "103")
    ]
    for item, _ in items:
        if item["fixture_id"] == "102":
            record(cursor, item, "pending", NOW)
        if item["fixture_id"] == "103":
            record(cursor, item, "ok", NOW, requested=True, received=True)
    before = json.dumps(cursor, sort_keys=True)
    resumed = list(resume_units(items, cursor, True))
    assert [(r["endpoint"], r["fixture_id"]) for r, _ in resumed] == [
        (e, f) for e in ("statistics", "events") for f in ("102", "101", "103")
    ]
    assert list(resume_units(items, cursor, False)) == items
    assert json.dumps(cursor, sort_keys=True) == before


def test_permission_wildcard_blocks_all_endpoints_only_in_its_league_season():
    cursor = empty_cursor()
    cursor["unsupported"]["39:2024:*"] = "整联赛季权限错误待人工复核"
    for endpoint in ("statistics", "events", "lineups", "players", "index", "injuries"):
        item = unit(endpoint, 39, 2024, "102", league="英超")
        assert unsupported(item, cursor) == "整联赛季权限错误待人工复核"
    assert unsupported(unit("statistics", 140, 2024, "103", league="西甲"), cursor) is None
    assert unsupported(unit("statistics", 39, 2025, "104", league="英超"), cursor) is None


@pytest.fixture
def daily_database(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import ApifFixtureIndex, Match, Odds, Team

    def forbid_network(*args, **kwargs):
        raise AssertionError("daily scheduling must use offline state only")

    monkeypatch.setattr("requests.sessions.Session.request", forbid_network)
    fixture = json.loads(
        (Path(__file__).parents[1] / "data/fixtures/t32/daily_context.json").read_text()
    )
    assert fixture["synthetic"]
    now = datetime.fromisoformat(fixture["now"])
    path = tmp_path / "daily.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(id=1, name="Synthetic Home"), Team(id=2, name="Synthetic Away")])
        session.flush()
        for case in fixture["schedule"]:
            sale = case.get("sale")
            source = "jc" if sale == "direct" else "fd"
            match_id = source + ":" + case["id"]
            jc_id = "jc:sale:" + case["id"]
            game = Match(
                match_id=match_id,
                source=source,
                league=case.get("league", "英超"),
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=case["kickoff"],
                status=case["status"],
                counterpart_id=jc_id if sale == "game_to_jc" else None,
            )
            session.add(game)
            if sale in {"game_to_jc", "jc_to_game"}:
                session.add(
                    Match(
                        match_id=jc_id,
                        source="jc",
                        league="英超",
                        home_team_id=1,
                        away_team_id=2,
                        kickoff_utc=case["kickoff"],
                        status=case["status"],
                        counterpart_id=match_id if sale == "jc_to_game" else None,
                    )
                )
            session.flush()
            if sale:
                session.add(
                    Odds(
                        match_id=jc_id if sale in {"game_to_jc", "jc_to_game"} else match_id,
                        pool="had",
                        line=0,
                        selection="h",
                        price=2,
                        snapshot_utc=now.isoformat(),
                    )
                )
            if case.get("indexed", True):
                status = "FT" if case["status"] == "finished" else "NS"
                context = dict(
                    fixture=dict(
                        id=int(case["id"]), date=case["kickoff"], status=dict(short=status)
                    ),
                    league=dict(id=case.get("league_id", 39), season=case.get("season", 2026)),
                    teams=dict(home=dict(id=1), away=dict(id=2)),
                )
                session.add(
                    ApifFixtureIndex(
                        provider_fixture_id=case["id"],
                        match_id=match_id,
                        league=case.get("league", "英超"),
                        provider_league_id=case.get("league_id", 39),
                        season=case.get("season", 2026),
                        kickoff_utc=case["kickoff"],
                        home_provider_id=1,
                        away_provider_id=2,
                        home_team_id=1,
                        away_team_id=2,
                        captured_utc=now.isoformat(),
                        raw_sha256="a" * 64,
                        raw_location="fixture://daily_context",
                        parser_version="synthetic",
                        evidence=dict(fixture=context),
                    )
                )
    engine.dispose()
    return path, now


def test_daily_only_previous_beijing_day_finished_linked_games_and_explicit_sale_links(
    daily_database,
):
    from zucai.stats_schedule import match_units

    database, now = daily_database
    selection = plan("39", "2026", "statistics,events,lineups,players", now)
    rows = list(match_units(database, selection, now, daily=True))
    historical = [row for row, context in rows if row["phase"] == "history"]
    assert len(historical) == 8
    assert {row["fixture_id"] for row in historical} == {"201", "202"}
    assert {row["endpoint"] for row in historical} == {"statistics", "events", "lineups", "players"}
    pregame = [row for row, context in rows if row["phase"] == "pregame"]
    assert {row["fixture_id"] for row in pregame if not row.get("missed")} == {"301", "302", "303"}
    assert {row["fixture_id"] for row in pregame if row.get("missed")} == {"305", "306", "310"}
    assert all(row["endpoint"] == "lineups" for row in pregame)
    assert all(isinstance(row["kickoff_utc"], str) for row in pregame)
    assert {row["fixture_id"]: datetime.fromisoformat(row["kickoff_utc"]) for row in pregame} == {
        "301": datetime.fromisoformat("2026-11-04T01:00:00Z"),
        "302": datetime.fromisoformat("2026-11-04T01:15:00Z"),
        "303": datetime.fromisoformat("2026-11-04T00:45:00Z"),
        "305": datetime.fromisoformat("2026-11-04T00:44:00Z"),
        "306": datetime.fromisoformat("2026-11-04T00:10:00Z"),
        "310": datetime.fromisoformat("2026-11-02T16:00:00Z"),
    }
    assert all(context["fixture"]["id"] == int(row["fixture_id"]) for row, context in rows)


def test_missed_window_includes_previous_beijing_midnight_and_excludes_older(daily_database):
    from zucai.stats_schedule import match_units

    database, now = daily_database
    rows = list(match_units(database, plan("39", "2026", "lineups", now), now, daily=True))
    missed = {row["fixture_id"] for row, context in rows if row.get("missed")}
    assert "310" in missed  # Previous Beijing midnight is more than 24 hours ago.
    assert "311" not in missed  # One second before that midnight belongs to the older day.
    assert "304" not in missed  # A future window more than 60 minutes away is still pending.


@pytest.mark.parametrize("outcome", ["ok", "empty", "error"])
def test_pregame_attempt_is_once_even_when_empty_or_error(outcome):
    cursor = empty_cursor()
    item = unit("lineups", 39, 2026, "102", league="英超", phase="pregame")
    assert due(item, cursor, NOW)
    record(cursor, item, outcome, NOW, requested=True, received=outcome != "error")
    assert not due(item, cursor, NOW + timedelta(minutes=1))
    assert not due(item, cursor, NOW + timedelta(days=7))
    assert cursor["records"][item["key"]]["attempts"] == 1


def test_pregame_unrequested_preflight_error_can_retry_until_window_missed(daily_database):
    from zucai.stats_schedule import match_units

    database, now = daily_database
    selection = plan("39", "2026", "lineups", now)
    rows = list(match_units(database, selection, now, daily=True))
    missed = next(row for row, context in rows if row.get("missed"))
    cursor = empty_cursor()
    record(cursor, missed, "error", now, requested=False, reason="no request was made")
    assert due(missed, cursor, now)
    record(cursor, missed, "missed", now, reason="missed 60–30 minute window")
    assert not due(missed, cursor, now + timedelta(minutes=1))
    entry = cursor["records"][missed["key"]]
    assert entry["attempts"] == 0 and not entry["received"]
    assert entry["outcome"] == "missed" and entry["reason"]
    assert coverage(cursor, now)["rows"][0]["missed"] == 1


def test_started_jpn_provider_season_is_scoped_without_future_other_requests(daily_database):
    from zucai.stats_schedule import match_units

    database, now = daily_database
    default = plan(None, None, "index,fd,statistics", now)
    assert default["seasons"] == [2022, 2023, 2024, 2025, 2026]
    assert default["league_seasons"] == {"98": [2022, 2023, 2024, 2025, 2026, 2027]}
    assert len(list(index_units(default))) == 40
    assert len(list(fd_units(default))) == 45
    assert all(row["season"] <= 2026 for row in index_units(default))
    assert all(row["season"] <= 2026 for row in fd_units(default))
    assert "701" in {row["fixture_id"] for row, context in match_units(database, default, now)}
    assert "701" in {
        row["fixture_id"] for row, context in match_units(database, default, now, daily=True)
    }
    explicit = plan("98", "2026", "statistics", now)
    assert "league_seasons" not in explicit
    assert not list(match_units(database, explicit, now))
    before_transition = datetime.fromisoformat("2026-07-31T12:00:00Z")
    assert "league_seasons" not in plan(None, None, "statistics", before_transition)


def test_daily_injuries_use_each_leagues_current_provider_season_only():
    now = datetime.fromisoformat("2027-01-05T00:00:00Z")
    selection = plan("39,113", "2022-2026", "injuries", now)
    assert [(row["league_id"], row["season"]) for row in injury_units(selection, now=now)] == [
        (39, 2026),
        (113, 2027),
    ]
    assert len(list(injury_units(selection))) == 10
    all_daily = list(injury_units(plan(None, None, "injuries", now), now=now))
    assert sum(unsupported(row, empty_cursor()) is None for row in all_daily) == 11
    assert all(
        row["season"] == (2027 if row["league_id"] == 113 else 2026)
        for row in injury_units(selection, now=now)
    )
