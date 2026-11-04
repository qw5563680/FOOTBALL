import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, MatchStat, Result, Team
from zucai.predict.base import utc

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t33/timeline.json"


@pytest.fixture
def technical_database(tmp_path):
    engine = get_engine(tmp_path / "technical.db")
    init_db(engine)
    doc = json.loads(FIXTURE.read_bytes())
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in doc["teams"]}
        session.add_all(teams.values())
        session.flush()
        for game in doc["matches"]:
            kickoff = utc(game["kickoff"])
            session.add(
                Match(
                    match_id=game["id"],
                    source="fd",
                    league="英超",
                    home_team_id=teams[game["home"]].id,
                    away_team_id=teams[game["away"]].id,
                    kickoff_utc=kickoff,
                    status=game["status"],
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=game["id"],
                    home_goals=1,
                    away_goals=0,
                    available_utc=kickoff + timedelta(hours=3),
                    available_source="assumed",
                )
            )
            for side, value in zip(("home", "away"), game["stats"], strict=True):
                session.add(
                    MatchStat(
                        provider="fd",
                        provider_fixture_id=game["id"],
                        endpoint="csv",
                        record_key=side + ":shots_on_target",
                        match_id=game["id"],
                        team_id=teams[game[side]].id,
                        provider_team_id=None,
                        side=side,
                        period_basis="90",
                        extra_time_included=False,
                        eligible_90=True,
                        captured_utc="2026-11-04T12:00:00Z",
                        available_utc=None,
                        availability_basis="backfilled",
                        raw_sha256="a" * 64,
                        raw_location="fixture",
                        parser_version="test",
                        missing_reason=None,
                        version=1,
                        stat_key="shots_on_target",
                        raw_value=value,
                        value=value,
                        unit="count",
                    )
                )
    yield engine
    engine.dispose()


def builder(engine):
    from zucai.features.stats import StatsFeatures

    with session_scope(engine) as session:
        return StatsFeatures(session, provider="fd")


def test_strict_excludes_backfill_but_replay_marks_assumption(technical_database):
    features = builder(technical_database)
    target = features.matches["fd:6"]
    strict = features.build(target, as_of=target["kickoff_utc"], mode="strict")
    replay = features.build(target, as_of=target["kickoff_utc"], mode="backfill_replay")
    key = "home_all_5_shots_on_target_for"
    assert strict["cells"][key]["value"] is None
    assert "backfilled_not_known" in strict["cells"][key]["missing_reasons"]
    cell = replay["cells"][key]
    assert cell["n"] == cell["N"] == 5
    assert cell["value"] == pytest.approx(4)
    assert cell["backfilled_n"] == 5
    assert set(cell["match_ids"]) == {f"fd:{i}" for i in range(1, 6)}
    assert replay["validation_status"] == "historical_replay_unvalidated"


def test_current_future_and_boundary_stats_never_enter_window(technical_database):
    features = builder(technical_database)
    target = features.matches["fd:6"]
    row = features.build(target, as_of=target["kickoff_utc"], mode="backfill_replay")
    cell = row["cells"]["home_all_5_shots_on_target_for"]
    weights = np.exp(-np.log(2) * np.arange(35, 0, -7) / 90)
    assert cell["decay"] == pytest.approx(np.dot([2, 0, 6, 8, 4], weights) / weights.sum())
    assert cell["match_ids"].count("fd:5") == 1  # two team perspectives are not two matches
    boundary = utc(features.matches["fd:5"]["kickoff_utc"]) + timedelta(hours=3)
    early = features.build(target, as_of=boundary, mode="backfill_replay")
    assert "fd:5" not in early["cells"]["home_all_5_shots_on_target_for"]["match_ids"]
    assert early["cells"]["home_all_5_shots_on_target_for"]["value"] is None


def test_null_is_not_zero_or_replaced_by_older_match(technical_database):
    with session_scope(technical_database) as session:
        stat = session.scalar(
            select(MatchStat).where(MatchStat.match_id == "fd:5", MatchStat.side == "home")
        )
        stat.value, stat.missing_reason = None, "empty"
    features = builder(technical_database)
    row = features.build(
        features.matches["fd:6"], as_of="2024-02-05T12:00:00Z", mode="backfill_replay"
    )
    cell = row["cells"]["home_all_5_shots_on_target_for"]
    assert cell["n"] == 4 and cell["value"] is None and cell["decay"] is None
    assert "empty" in cell["missing_reasons"]
    assert "fd:5" in cell["match_ids"]
    assert any(e["value"] == 0 for e in cell["evidence"])  # zero is retained, not missing


def test_future_captured_correction_not_used_by_either_mode(technical_database):
    with session_scope(technical_database) as session:
        old = session.scalar(
            select(MatchStat).where(MatchStat.match_id == "fd:1", MatchStat.side == "home")
        )
        values = {
            column.name: getattr(old, column.name)
            for column in MatchStat.__table__.columns
            if column.name != "id"
        }
        values.update(
            version=2,
            value=999,
            raw_value=999,
            raw_sha256="b" * 64,
            captured_utc="2024-03-01T12:00:00Z",
            available_utc="2024-03-01T12:00:00Z",
            availability_basis="captured",
        )
        session.add(MatchStat(**values))
    features = builder(technical_database)
    target = features.matches["fd:6"]
    for mode in ("strict", "backfill_replay"):
        row = features.build(target, as_of=target["kickoff_utc"], mode=mode)
        cell = row["cells"]["home_all_5_shots_on_target_for"]
        assert all(e.get("value") != 999 for e in cell["evidence"])
        assert (cell["value"] is None) == (mode == "strict")


def test_extra_time_is_not_a_90_minute_input(technical_database):
    with session_scope(technical_database) as session:
        stat = session.scalar(
            select(MatchStat).where(MatchStat.match_id == "fd:5", MatchStat.side == "home")
        )
        stat.period_basis, stat.extra_time_included, stat.eligible_90 = (
            "incl_extra_time",
            True,
            False,
        )
    features = builder(technical_database)
    row = features.build(
        features.matches["fd:6"], as_of="2024-02-05T12:00:00Z", mode="backfill_replay"
    )
    assert "not_90_minutes" in row["cells"]["home_all_5_shots_on_target_for"]["missing_reasons"]


def test_captured_values_and_provider_venue_windows_stay_separate(technical_database):
    from zucai.features.stats import StatsFeatures

    with session_scope(technical_database) as session:
        for stat in session.scalars(select(MatchStat)):
            game = session.get(Match, stat.match_id)
            stamp = utc(game.kickoff_utc) + timedelta(hours=4)
            stat.availability_basis = "captured"
            stat.captured_utc = stamp
            stat.available_utc = stamp
    features = builder(technical_database)
    target = features.matches["fd:6"]
    result = features.build(target, as_of=target["kickoff_utc"])
    assert result["cells"]["home_all_5_shots_on_target_for"]["value"] == 4
    assert result["cells"]["home_all_5_shots_on_target_for"]["backfilled_n"] == 0
    assert result["cells"]["home_home_5_shots_on_target_for"]["sample_n"] == 3
    assert result["cells"]["home_all_10_shots_on_target_for"]["value"] is None
    assert result["rest"]["home"]["value"] == 7
    assert result["opponent_strength"]["home"]["missing_reason"]
    with session_scope(technical_database) as session:
        apif = StatsFeatures(session, provider="apif")
    separate = apif.build(target, as_of=target["kickoff_utc"])
    assert separate["cells"]["home_all_5_shots_on_target_for"]["n"] == 0
    with pytest.raises(ValueError, match="赔率"):
        features.build(target, as_of=target["kickoff_utc"], stat_keys=("market",))
