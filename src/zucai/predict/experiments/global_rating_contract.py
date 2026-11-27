"""Frozen identity of the T100 experiment, also used by archive consumers."""

RATING_NAME = "elo-global-v1"
RATING_VERSION = "t100-cross-league-rating-v1"
RATING_RUN_KEY = "global_rating_version"


def is_rating_target(row):
    return row.get("model_name") == "dixon-coles" and row.get("status") == "predicted"
