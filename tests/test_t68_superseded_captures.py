"""T68: a page fetched before kickoff is the same fixture at an earlier moment, not a
contradiction of the finished capture. Everything else must still agree in full."""

import pytest

from zucai.identity_daily_gate import _settled_candidates


def _capture(
    fetched, *, status="finished", raw="FT", score=(2, 1), kickoff="2026-11-14T19:00:00+00:00"
):
    return dict(
        match_id="apif:1",
        competition_id="89",
        home="1",
        away="2",
        kickoff_utc=kickoff,
        status=status,
        raw_status=raw,
        score=list(score) if score else None,
        provenance=[dict(path="p", sha256="0" * 64, fetched_at_utc=fetched)],
    )


def _early(fetched="2026-11-14T03:00:00+00:00", **changes):
    return dict(_capture(fetched, status="NS", raw="NS", score=None), **changes)


FINAL = "2026-11-18T08:00:00+00:00"


def test_capture_before_kickoff_is_set_aside_once_a_result_exists():
    early, final = _early(), _capture(FINAL)
    kept, superseded = _settled_candidates([early, _early("2026-11-14T04:00:00+00:00"), final])
    assert kept == [final]
    assert len(superseded) == 2 and early in superseded


def test_without_a_finished_capture_nothing_is_set_aside():
    rows = [_early(), _early("2026-11-14T04:00:00+00:00")]
    assert _settled_candidates(rows) == (rows, [])


@pytest.mark.parametrize(
    "changes",
    [
        dict(home="3"),  # another team: a real contradiction
        dict(competition_id="88"),
        dict(score=[0, 0]),  # carries a score: not an unplayed capture
        dict(raw_status="1H", status="1H"),  # in play: not before kickoff
        dict(raw_status="CANC", status="CANC"),
        dict(provenance=[]),
    ],
)
def test_anything_else_stays_a_candidate(changes):
    early, final = _early(**changes), _capture(FINAL)
    kept, superseded = _settled_candidates([early, final])
    assert kept == [early, final] and superseded == []


def test_unplayed_capture_fetched_after_its_kickoff_stays():
    late = _early("2026-11-14T21:30:00+00:00")
    kept, superseded = _settled_candidates([late, _capture(FINAL)])
    assert late in kept and superseded == []


def test_unplayed_capture_fetched_after_the_result_stays():
    after = _early("2026-11-19T00:00:00+00:00", kickoff="2026-11-20T19:00:00+00:00")
    kept, superseded = _settled_candidates([after, _capture(FINAL)])
    assert after in kept and superseded == []


def test_postponed_capture_without_clock_is_set_aside():
    postponed = _early(raw_status="PST", status="PST", kickoff_utc=None)
    kept, superseded = _settled_candidates([postponed, _capture(FINAL)])
    assert superseded == [postponed] and len(kept) == 1


def test_two_finished_captures_are_both_kept_for_the_full_comparison():
    first, second = _capture(FINAL), _capture("2026-11-18T09:00:00+00:00", score=(3, 1))
    kept, superseded = _settled_candidates([_early(), first, second])
    assert kept == [first, second] and len(superseded) == 1


def _materialized_facts():
    return dict(
        configs={"89": {"league": "测试乙"}},
        formal={
            "测试乙": {
                "1": {"status": "approved", "canonical": "甲队"},
                "2": {"status": "approved", "canonical": "乙队"},
            }
        },
        teams={
            "a": {"name": "甲队", "project_team_id": "11"},
            "b": {"name": "乙队", "project_team_id": "12"},
        },
    )


def _state(home=11, away=12):
    return dict(
        matches={
            "apif:1": dict(
                match_id="apif:1",
                source="apif",
                league="测试乙",
                home_team_id=home,
                away_team_id=away,
                kickoff_utc="2026-11-08T11:15:00Z",
                status="postponed",
                counterpart_id=None,
            )
        },
        results={},
    )


def _original(**changes):
    row = dict(
        match_id="apif:1",
        home="1",
        away="2",
        kickoff_utc=None,
        raw_status="PST",
        score=None,
    )
    row.update(changes)
    return row


def test_postponed_original_without_clock_is_checked_by_its_teams():
    from zucai.identity_daily_gate import _materialized_original

    _materialized_original(_materialized_facts(), "89", _state(), _original())
    with pytest.raises(ValueError, match="differs from original"):
        _materialized_original(_materialized_facts(), "89", _state(home=12, away=11), _original())


@pytest.mark.parametrize(
    "changes",
    [
        dict(raw_status="NS"),  # an ordinary capture without a time is still refused
        dict(raw_status="FT"),
        dict(kickoff_utc="2026-11-09T11:15:00+00:00", raw_status="NS"),  # a day away
    ],
)
def test_other_originals_still_need_a_matching_time(changes):
    from zucai.identity_daily_gate import _materialized_original

    with pytest.raises(ValueError, match="differs from original"):
        _materialized_original(_materialized_facts(), "89", _state(), _original(**changes))
