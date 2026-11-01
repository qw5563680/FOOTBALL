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
    if goals is not None:
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


def quotes(session, stamp, prices, *, pool="hhad", line=-1, match_id="jc:target"):
    for selection, price in prices.items():
        session.add(
            Odds(
                match_id=match_id,
                pool=pool,
                line=line,
                selection=selection,
                price=price,
                snapshot_utc=stamp,
            )
        )
    session.flush()


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
    performance_columns = [
        c for c in MODEL_FEATURE_COLUMNS if c not in {"home_rest_days", "away_rest_days"}
    ]
    assert len(performance_columns) == 41
    pd.testing.assert_frame_equal(before[performance_columns], after[performance_columns])
    assert after.iloc[0]["meta_home_last10_n"] == 1
    assert before.iloc[0]["home_rest_days"] == 7
    assert after.iloc[0]["home_rest_days"] == 3
    assert after.iloc[0]["meta_home_rest_match_id"] == "jc:known-at-cutoff"


@pytest.mark.parametrize("side", ["home", "away"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("status", ["scheduled", "finished"])
@pytest.mark.parametrize("result_state", ["missing_result", "missing_available"])
def test_rest_uses_all_same_source_schedules_before_kickoff_without_result_availability(
    feature_db, side, strict, status, result_state
):
    from zucai.features.build import MODEL_FEATURE_COLUMNS

    engine, teams = feature_db
    team = "A" if side == "home" else "B"
    previous_home, previous_away = ("X", team) if side == "home" else (team, "X")
    venue = "away" if side == "home" else "home"
    with session_scope(engine) as s:
        game(s, teams, "known", 7, home=previous_home, away=previous_away, goals=(2, 1))
        before = build(s, strict_availability=strict)
        recent = game(
            s,
            teams,
            "recent-schedule",
            0.05,
            home=previous_home,
            away=previous_away,
            league="Cup",
            status=status,
            goals=None if result_state == "missing_result" else (99, 0),
            basis="assumed",
        )
        if result_state == "missing_available":
            s.get(Result, recent).available_utc = None
        for key, days, options in [
            ("cancelled", 0.04, {"status": "cancelled"}),
            ("other-provider", 0.03, {"source": "c500"}),
            ("same-kickoff", 0, {}),
            ("future", -1, {}),
        ]:
            game(
                s,
                teams,
                key,
                days,
                home=previous_home,
                away=previous_away,
                goals=None,
                **options,
            )
        game(s, teams, "unrelated", 0.01, home="X", away="Y", goals=None)
        s.flush()
        frame = build(s, strict_availability=strict)
    row = frame.iloc[0]
    assert row[f"{side}_rest_days"] == pytest.approx(0.05)
    assert row[f"meta_{side}_rest_match_id"] == recent
    evidence = frame.attrs["audit"]["jc:target"]["windows"][f"{side}_rest"]
    assert len(evidence) == 1
    assert set(evidence[0]) == {"match_id", "source", "league", "kickoff_utc", "status", "venue"}
    assert evidence[0] == {
        "match_id": recent,
        "source": "jc",
        "league": "Cup",
        "kickoff_utc": (KICKOFF - timedelta(days=0.05)).isoformat().replace("+00:00", "Z"),
        "status": status,
        "venue": venue,
    }
    assert pd.Timestamp(evidence[0]["kickoff_utc"]) > CUTOFF
    performance_columns = [
        c for c in MODEL_FEATURE_COLUMNS if c not in {"home_rest_days", "away_rest_days"}
    ]
    pd.testing.assert_frame_equal(before[performance_columns], frame[performance_columns])
    for name in ("meta_history_n", "meta_history_basis", "meta_assumed_history_share"):
        assert row[name] == before.iloc[0][name]
    for window, games in before.attrs["audit"]["jc:target"]["windows"].items():
        if window not in {"home_rest", "away_rest"}:
            assert frame.attrs["audit"]["jc:target"]["windows"][window] == games


def test_rest_evidence_does_not_depend_on_added_corrected_or_deleted_results(feature_db):
    engine, teams = feature_db
    with session_scope(engine) as s:
        recent = game(s, teams, "cup", 1, league="Cup", status="scheduled", goals=None)
        frames = [build(s)]
        result = Result(
            match_id=recent,
            home_goals=9,
            away_goals=0,
            available_utc=CUTOFF + timedelta(days=1),
            available_source="assumed",
        )
        s.add(result)
        s.flush()
        frames.append(build(s, strict_availability=True))
        result.home_goals, result.away_goals = 0, 8
        result.available_utc, result.available_source = None, "unknown"
        s.flush()
        frames.append(build(s))
        s.delete(result)
        s.flush()
        frames.append(build(s))
    for frame in frames:
        assert frame.iloc[0]["home_rest_days"] == 1
        assert frame.iloc[0]["meta_home_rest_match_id"] == recent
        assert (
            frame.attrs["audit"]["jc:target"]["windows"]["home_rest"]
            == frames[0].attrs["audit"]["jc:target"]["windows"]["home_rest"]
        )


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
    from zucai.features.build import (
        ALL_COLUMNS,
        MARKET_COLUMNS,
        META_COLUMNS,
        MODEL_FEATURE_COLUMNS,
        build_features,
    )

    engine, _ = feature_db
    with session_scope(engine) as s:
        frame = build(s)
        empty = build_features(s, [], as_of=CUTOFF)
    assert list(frame.columns) == list(empty.columns) == list(ALL_COLUMNS)
    assert empty.empty
    assert frame[list(MODEL_FEATURE_COLUMNS)].isna().all().all()
    assert frame.iloc[0]["meta_home_last5_n"] == 0
    assert math.isnan(frame.iloc[0]["market_p_home"])
    assert MARKET_COLUMNS == (
        "market_p_home",
        "market_p_draw",
        "market_p_away",
        "market_hhad_line",
        "market_hhad_p_home",
        "market_hhad_p_draw",
        "market_hhad_p_away",
    )
    assert len(MODEL_FEATURE_COLUMNS) == 43
    assert "market_line" not in ALL_COLUMNS
    for side in ("home", "away"):
        assert f"meta_{side}_rest_n" not in META_COLUMNS
        assert f"meta_{side}_rest_assumed_n" not in META_COLUMNS
        assert f"meta_{side}_rest_match_id" in META_COLUMNS
        assert pd.isna(frame.iloc[0][f"meta_{side}_rest_match_id"])
        assert frame.attrs["audit"]["jc:target"]["windows"][f"{side}_rest"] == []
    for exported in (frame, empty):
        assert exported["market_hhad_line"].dtype == pd.Int64Dtype()
        assert exported["market_hhad_line"].isna().all()
        assert exported.attrs["schema_version"] == 1
        assert exported.attrs["feature_version"] == "t04-v2"
        assert exported.attrs["market_columns"] == list(MARKET_COLUMNS)


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
    assert [row[f"market_p_{side}"] for side in ("home", "draw", "away")] == [0.5, 0.25, 0.25]
    assert row["market_hhad_line"] == -1
    assert frame["market_hhad_line"].dtype == pd.Int64Dtype()
    assert [row[f"market_hhad_p_{side}"] for side in ("home", "draw", "away")] == pytest.approx(
        [6 / 13, 4 / 13, 3 / 13]
    )
    assert pd.Timestamp(row["meta_market_snapshot_utc"]) == pd.Timestamp(good)
    assert pd.Timestamp(row["meta_market_hhad_snapshot_utc"]) == good + timedelta(minutes=30)
    assert not any(c.startswith(("meta_", "market_")) for c in MODEL_FEATURE_COLUMNS)
    assert frame.attrs["model_feature_columns"] == list(MODEL_FEATURE_COLUMNS)
    assert set(MARKET_COLUMNS).isdisjoint(MODEL_FEATURE_COLUMNS)
    hhad = frame.attrs["audit"]["jc:target"]["market_hhad"]
    assert hhad == {
        "snapshot_utc": (good + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "pool": "hhad",
        "line": -1,
        "prices": {"h": 2.0, "d": 3.0, "a": 4.0},
        "normalization": "inverse_price_proportional",
        "model_input": False,
    }


@pytest.mark.parametrize(
    "invalid_snapshot",
    [
        "incomplete",
        "split_line",
        "split_time",
        "fractional_line",
        "infinite_line",
        "invalid_price",
        "infinite_price",
    ],
)
def test_hhad_uses_latest_complete_same_line_snapshot_and_skips_invalid_quotes(
    feature_db, invalid_snapshot
):
    engine, _ = feature_db
    older, good, invalid = [CUTOFF - timedelta(hours=n) for n in (3, 2, 1)]
    with session_scope(engine) as s:
        quotes(s, older, {"h": 9.0, "d": 8.0, "a": 7.0}, line=1)
        quotes(s, good, {"h": 4.0, "d": 4.0, "a": 2.0}, line=-2)
        if invalid_snapshot == "incomplete":
            quotes(s, invalid, {"h": 2.0, "d": 2.0}, line=-3)
        elif invalid_snapshot == "split_line":
            quotes(s, invalid, {"h": 2.0}, line=-3)
            quotes(s, invalid, {"d": 2.0, "a": 2.0}, line=-4)
        elif invalid_snapshot == "split_time":
            quotes(s, invalid, {"h": 2.0}, line=-3)
            quotes(s, invalid + timedelta(minutes=1), {"d": 2.0, "a": 2.0}, line=-3)
        elif invalid_snapshot == "fractional_line":
            quotes(s, invalid, {"h": 2.0, "d": 2.0, "a": 2.0}, line=-0.5)
        elif invalid_snapshot == "infinite_line":
            quotes(s, invalid, {"h": 2.0, "d": 2.0, "a": 2.0}, line=math.inf)
        elif invalid_snapshot == "invalid_price":
            quotes(s, invalid, {"h": 1.0, "d": 2.0, "a": 2.0}, line=-3)
        else:
            quotes(s, invalid, {"h": math.inf, "d": 2.0, "a": 2.0}, line=-3)
        frame = build(s)
    row = frame.iloc[0]
    assert row["market_hhad_line"] == -2
    assert frame["market_hhad_line"].dtype == pd.Int64Dtype()
    assert [row[f"market_hhad_p_{side}"] for side in ("home", "draw", "away")] == [0.25, 0.25, 0.5]
    assert pd.Timestamp(row["meta_market_hhad_snapshot_utc"]) == good
    assert pd.isna(row["market_p_home"])
    assert frame.attrs["audit"]["jc:target"]["market_hhad"]["prices"] == {
        "h": 4.0,
        "d": 4.0,
        "a": 2.0,
    }


@pytest.mark.parametrize("as_of", [CUTOFF, KICKOFF + timedelta(days=1)])
def test_hhad_and_had_quotes_must_be_strictly_before_minimum_of_asof_and_kickoff(feature_db, as_of):
    from zucai.features.build import build_features

    engine, _ = feature_db
    effective = min(as_of, KICKOFF)
    good = effective - timedelta(hours=1)
    with session_scope(engine) as s:
        for pool, line in (("had", 0), ("hhad", -1)):
            quotes(s, good, {"h": 2.0, "d": 4.0, "a": 4.0}, pool=pool, line=line)
            quotes(s, effective, {"h": 4.0, "d": 2.0, "a": 4.0}, pool=pool, line=line)
            quotes(
                s,
                effective + timedelta(minutes=1),
                {"h": 4.0, "d": 4.0, "a": 2.0},
                pool=pool,
                line=line,
            )
        frame = build_features(s, ["jc:target"], as_of=as_of)
    row = frame.iloc[0]
    for prefix in ("market", "market_hhad"):
        assert [row[f"{prefix}_p_{side}"] for side in ("home", "draw", "away")] == [0.5, 0.25, 0.25]
        key = "meta_market_snapshot_utc" if prefix == "market" else "meta_market_hhad_snapshot_utc"
        assert pd.Timestamp(row[key]) == good


@pytest.mark.parametrize("pool", ["had", "hhad"])
def test_incomplete_market_does_not_borrow_prices_from_the_other_pool(feature_db, pool):
    engine, _ = feature_db
    with session_scope(engine) as s:
        good = CUTOFF - timedelta(hours=1)
        if pool == "had":
            quotes(s, good, {"h": 2.0}, pool="had", line=0)
            quotes(s, good, {"d": 4.0, "a": 4.0}, pool="hhad", line=-1)
        else:
            quotes(s, good, {"h": 2.0}, pool="hhad", line=-1)
            quotes(s, good, {"d": 4.0, "a": 4.0}, pool="had", line=0)
        frame = build(s)
    for prefix in ("market", "market_hhad"):
        assert frame[[f"{prefix}_p_{side}" for side in ("home", "draw", "away")]].isna().all().all()
    assert pd.isna(frame.iloc[0]["market_hhad_line"])
    assert frame["market_hhad_line"].dtype == pd.Int64Dtype()
