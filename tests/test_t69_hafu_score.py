"""T69 scoreboard recording of half-time/full-time outcomes (additive only)."""

import math

from zucai.predict.hafu_share import HAFU_SELECTIONS
from zucai.scoreboard.score import _hafu_score, _hafu_statistics


def test_hafu_score_records_nine_class_result():
    row = {
        "half_full_time": {
            "status": "experimental",
            "probabilities": {key: 1 / 9 for key in HAFU_SELECTIONS},
            "share": 0.45,
            "share_tier": "league",
        }
    }
    result = {
        "home_goals": 2,
        "away_goals": 1,
        "ht_home_goals": 1,
        "ht_away_goals": 0,
    }
    scored = _hafu_score(row, result)
    assert scored["status"] == "scored"
    assert scored["actual"] == "hh"
    assert scored["share"] == 0.45
    assert scored["log_loss"] == -math.log(1 / 9)
    assert math.isclose(scored["brier"], 8 / 9)


def test_hafu_score_missing_half_scores_or_field():
    row = {
        "half_full_time": {
            "status": "experimental",
            "probabilities": {key: 1 / 9 for key in HAFU_SELECTIONS},
            "share": 0.45,
            "share_tier": "league",
        }
    }
    assert _hafu_score({}, {"home_goals": 1, "away_goals": 0}) is None
    unscored = _hafu_score(row, {"home_goals": 1, "away_goals": 0})
    assert unscored["status"] == "unscored_no_half_score" and unscored["actual"] is None


def test_hafu_statistics_aggregates_only_scored_rows():
    scored = {
        "status": "scored",
        "actual": "hh",
        "probabilities": {key: 1 / 9 for key in HAFU_SELECTIONS},
        "log_loss": 1.0,
        "brier": 0.5,
    }
    stats = _hafu_statistics(
        [
            {"half_full_time": scored},
            {"half_full_time": {"status": "unscored_no_half_score", "actual": None}},
            {},
        ]
    )
    assert stats["n"] == 1
    assert stats["log_loss"] == 1.0
    assert stats["brier"] == 0.5
    assert stats["unscored_no_half_score"] == 1
    assert isinstance(stats["calibration"], list)
