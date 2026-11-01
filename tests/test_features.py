import json
import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team

KICKOFF = datetime(2025, 2, 1, 12, tzinfo=UTC)
CUTOFF = KICKOFF - timedelta(hours=2)


@pytest.fixture
def feature_db(tmp_path):
    engine = get_engine(tmp_path / "db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(name=name) for name in ("A", "B", "X", "Y")])
        session.flush()
        teams = {t.name: t.id for t in session.query(Team).all()}
        session.add(
            Match(
                match_id="jc:target",
                source="jc",
                league="L",
                home_team_id=teams["A"],
                away_team_id=teams["B"],
                kickoff_utc=KICKOFF,
                status="scheduled",
            )
        )
    yield engine, teams
    engine.dispose()


def game(
    session,
    teams,
    key,
    days,
    home="A",
    away="X",
    goals=(1, 0),
    *,
    source="jc",
    league="L",
    basis="captured",
    available=None,
    status="finished",
):
    kickoff = KICKOFF - timedelta(days=days)
    ident = f"{source}:{key}"
    session.add(
        Match(
            match_id=ident,
            source=source,
            league=league,
            home_team_id=teams[home],
            away_team_id=teams[away],
            kickoff_utc=kickoff,
            status=status,
        )
    )
    session.flush()
    session.add(
        Result(
            match_id=ident,
            home_goals=goals[0],
            away_goals=goals[1],
            available_utc=available if available is not None else kickoff + timedelta(hours=3),
            available_source=basis,
        )
    )
    session.flush()
    return ident


def build(session, ids=None, **kwargs):
    from zucai.features.build import build_features

    return build_features(session, ["jc:target"] if ids is None else ids, as_of=CUTOFF, **kwargs)


@pytest.mark.parametrize("correction_timing", ["between_reads", "cached_before_call"])
def test_public_builder_does_not_mix_match_and_score_versions(feature_db, correction_timing):
    engine, teams = feature_db
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
    with session_scope(engine) as session:
        game(session, teams, "corrected", 5, goals=(2, 0))

    corrected = False

    def correct_identity_and_score():
        nonlocal corrected
        with engine.begin() as writer:
            writer.execute(
                update(Match)
                .where(Match.match_id == "jc:corrected")
                .values(home_team_id=teams["X"], away_team_id=teams["A"])
            )
            writer.execute(
                update(Result)
                .where(Result.match_id == "jc:corrected")
                .values(
                    home_goals=0,
                    away_goals=2,
                    available_utc=KICKOFF - timedelta(days=4),
                    available_source="published",
                )
            )
        corrected = True

    def interleave(_conn, _cursor, statement, _parameters, _context, _executemany):
        if not corrected and "FROM teams" in statement:
            correct_identity_and_score()

    with Session(engine) as reader:
        if correction_timing == "cached_before_call":
            cached = reader.get(Match, "jc:corrected")
            correct_identity_and_score()
            assert cached.home_team_id == teams["A"]  # Keep the stale ORM object alive.
        else:
            event.listen(engine, "before_cursor_execute", interleave)
        try:
            frame = build(reader)
        finally:
            if correction_timing == "between_reads":
                event.remove(engine, "before_cursor_execute", interleave)
    assert corrected
    # A scored two goals in BOTH committed versions. Mixing identity/score gives zero.
    assert frame.iloc[0]["home_last5_goals_for_mean"] == 2, frame.attrs["excluded_history_counts"]
    used = frame.attrs["audit"]["jc:target"]["windows"]["home_last5"][0]
    assert used["goals_for"] == 2
    assert used["venue"] == ("home" if correction_timing == "between_reads" else "away")


def test_last5_last10_means_and_decay_are_hand_checkable(feature_db):
    engine, teams = feature_db
    with session_scope(engine) as s:
        for i in range(1, 7):
            game(s, teams, f"a{i}", i, goals=(i, 0))
            game(s, teams, f"b{i}", i, home="X", away="B", goals=(2, i - 1))
        frame = build(s)
    row = frame.iloc[0]
    assert row["home_last5_goals_for_mean"] == 3
    assert row["home_last10_goals_for_mean"] == 3.5
    assert row["home_last5_goals_against_mean"] == 0
    assert row["home_last5_points_mean"] == 3
    assert row["away_last5_goals_for_mean"] == 2
    assert row["away_last5_points_mean"] == 1.4
    weights = [2 ** (-(i - 1) / 90) for i in range(1, 6)]
    expected = sum(i * w for i, w in zip(range(1, 6), weights, strict=True)) / sum(weights)
    assert row["home_last5_goals_for_decay90"] == pytest.approx(expected)
    assert row["meta_home_last5_n"] == 5 and row["meta_home_last10_n"] == 6
    used = frame.attrs["audit"]["jc:target"]["windows"]["home_last5"]
    assert [r["match_id"] for r in used] == [f"jc:a{i}" for i in range(1, 6)]


def test_venue_scope_source_scope_and_cross_competition_rest(feature_db):
    engine, teams = feature_db
    with session_scope(engine) as s:
        for i in range(2, 7):
            game(s, teams, f"home{i}", i, goals=(2, 0))
        game(s, teams, "away", 1.5, home="X", away="A", goals=(0, 10))
        game(s, teams, "cup", 1, league="Cup", goals=(100, 0))
        game(s, teams, "other-provider", 0.5, source="c500", goals=(900, 0))
        game(s, teams, "b-home", 2, home="B", goals=(10, 0))
        game(s, teams, "b-away", 3, home="X", away="B", goals=(0, 4))
        frame = build(s)
    row = frame.iloc[0]
    assert row["home_last5_goals_for_mean"] == 3.6
    assert row["home_home5_goals_for_mean"] == 2
    assert row["away_away5_goals_for_mean"] == 4
    assert row["away_last5_goals_for_mean"] == 7
    assert row["home_rest_days"] == 1
    assert frame.attrs["audit"]["jc:target"]["windows"]["home_rest"][0]["match_id"] == "jc:cup"


def test_head_to_head_uses_target_home_perspective_and_only_latest_three(feature_db):
    engine, teams = feature_db
    with session_scope(engine) as s:
        game(s, teams, "h1", 1, home="B", away="A", goals=(2, 1))
        game(s, teams, "h2", 2, home="A", away="B", goals=(2, 2))
        game(s, teams, "h3", 3, home="B", away="A", goals=(0, 3))
        game(s, teams, "h4", 4, home="A", away="B", goals=(99, 0))
        frame = build(s)
    row = frame.iloc[0]
    assert row["h2h_last3_home_win_rate"] == pytest.approx(1 / 3)
    assert row["h2h_last3_draw_rate"] == pytest.approx(1 / 3)
    assert row["h2h_last3_away_win_rate"] == pytest.approx(1 / 3)
    assert row["h2h_last3_home_goals_mean"] == 2
    assert row["h2h_last3_away_goals_mean"] == pytest.approx(4 / 3)
    assert json.loads(row["meta_h2h_outcomes"]) == ["0", "1", "3"]
    assert row["meta_h2h_last3_n"] == 3


def test_future_target_and_late_known_results_do_not_change_model_features(feature_db):
    from zucai.features.build import MODEL_FEATURE_COLUMNS

    engine, teams = feature_db
    with session_scope(engine) as s:
        game(s, teams, "known", 7, goals=(2, 1))
        before = build(s)
        game(s, teams, "old-but-late", 20, goals=(90, 0), available=CUTOFF + timedelta(seconds=1))
        game(s, teams, "known-at-cutoff", 3, goals=(80, 0), available=CUTOFF)
        game(s, teams, "future", -1, goals=(70, 0))
        game(s, teams, "not-finished", 10, goals=(60, 0), status="scheduled")
        s.get(Match, "jc:target").status = "finished"
        s.add(
            Result(
                match_id="jc:target",
                home_goals=50,
                away_goals=0,
                available_utc=KICKOFF + timedelta(hours=3),
                available_source="captured",
            )
        )
        s.flush()
        after = build(s)
    pd.testing.assert_frame_equal(
        before[list(MODEL_FEATURE_COLUMNS)], after[list(MODEL_FEATURE_COLUMNS)]
    )
    assert after.iloc[0]["meta_home_last10_n"] == 1


@pytest.mark.parametrize("invalid_basis", ["unknown", ""])
def test_missing_and_assumed_availability_is_explicit_and_strict_mode_excludes(
    feature_db, invalid_basis
):
    engine, teams = feature_db
    with session_scope(engine) as s:
        game(s, teams, "legacy", 1, goals=(2, 0), basis=None)
        game(s, teams, "published", 2, goals=(4, 0), basis="published")
        game(s, teams, "assumed", 3, goals=(10, 0), basis="assumed")
        missing = game(s, teams, "missing", 4, goals=(99, 0))
        s.get(Result, missing).available_utc = None
        game(s, teams, "invalid-basis", 5, goals=(88, 0), basis=invalid_basis)
        s.flush()
        standard = build(s)
        strict = build(s, strict_availability=True)
    assert standard.iloc[0]["home_last5_goals_for_mean"] == pytest.approx(16 / 3)
    assert standard.iloc[0]["meta_home_last5_assumed_n"] == 1
    assert strict.iloc[0]["home_last5_goals_for_mean"] == 3
    assert strict.iloc[0]["meta_home_last5_assumed_n"] == 0
    assert strict.attrs["strict_availability"] is True
    assert standard.attrs["excluded_history_counts"]["missing_available_utc"] == 1
    assert standard.attrs["excluded_history_counts"]["invalid_available_source"] == 1


def test_generated_time_is_completion_time_separate_from_frozen_cutoff(feature_db, monkeypatch):
    from zucai.features import build as module

    times = iter([CUTOFF, CUTOFF + timedelta(minutes=1)])

    class Clock:
        @staticmethod
        def now(tz):
            return next(times)

    monkeypatch.setattr(module, "datetime", Clock)
    engine, _ = feature_db
    with session_scope(engine) as s:
        frame = build(s)
    assert pd.Timestamp(frame.attrs["generated_utc"]) == CUTOFF + timedelta(minutes=1)
    assert pd.Timestamp(frame.iloc[0]["meta_generated_utc"]) == CUTOFF + timedelta(minutes=1)
    assert pd.Timestamp(frame.attrs["as_of_utc"]) == CUTOFF


def test_historical_target_uses_its_kickoff_even_when_asof_is_later(feature_db):
    from zucai.features.build import build_features

    engine, teams = feature_db
    with session_scope(engine) as s:
        game(s, teams, "past", 3, goals=(2, 0))
        game(s, teams, "late-result", 2, goals=(99, 0), available=KICKOFF + timedelta(hours=1))
        game(s, teams, "later-game", -1, goals=(88, 0))
        frame = build_features(s, ["jc:target"], as_of=KICKOFF + timedelta(days=2))
    assert frame.iloc[0]["home_last5_goals_for_mean"] == 2
    assert pd.Timestamp(frame.iloc[0]["meta_cutoff_utc"]) == KICKOFF


def test_empty_history_has_nan_means_zero_samples_and_fixed_schema(feature_db):
    from zucai.features.build import ALL_COLUMNS, MODEL_FEATURE_COLUMNS, build_features

    engine, _ = feature_db
    with session_scope(engine) as s:
        frame = build(s)
        empty = build_features(s, [], as_of=CUTOFF)
    assert list(frame.columns) == list(empty.columns) == list(ALL_COLUMNS)
    assert empty.empty
    assert frame[list(MODEL_FEATURE_COLUMNS)].isna().all().all()
    assert frame.iloc[0]["meta_home_last5_n"] == 0
    assert math.isnan(frame.iloc[0]["market_p_home"])


@pytest.mark.parametrize("ids", [["missing"], ["jc:target", "jc:target"]])
def test_invalid_target_ids_are_not_silently_ignored(feature_db, ids):
    engine, _ = feature_db
    with session_scope(engine) as s, pytest.raises(ValueError):
        build(s, ids)


@pytest.mark.parametrize("as_of", ["2025-01-01T00:00:00", "not-a-date"])
def test_cutoffs_require_real_timezone_aware_instants(feature_db, as_of):
    from zucai.features.build import build_features

    engine, _ = feature_db
    with session_scope(engine) as s, pytest.raises(ValueError):
        build_features(s, ["jc:target"], as_of=as_of)


def test_market_is_latest_complete_prekickoff_had_snapshot_and_never_a_model_column(feature_db):
    from zucai.features.build import MARKET_COLUMNS, MODEL_FEATURE_COLUMNS

    engine, _ = feature_db
    with session_scope(engine) as s:
        good = CUTOFF - timedelta(hours=2)
        for stamp, pool, line, prices in [
            (good, "had", 0, {"h": 2.0, "d": 4.0, "a": 4.0}),
            (good + timedelta(hours=1), "had", 0, {"h": 9.0}),
            (CUTOFF, "had", 0, {"h": 1.5, "d": 3.0, "a": 6.0}),
            (good + timedelta(minutes=30), "hhad", -1, {"h": 2.0, "d": 3.0, "a": 4.0}),
        ]:
            for selection, price in prices.items():
                s.add(
                    Odds(
                        match_id="jc:target",
                        pool=pool,
                        line=line,
                        selection=selection,
                        price=price,
                        snapshot_utc=stamp,
                    )
                )
        s.flush()
        frame = build(s)
    row = frame.iloc[0]
    assert [row[k] for k in MARKET_COLUMNS] == [0.5, 0.25, 0.25, 0.0]
    assert pd.Timestamp(row["meta_market_snapshot_utc"]) == pd.Timestamp(good)
    assert not any(c.startswith(("meta_", "market_")) for c in MODEL_FEATURE_COLUMNS)
    assert frame.attrs["model_feature_columns"] == list(MODEL_FEATURE_COLUMNS)
