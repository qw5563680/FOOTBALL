"""Only invented source arrivals/probabilities; no fixed-archive performance run."""

import json
from pathlib import Path

import numpy as np
import pytest
from paired_runner import (
    fit_selected,
    had,
    paired_metrics,
    point_metrics,
    previous_coverage,
    score_version,
    select_rows,
    summarize_previous,
)

FIXTURE = Path("/workspace/work/football-t63/data/fixtures/t63/source-latency.json")


def evidence(rows, at="2026-11-01T14:00:00+00:00"):
    proofs = {row["match_id"]: {"conservative_available_utc": at} for row in rows}
    events = {
        row["match_id"]: [
            {"conservative_available_utc": at, "version": score_version(row)}
        ]
        for row in rows
    }
    return proofs, events, proofs


def fixture_rows():
    return [
        dict(row, ht_home_goals=None, ht_away_goals=None)
        for row in json.loads(FIXTURE.read_bytes())["base_rows"]
    ]


def test_fd_assumed_window_result_waits_for_its_actual_version_archive_commit():
    row = dict(
        fixture_rows()[0],
        kickoff_utc="2026-11-01T12:00:00+00:00",
        available_utc="2026-11-01T15:00:00+00:00",
        available_source="assumed",
    )
    proofs, events, identities = evidence([row], "2026-11-03T10:00:00+00:00")
    selected, excluded = select_rows(
        [row], "2026-11-02T00:00:00+00:00", "old", proofs, events, identities
    )
    assert (
        not selected
        and "current_result_version_not_archived_before_cutoff"
        in excluded[0]["reasons"]
    )
    selected, _ = select_rows(
        [row], "2026-11-03T12:00:00+00:00", "old", proofs, events, identities
    )
    assert selected[0]["available_utc"] == "2026-11-03T10:00:01+00:00"
    assert selected[0]["stored_available_source"] == "assumed"
    assert row["available_utc"] == "2026-11-01T15:00:00+00:00"


def test_same_git_second_does_not_invent_subsecond_availability():
    rows = fixture_rows()
    proofs, events, identities = evidence(rows)
    selected, _ = select_rows(
        rows, "2026-11-01T14:00:00.999999+00:00", "old", proofs, events, identities
    )
    assert not selected


def test_added_source_without_timed_identity_is_not_approved_by_friendly_names():
    row = dict(
        fixture_rows()[0],
        match_id="apif:synthetic:1",
        source="apif",
        counterpart_id="jc:synthetic:1",
    )
    proofs, events, identities = evidence([row])
    selected, excluded = select_rows(
        [row], "2026-11-02T00:00:00+00:00", "new", proofs, events, identities
    )
    assert not selected
    assert {"unproven_historical_identity", "unproven_historical_link"} <= set(
        excluded[0]["reasons"]
    )


def test_current_score_version_must_be_active_at_cutoff():
    rows = fixture_rows()
    proofs, events, identities = evidence(rows)
    events[rows[0]["match_id"]].append(
        {
            "conservative_available_utc": "2026-11-02T00:00:00+00:00",
            "version": [rows[0]["match_id"], 9, 9, None, None],
        }
    )
    selected, excluded = select_rows(
        rows, "2026-11-02T12:00:00+00:00", "old", proofs, events, identities
    )
    assert len(selected) == len(rows) - 1
    assert "current_result_version_not_active_at_cutoff" in excluded[0]["reasons"]


def test_synthetic_identical_arms_fit_separately_and_return_legal_probabilities():
    rows = fixture_rows()
    proofs, events, identities = evidence(rows)
    old, _ = select_rows(
        rows, "2026-11-02T12:00:00+00:00", "old", proofs, events, identities
    )
    new, _ = select_rows(
        rows, "2026-11-02T12:00:00+00:00", "new", proofs, events, identities
    )
    parameters = {"half_life_days": 90, "draw_calibration": False}
    old_model = fit_selected(old, "2026-11-02T12:00:00+00:00", parameters, ["fd", "jc"])
    new_model = fit_selected(
        new, "2026-11-02T12:00:00+00:00", parameters, ["apif", "fd", "jc"]
    )
    target = dict(rows[0], kickoff_utc="2026-11-03T12:00:00+00:00", source="jc")
    old_probs = had(old_model.predict_score_matrix(target))
    new_probs = had(new_model.predict_score_matrix(target))
    np.testing.assert_array_equal(old_probs, new_probs)
    assert sum(old_probs) == pytest.approx(1)


def test_metrics_use_unscaled_brier_and_fractional_strict_tie_credit():
    metrics = point_metrics([0.5, 0.5, 0.0], 0)
    assert metrics["log_loss"] == pytest.approx(np.log(2))
    assert metrics["brier"] == 0.5
    assert metrics["hit_rate"] == 0.5
    row = {
        "paired_scoring_eligible": True,
        "result": {"home_goals": 2, "away_goals": 0},
        "old": {"probabilities": [0.5, 0.5, 0.0]},
        "new": {"probabilities": [0.5, 0.5, 0.0]},
    }
    summary = paired_metrics([row])
    assert summary["paired_n"] == 1
    for value in summary["metrics"].values():
        assert value["new_minus_old"] == 0
        assert value["paired_descriptive_95_percentile_interval"] == [0, 0]


def test_at_least_one_known_training_side_counts_when_the_other_side_is_unknown():
    known = {
        "status": "identified",
        "old_in_training": True,
        "new_in_training": True,
        "actual_conservative_archive_bound_before_cutoff": True,
        "stored_available_source": "captured",
    }
    unknown = {"status": "unknown", "old_in_training": None, "new_in_training": None}
    records = [
        {"previous_coverage": {"home": known, "away": known}},
        {"previous_coverage": {"home": known, "away": unknown}},
        {"previous_coverage": {"home": unknown, "away": unknown}},
    ]
    counts = summarize_previous(records)["arms"]["old"]
    assert counts["matches_both_previous_in_training"] == 1
    assert counts["matches_at_least_one_previous_in_training"] == 2
    assert counts["matches_with_unknown_previous"] == 2


def test_failed_fit_does_not_count_selected_candidate_ids_as_actual_training():
    previous = {
        "status": "identified_legacy_previous",
        "candidates": [
            {
                "match_id": "fd:one",
                "source": "fd",
                "kickoff_utc": "2026-11-01T12:00:00Z",
                "result": {"available_source": "assumed"},
                "archive_current_score_version_before_cutoff": True,
                "current_score_version_proof": {
                    "conservative_available_utc": "2026-11-02T00:00:00Z"
                },
            }
        ],
    }
    target = {"previous": {"home": previous, "away": previous}}
    coverage = previous_coverage(target, None, {"fd:one"})
    assert coverage["home"]["old_in_training"] is None
    counts = summarize_previous([{"previous_coverage": coverage}])["arms"]["old"]
    assert counts.get("in_training_team_occurrences", 0) == 0
    assert counts["fit_unavailable_team_occurrences"] == 2
