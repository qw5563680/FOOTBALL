from collections import Counter
from datetime import UTC, datetime

import pytest

from zucai.scoreboard.score import prematch_rows, published_score_keys, record_key

BEFORE_JC_PUBLICATION = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)
AFTER_KICKOFF = datetime(2026, 11, 27, 0, tzinfo=UTC)


def _forecasts():
    sfc = dict(
        status="predicted",
        mode="prematch",
        model_name="dixon-coles",
        match_id="apif:1",
        source="apif",
        league="欧罗巴",
        home="甲",
        away="乙",
        sfc_only=True,
        batch_id="early-sfc",
        as_of_utc="2026-11-26T08:00:00Z",
        generated_utc="2026-11-26T08:00:00Z",
        published_utc="2026-11-26T08:02:00Z",
        kickoff_utc="2026-11-26T17:45:00Z",
    )
    jc = dict(
        sfc,
        match_id="jc:1",
        source="jc",
        sfc_only=False,
        batch_id="later-jc",
        as_of_utc="2026-11-26T10:00:00Z",
        generated_utc="2026-11-26T10:00:00Z",
        published_utc="2026-11-26T10:02:00Z",
        kickoff_utc="2026-11-27T01:45:00+08:00",
    )
    return sfc, jc


def test_published_jc_forecast_supersedes_only_duplicate_active_sfc_score():
    sfc, jc = _forecasts()
    forecasts = [sfc, jc]
    assert prematch_rows(forecasts, BEFORE_JC_PUBLICATION) == [sfc]
    excluded = Counter()
    assert prematch_rows(forecasts, AFTER_KICKOFF, excluded=excluded) == [jc]
    assert excluded["sfc_jc_counterpart"] == 1

    scores = [
        dict(
            match_id=row["match_id"],
            batch_id=row["batch_id"],
            record_id=record_key(row["match_id"], row["batch_id"]),
        )
        for row in forecasts
    ]
    assert published_score_keys(scores, forecasts, AFTER_KICKOFF) == {scores[1]["record_id"]}
    assert len(scores) == 2  # Historical score rows remain append-only.


@pytest.mark.parametrize("difference", [{"sfc_only": False}, {"home": "乙", "away": "甲"}])
def test_different_scope_or_fixture_is_not_treated_as_jc_counterpart(difference):
    sfc, jc = _forecasts()
    sfc.update(difference)
    assert prematch_rows([sfc, jc], AFTER_KICKOFF) == [sfc, jc]
