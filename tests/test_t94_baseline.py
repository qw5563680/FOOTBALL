"""T94 historical rates are grouped by league and available strictly before cutoff."""

from datetime import UTC, datetime

import pytest

from zucai.coverage_baseline import historical_baseline
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.result_source_links import record_result_link

CUTOFF = datetime(2026, 11, 26, 8, tzinfo=UTC)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(id=i, name=f"球队{i}") for i in range(1, 9))
        session.flush()
        yield session
    engine.dispose()


def game(
    session,
    match_id,
    score,
    *,
    league="英超",
    teams=(1, 2),
    kickoff="2026-11-25T12:00:00Z",
    available="2026-11-25T15:00:00Z",
    basis="captured",
    status="finished",
):
    match = Match(
        match_id=match_id,
        source=match_id.split(":")[0],
        league=league,
        home_team_id=teams[0],
        away_team_id=teams[1],
        kickoff_utc=kickoff,
        status=status,
    )
    session.add(match)
    session.flush()
    session.add(
        Result(
            match_id=match_id,
            home_goals=score[0],
            away_goals=score[1],
            available_utc=available,
            available_source=basis,
        )
    )
    return match


def test_league_then_global_without_market_or_team_inputs(session):
    game(session, "jc:1", (2, 1))
    game(session, "jc:2", (0, 0), teams=(1, 3), basis="published")
    game(session, "jc:3", (0, 1), league="法甲", teams=(3, 4))

    history = historical_baseline(session, CUTOFF)
    league = history.for_league("英超")
    assert league["model_name"] == "historical-1x2-baseline"
    assert league["model_version"] == "v1"
    assert league["history_scope"] == "league"
    assert league["history_count"] == 2
    assert league["probabilities"] == {"h": 0.5, "d": 0.5, "a": 0.0}
    global_history = history.for_league("亚冠精英")
    assert history.for_league(None) == global_history
    assert global_history["history_scope"] == "global"
    assert global_history["history_count"] == 3
    assert global_history["probabilities"] == dict.fromkeys("hda", 1 / 3)
    assert sum(global_history["probabilities"].values()) == pytest.approx(1)


def test_only_observed_finished_results_strictly_before_cutoff_can_change_rates(session):
    assert historical_baseline(session, CUTOFF).for_league("英超") is None
    game(session, "jc:1", (2, 1))
    before = historical_baseline(session, CUTOFF).for_league("英超")
    extras = [
        {"available": "2026-11-26T08:00:00Z"},
        {"available": "2026-11-26T08:30:00Z"},
        {"available": "2026-11-26T16:00:00+08:00"},
        {"available": None},
        {"basis": "assumed"},
        {"basis": "replayed"},
        {"basis": None},
        {"status": "scheduled"},
        {"status": "cancelled"},
        {"kickoff": "2026-11-25T17:00:00Z"},
    ]
    for index, fields in enumerate(extras, 2):
        game(session, f"jc:{index}", (0, 1), teams=(1, 3), **fields)

    assert historical_baseline(session, CUTOFF).for_league("英超") == before


def test_duplicate_scores_count_once_and_conflicting_scores_never_count(session):
    game(session, "jc:1", (2, 1))
    game(session, "apif:1", (2, 1))
    game(session, "jc:2", (1, 0), teams=(3, 4))
    game(session, "fdorg:2", (0, 1), teams=(3, 4))
    game(session, "jc:3", (1, 1), teams=(5, 6))
    game(session, "apif:3", (0, 2), teams=(5, 6), available="2026-11-26T09:00:00Z")

    counts = historical_baseline(session, CUTOFF).for_league("英超")
    assert counts["history_count"] == 2
    assert counts["probabilities"] == {"h": 0.5, "d": 0.5, "a": 0.0}


def test_dated_link_with_different_kickoff_is_applied_only_after_verification(session):
    target = game(session, "jc:1", (2, 1))
    provider = game(session, "apif:1", (2, 1), kickoff="2026-11-25T11:55:00Z")
    assert record_result_link(session, provider, target, CUTOFF)

    assert historical_baseline(session, CUTOFF).for_league("英超")["history_count"] == 2
    later = datetime(2026, 11, 26, 9, tzinfo=UTC)
    assert historical_baseline(session, later).for_league("英超")["history_count"] == 1
