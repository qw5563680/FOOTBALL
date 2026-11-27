import json
from datetime import UTC, datetime, timedelta
from itertools import product
from math import tanh

import pytest

from zucai.predict.experiments.global_rating import (
    RESULT_INPUT_FIELDS,
    RatingParameters,
    RatingResult,
    RatingSnapshot,
    candidate_parameters,
    fit_ratings,
)

START = datetime(2025, 1, 1, tzinfo=UTC)
PARAMETERS = RatingParameters(32, 0, 150, 0.5)


def _game(match_id, home, away, *, league="英冠", score=(1, 0), starts=0, known=3):
    return RatingResult.from_row(
        {
            "match_id": f"fd:{match_id}",
            "source": "fd",
            "league": league,
            "home_id": home,
            "away_id": away,
            "kickoff_utc": START + timedelta(hours=starts),
            "available_utc": None if known is None else START + timedelta(hours=known),
            "available_source": "assumed",
            "home_goals": score[0],
            "away_goals": score[1],
        }
    )


def _fit(history, *, cutoff=24):
    (snapshot,) = fit_ratings(
        history, as_of=START + timedelta(hours=cutoff), parameters=(PARAMETERS,)
    )
    return snapshot


def _bytes(snapshot, home=1, away=2):
    return json.dumps(
        {
            "snapshot": snapshot.as_dict(),
            "prediction": snapshot.predict(
                home, away, kickoff_utc=START + timedelta(days=2)
            ).as_dict(),
        },
        sort_keys=True,
        allow_nan=False,
    ).encode()


def test_only_results_strictly_prior_to_cutoff_enter_snapshot_and_forecast():
    day = _game("past", 1, 2)
    baseline = _fit([day])
    changed = _fit(
        [
            day,
            _game("equal", 1, 2, score=(0, 7), starts=21, known=24),
            _game("late", 1, 2, score=(0, 9), starts=10, known=25),
            _game("future", 3, 4, starts=40, known=43),
            _game("unverified", 3, 5, starts=4, known=None),
            _game("invalid-clock", 6, 7, starts=5, known=5),
        ]
    )
    assert changed.training_rows == 1
    assert _bytes(changed) == _bytes(baseline)


def test_result_at_same_instant_as_later_kickoff_does_not_enter_its_expectation():
    delayed_win = _game("first", 1, 2, known=5)
    draw = _game("second", 1, 3, score=(2, 2), starts=5, known=8)
    snapshot = _fit([draw, delayed_win])
    assert snapshot.ratings == pytest.approx({1: 1516, 2: 1484, 3: 1500})


def test_first_appearances_use_weakest_quarter_of_previous_league_members():
    history = [
        _game("1", 1, 2),
        _game("2", 3, 4, score=(3, 0)),
        _game("3", 5, 6, score=(0, 0)),
    ]
    initial = _fit(history, cutoff=4)
    snapshot = _fit(
        [
            *history,
            _game("4", 7, 8, score=(0, 0), starts=4, known=7),
            _game("5", 9, 10, league="英超", score=(0, 0), starts=4, known=7),
        ]
    )
    lower_quarter = (initial.ratings[2] + initial.ratings[4]) / 2
    assert snapshot.ratings[7] == snapshot.ratings[8] == pytest.approx(lower_quarter)
    assert snapshot.ratings[9] == snapshot.ratings[10] == 1500


def test_same_timestamp_new_teams_cannot_seed_initialization_in_another_league():
    history = [
        _game("0", 1, 2),
        _game("1", 3, 4, starts=4, known=7, score=(0, 0)),
        _game("2", 3, 5, league="英超", starts=4, known=7, score=(0, 0)),
    ]
    snapshot = _fit(history)
    assert snapshot.ratings[4] == 1484
    assert snapshot.ratings[5] < 1500
    assert snapshot.ratings[3] < 1500
    assert _bytes(snapshot) == _bytes(_fit(reversed(history)))


def test_identity_continues_on_same_team_key_and_unknown_target_has_reason():
    snapshot = _fit([_game("old-division", 1, 2, score=(3, 0))])
    kickoff = START + timedelta(days=2)
    known = snapshot.predict(1, 2, kickoff_utc=kickoff)
    missing = snapshot.predict(1, 3, kickoff_utc=kickoff)
    assert sum(known.probabilities) == pytest.approx(1)
    assert known.probabilities[0] > known.probabilities[2]
    assert missing.reason == "missing_team_rating"
    assert missing.missing_team_ids == (3,)
    assert missing.probabilities is None
    assert snapshot.predict(1, 2, kickoff_utc=snapshot.as_of_utc).reason == "cutoff_not_pre_match"


def test_explicit_whitelist_and_candidate_grid_are_deterministic():
    game = _game("with-prices", 1, 2)
    full_record = dict(
        **{field: getattr(game, field) for field in RESULT_INPUT_FIELDS},
        price=1234,
        p_market=0.99,
        closing_odds=(1, 2, 3),
        shots_on_target=100,
    )
    assert RatingResult.from_row(full_record) == game
    assert set(RESULT_INPUT_FIELDS) == {
        "match_id",
        "source",
        "league",
        "home_id",
        "away_id",
        "kickoff_utc",
        "available_utc",
        "available_source",
        "home_goals",
        "away_goals",
    }
    grid = candidate_parameters()
    assert len(grid) == len(set(grid)) == 54
    assert [
        (p.update_scale, p.home_advantage, p.logistic_scale, p.draw_ratio) for p in grid
    ] == list(product((16, 24, 32), (35, 65, 95), (150, 200), (0.5, 0.6, 0.7)))
    cutoff = START + timedelta(days=1)
    snapshots = fit_ratings([game], as_of=cutoff, parameters=grid)
    for params, snapshot in zip(grid, snapshots, strict=True):
        (single,) = fit_ratings([game], as_of=cutoff, parameters=(params,))
        assert _bytes(snapshot) == _bytes(single)


@pytest.mark.parametrize("scale", [150, 200])
@pytest.mark.parametrize("ratio", [0.5, 0.6, 0.7])
def test_draw_probability_peaks_at_zero_home_adjusted_rating_gap(scale, ratio):
    params = RatingParameters(24, 65, scale, draw_ratio=ratio)
    snapshot = RatingSnapshot(params, START, {1: 1435, 2: 1500, 3: 1475, 4: 1525}, 0, None)
    kickoff = START + timedelta(hours=1)
    expected_draw = tanh(ratio / 2)
    expected_win = (1 - expected_draw) / 2
    assert params.draw_threshold == pytest.approx(ratio * scale)
    assert snapshot.as_dict()["parameters"]["draw_ratio"] == ratio
    balanced = snapshot.predict(1, 2, kickoff_utc=kickoff).probabilities
    assert balanced == pytest.approx((expected_win, expected_draw, expected_win))
    for away in (3, 4):
        assert snapshot.predict(1, away, kickoff_utc=kickoff).probabilities[1] < expected_draw


def test_repeated_result_id_is_not_counted_twice():
    game = _game("repeat", 1, 2)
    with pytest.raises(ValueError, match="fd:repeat"):
        _fit([game, game])
