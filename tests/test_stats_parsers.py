"""Offline parsing: actual field shapes, missingness, period and capture clocks."""

import json
from pathlib import Path

import pytest

from zucai.sources.apif_stats import STAT_TYPES, parse_endpoint
from zucai.sources.football_data import FootballDataCoUk

FIXTURES = Path(__file__).parents[1] / "data/fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def parse(endpoint, *, raw=None, fixture=None, captured="2024-10-02T18:59:59Z"):
    return parse_endpoint(
        raw or load(f"t30/{endpoint}.json"),
        endpoint=endpoint,
        fixture=fixture or load("t31/fixture.json"),
        captured_utc=captured,
    )


def test_statistics_raw_numeric_percent_zero_and_null():
    rows = {(r["side"], r["stat_key"]): r for r in parse("statistics")}
    assert rows["home", "possession"]["raw_value"] == "53%"
    assert rows["home", "possession"]["value"] == 53.0
    assert rows["home", "possession"]["unit"] == "%"
    assert rows["home", "expected_goals"]["value"] == 1.89
    assert rows["away", "expected_goals"]["value"] == 0
    assert rows["away", "expected_goals"]["missing_reason"] is None
    assert rows["home", "saves"]["value"] is None
    assert rows["home", "saves"]["missing_reason"] == "null"
    assert rows["home", "shots"]["missing_reason"] == "not_provided"
    assert len(STAT_TYPES) == 19
    assert not any("half" in row["stat_key"] for row in rows.values())


@pytest.mark.parametrize(
    "status,basis,extra",
    [("FT", "90", False), ("AET", "incl_extra_time", True), ("PEN", "incl_extra_time", True)],
)
def test_period_is_fixture_status_not_inferred_from_statistics(status, basis, extra):
    fixture = load("t31/fixture.json")
    fixture["fixture"]["status"]["short"] = status
    for row in parse("statistics", fixture=fixture):
        assert row["period_basis"] == basis
        assert row["extra_time_included"] is extra
        assert row["eligible_90"] is (not extra)


def test_empty_statistics_and_pre2024_xg():
    fixture = load("t31/fixture.json")
    fixture["league"]["season"] = 2022
    raw = load("t30/statistics.json")
    raw.update(response=[], results=0)
    rows = parse("statistics", raw=raw, fixture=fixture)
    assert len(rows) == 36  # 16 common types plus xG / goals_prevented, two known teams.
    assert all(row["value"] is None for row in rows)
    assert {r["missing_reason"] for r in rows if r["stat_key"] == "expected_goals"} == {
        "not_provided_before_2024"
    }


@pytest.mark.parametrize("endpoint", ["statistics", "events", "lineups", "players"])
def test_errors_and_wrong_fixture_never_become_data(endpoint):
    raw = load(f"t30/{endpoint}.json")
    raw["errors"] = {"plan": "not allowed"}
    with pytest.raises(ValueError, match="errors"):
        parse(endpoint, raw=raw)
    raw["errors"] = []
    raw["parameters"]["fixture"] = "999"
    with pytest.raises(ValueError, match="fixture"):
        parse(endpoint, raw=raw)


def test_events_preserve_actor_and_added_time():
    raw = load("t30/events.json")
    raw["response"][0]["time"].update(elapsed=90, extra=3)
    event = parse("events", raw=raw)[0]
    assert event["elapsed"] == 90 and event["extra"] == 3
    assert event["value"]["total_minute"] == 93
    assert event["value"]["player"]["name"] == "Synthetic Player"
    assert event["raw_value"] == raw["response"][0]


@pytest.mark.parametrize(
    "captured,before",
    [
        ("2024-10-02T18:59:59Z", True),
        ("2024-10-02T19:00:00Z", False),
        ("2026-11-04T00:00:00Z", False),
    ],
)
def test_lineups_capture_clock_and_players_normalization(captured, before):
    lineup = parse("lineups", captured=captured)
    assert all(row["captured_before_kickoff"] is before for row in lineup)
    assert lineup[0]["formation"] == "4-4-2"
    assert lineup[1]["player_id"] == 10
    assert lineup[1]["role"] == "start"
    player = parse("players", captured=captured)[0]
    assert player["minutes"] == 90 and player["rating"] == 7.2 and player["position"] == "F"
    assert player["raw_value"]["statistics"][0]["games"]["rating"] == "7.2"
    assert player["value"]["statistics"][0]["games"]["rating"] == 7.2


@pytest.mark.parametrize("file,reason", [("fd.csv", "empty"), ("fd_missing.csv", "column_absent")])
def test_fd_columns_and_missing_are_not_goals_or_corners_guesses(file, reason):
    adapter = FootballDataCoUk(leagues=load("t31/fd_leagues.json"), div_code="E0", season="2425")
    games = adapter.parse_statistics((FIXTURES / "t31" / file).read_bytes())
    assert not adapter.rejected and len(games) == 1
    rows = {(r["side"], r["stat_key"]): r for r in games[0].statistics}
    assert rows["away", "shots"]["value"] == 0
    assert rows["away", "shots_on_target"]["value"] is None
    assert rows["away", "shots_on_target"]["missing_reason"] == reason
    assert len(rows) == 16
    if file == "fd.csv":
        assert rows["home", "free_kicks_conceded"]["value"] == 13


def test_fd_country_file_not_supported():
    adapter = FootballDataCoUk(leagues=load("t31/fd_leagues.json"), div_code="JPN", season=None)
    with pytest.raises(ValueError, match="主联赛"):
        adapter.parse_statistics((FIXTURES / "t31/fd.csv").read_bytes())


def test_full_statistics_dictionary_and_optional_free_kicks():
    raw = load("t31/statistics_full.json")
    rows = {(r["side"], r["stat_key"]): r for r in parse("statistics", raw=raw)}
    assert len(rows) == 38
    assert rows["home", "pass_accuracy"]["value"] == 85.0
    assert rows["home", "pass_accuracy"]["unit"] == "%"
    assert rows["home", "goals_prevented"]["value"] == -0.96
    assert rows["home", "free_kicks"]["value"] == 4


def test_bad_team_row_is_rejected_while_valid_other_team_is_kept():
    raw = load("t30/statistics.json")
    raw["response"][0]["statistics"].append(raw["response"][0]["statistics"][0])
    rejected = []
    rows = parse_endpoint(
        raw,
        endpoint="statistics",
        fixture=load("t31/fixture.json"),
        captured_utc="2026-11-04T00:00:00Z",
        rejected=rejected,
    )
    assert len(rejected) == 1 and rejected[0]["row_number"] == 1
    assert {r["side"] for r in rows} == {"away"}
