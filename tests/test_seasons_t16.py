"""T16 B1: a transition is distinct from both adjacent complete seasons."""

import csv
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from zucai.predict import backtest, seasons


@pytest.mark.parametrize("league", ["日职", "JPN"])
@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2025-12-31T23:59:59.999999999Z", "2025"),
        ("2026-01-01T00:00:00Z", "2026T"),
        ("2026-07-31T23:59:59.999999999Z", "2026T"),
        ("2026-08-01T00:00:00Z", "2627"),
        ("2027-07-31T23:59:59.999999999Z", "2627"),
        ("2027-08-01T00:00:00Z", "2728"),
        ("2026-08-01T00:00:00+08:00", "2026T"),
    ],
)
def test_jpn_classifies_each_side_of_all_three_rules(league, instant, expected):
    assert seasons.season_for(instant, league) == expected


@pytest.mark.parametrize("league", ["日职", "JPN"])
@pytest.mark.parametrize(
    ("label", "low", "high", "eligible"),
    [
        ("2025", "2025-01-01T00:00Z", "2026-01-01T00:00Z", True),
        ("2026T", "2026-01-01T00:00Z", "2026-08-01T00:00Z", False),
        ("2627", "2026-08-01T00:00Z", "2027-08-01T00:00Z", True),
        ("2728", "2027-08-01T00:00Z", "2028-08-01T00:00Z", True),
    ],
)
def test_jpn_bounds_and_complete_eligibility(league, label, low, high, eligible):
    assert seasons.season_bounds(label, league) == (pd.Timestamp(low), pd.Timestamp(high))
    assert seasons.season_is_complete_eligible(label, league) is eligible


@pytest.mark.parametrize("league", ["日职", "JPN"])
@pytest.mark.parametrize("label", ["2026", "2027", "2526", "2628", "2025T"])
def test_jpn_rejects_ambiguous_or_obsolete_labels(league, label):
    with pytest.raises(ValueError):
        seasons.season_bounds(label, league)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        ("2025-12-31T23:59:59Z", ["2023", "2024"]),
        ("2026-01-01T00:00Z", ["2024", "2025"]),
        ("2026-08-01T00:00Z", ["2024", "2025"]),
        ("2027-07-31T23:59:59Z", ["2024", "2025"]),
        ("2027-08-01T00:00Z", ["2025", "2627"]),
        ("2028-08-01T00:00Z", ["2627", "2728"]),
    ],
)
def test_latest_complete_seasons_never_counts_transition(as_of, expected):
    assert seasons.latest_complete_seasons(as_of, "日职") == expected
    assert seasons.latest_complete_seasons(as_of, "JPN") == expected


def test_recent_three_complete_seasons_support_tuning_across_transition():
    assert seasons.latest_complete_seasons("2027-08-01T00:00Z", "日职", count=3) == [
        "2024",
        "2025",
        "2627",
    ]


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_latest_complete_seasons_rejects_invalid_counts(count):
    with pytest.raises(ValueError):
        seasons.latest_complete_seasons("2027-08-01T00:00Z", "日职", count=count)


def test_parse_and_next_season_preserve_chronological_transition():
    assert seasons.parse_seasons("2627,2025,2026T", "日职") == ["2025", "2026T", "2627"]
    assert seasons.next_season("2025", "日职") == "2026T"
    assert seasons.next_season("2026T", "日职") == "2627"
    assert seasons.next_season("2627", "日职") == "2728"
    with pytest.raises(ValueError):
        seasons.parse_seasons("2026T,2026T", "日职")


def test_backtest_never_marks_transition_complete_after_its_end():
    kickoff = pd.Timestamp("2026-02-01T12:00Z")
    history = pd.DataFrame(
        [
            dict(
                match_id="fd:transition",
                source="fd",
                league="日职",
                home="A",
                away="B",
                kickoff_utc=kickoff,
                available_utc=pd.Timestamp("2028-01-01T00:00Z"),
                available_source="captured",
                home_goals=1,
                away_goals=0,
                counterpart_id=None,
            )
        ]
    )
    # The unavailable label avoids fitting; completeness is a separate time/rule contract.
    result = backtest.evaluate(history, {}, since="2026-01-01T00:00Z", as_of="2027-08-01T00:00Z")
    assert list(result["seasons"]) == ["2026T"]
    assert result["seasons"]["2026T"]["complete"] is False
    assert result["seasons"]["2026T"]["in_period"] == 1


def test_another_league_can_use_the_same_segment_configuration(monkeypatch):
    monkeypatch.setitem(
        seasons.LEAGUE_SEASON_RULES,
        "segmented-test",
        (
            seasons.SeasonRule(None, start_month=1),
            seasons.SeasonRule("2026-01-01T00:00Z", label="2026T", complete_eligible=False),
            seasons.SeasonRule("2026-09-01T00:00Z", start_month=9),
        ),
    )
    assert seasons.season_for("2026-08-31T23:59:59Z", "segmented-test") == "2026T"
    assert seasons.season_bounds("2026T", "segmented-test") == (
        pd.Timestamp("2026-01-01T00:00Z"),
        pd.Timestamp("2026-09-01T00:00Z"),
    )
    assert seasons.season_bounds("2627", "segmented-test") == (
        pd.Timestamp("2026-09-01T00:00Z"),
        pd.Timestamp("2027-09-01T00:00Z"),
    )
    assert seasons.latest_complete_seasons("2027-09-01T00:00Z", "segmented-test") == [
        "2025",
        "2627",
    ]


@pytest.mark.parametrize(
    ("league", "instant", "label", "low", "high"),
    [
        ("瑞超", "2026-08-01T00:00Z", "2026", "2026-01-01T00:00Z", "2027-01-01T00:00Z"),
        ("SWE", "2026-08-01T00:00Z", "2026", "2026-01-01T00:00Z", "2027-01-01T00:00Z"),
        ("英超", "2026-07-01T00:00Z", "2627", "2026-07-01T00:00Z", "2027-07-01T00:00Z"),
    ],
)
def test_other_leagues_keep_existing_boundaries(league, instant, label, low, high):
    assert seasons.season_for(instant, league) == label
    assert seasons.season_bounds(label, league) == (pd.Timestamp(low), pd.Timestamp(high))
    assert seasons.season_is_complete_eligible(label, league) is True


def test_real_jpn_sample_agrees_with_labels_and_keeps_source_provenance():
    root = Path(__file__).resolve().parents[1] / "data/fixtures/t16"
    raw = (root / "jpn_season_boundaries.csv").read_bytes()
    provenance = json.loads((root / "provenance.json").read_text())["jpn_season_boundaries.csv"]
    assert hashlib.sha256(raw).hexdigest() == provenance["fixture_sha256"]
    rows = list(csv.DictReader(raw.decode().splitlines()))
    assert len(rows) == provenance["data_rows"] == 8
    assert provenance["source_2026_before_august_rows"] == 0
    for row in rows:
        stamp = pd.to_datetime(f"{row['Date']} {row['Time']}", format="%d/%m/%Y %H:%M")
        kickoff = stamp.tz_localize("Europe/London").tz_convert("UTC")
        expected = {"2025": "2025", "2026/2027": "2627"}[row["Season"]]
        assert seasons.season_for(kickoff, "日职") == expected
