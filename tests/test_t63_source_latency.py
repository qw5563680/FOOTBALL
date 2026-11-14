"""T63 uses invented offline source arrivals, never real performance results."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from zucai.predict.dixon_coles import DixonColes
from zucai.predict.pooled import select_training_history
from zucai.predict.training_sources import apif_history_sources

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t63/source-latency.json").read_text())


def history():
    frame = pd.DataFrame(FIXTURE["base_rows"] + FIXTURE["arrivals"])
    frame["provider_identity_verified"] = True  # Synthetic fixture's explicit approved identities.
    frame["identity_verified_utc"] = "2026-11-01T14:00:00Z"
    for key in ("kickoff_utc", "available_utc"):
        frame[key] = pd.to_datetime(frame[key], utc=True)
    frame.attrs.update(sources=["apif", "fd", "jc"], linked_source_selection=True)
    frame.attrs["verified_result_links"] = [
        ("apif:synthetic:late", "fd:synthetic:late", "2026-11-03T10:00Z")
    ]
    return frame


@pytest.mark.parametrize(
    "cutoff,source",
    [("2026-11-02T00:00Z", "apif"), ("2026-11-03T12:00Z", "fd"), ("2026-11-05T00:00Z", "jc")],
)
def test_first_paid_result_remains_usable_until_higher_priority_result_arrives(cutoff, source):
    frame = history()
    expected = frame.loc[~frame.match_id.str.endswith(":late") | (frame.source == source)].copy()
    expected.attrs.pop("linked_source_selection")
    model = DixonColes().fit(frame, as_of=cutoff)
    reference = DixonColes().fit(expected, as_of=cutoff)
    assert model.training_rows == 19
    assert model.training_source_counts == reference.training_source_counts
    np.testing.assert_array_equal(model.engine._params, reference.engine._params)


def test_equal_cutoff_is_not_known_and_unlinked_overlap_still_fails():
    frame = history()
    model = DixonColes().fit(frame, as_of="2026-11-03T10:00Z")
    assert model.training_source_counts == {"apif": 1, "fd": 18}
    frame.loc[frame.source == "apif", "counterpart_id"] = None
    frame.attrs["verified_result_links"] = []
    with pytest.raises(ValueError, match="重复|重叠"):
        DixonColes().fit(frame, as_of="2026-11-05T00:00Z")


def test_old_assumed_paid_time_needs_actual_observation_without_rewriting_history():
    frame = history()
    paid = frame.source == "apif"
    frame.loc[paid, "available_source"] = "assumed"
    frame["result_checked_utc"] = pd.NaT
    frame["result_checked_utc"] = pd.to_datetime(frame.result_checked_utc, utc=True)
    original = frame.copy(deep=True)
    model = DixonColes().fit(frame, as_of="2026-11-02T00:00Z")
    assert model.training_rows == 18
    pd.testing.assert_frame_equal(frame, original)
    frame.loc[paid, "result_checked_utc"] = pd.Timestamp("2026-11-02T12:00Z")
    before = DixonColes().fit(frame, as_of="2026-11-02T12:00Z")
    after = DixonColes().fit(frame, as_of="2026-11-02T13:00Z")
    assert before.training_rows == 18
    assert after.training_rows == 19
    assert after.latest_available == pd.Timestamp("2026-11-02T12:00Z")
    assert frame.loc[paid, "available_source"].item() == "assumed"
    assert frame.loc[paid, "available_utc"].item() == pd.Timestamp("2026-11-01T15:00Z")


def test_unverified_paid_identity_is_not_a_training_team():
    frame = history()
    frame.loc[frame.source == "apif", "home"] = "apif:team:551"
    model = DixonColes().fit(frame, as_of="2026-11-02T00:00Z")
    assert model.training_rows == 18
    assert "apif:team:551" not in model.teams


def test_conflicting_paid_score_does_not_override_legacy_fd_score():
    frame = history()
    frame.loc[frame.source == "apif", ["home_goals", "away_goals"]] = [7, 4]
    before = select_training_history(frame, "2026-11-02T00:00Z")
    assert before.loc[before.source == "apif", "home_goals"].item() == 7
    after = select_training_history(frame, "2026-11-03T12:00Z")
    assert after.loc[after.match_id == "fd:synthetic:late", "home_goals"].item() == 2


def test_unresolved_api_only_conflict_is_withheld_and_scored_once_as_unavailable():
    from zucai.predict.pooled import evaluation_history, select_training_history

    frame = history()
    frame = frame.loc[frame.source != "jc"].copy()
    late = frame.match_id == "fd:synthetic:late"
    frame.loc[late, "source"] = "fdorg"
    frame.loc[late, "match_id"] = "fdorg:synthetic:late"
    frame.loc[frame.source == "apif", "counterpart_id"] = "fdorg:synthetic:late"
    frame.attrs["verified_result_links"] = [
        ("apif:synthetic:late", "fdorg:synthetic:late", "2026-11-03T10:00Z")
    ]
    frame.loc[late, "home_goals"] = 8
    frame["league"] = "英超"
    frame.attrs["sources"] = ["apif", "fd", "fdorg"]
    assert len(select_training_history(frame, "2026-11-05T00:00Z")) == 18
    labels = evaluation_history(frame, "2026-11-05T00:00Z")
    assert len(labels) == 19 and labels.available_utc.isna().sum() == 1


def test_later_link_cannot_resolve_an_earlier_overlap():
    from zucai.predict.pooled import select_training_history

    frame = history()
    frame.attrs["verified_result_links"] = [
        ("apif:synthetic:late", "fd:synthetic:late", "2026-11-05T00:00Z"),
        ("fd:synthetic:late", "jc:synthetic:late", "2026-11-05T00:00Z"),
    ]
    with pytest.raises(ValueError, match="重复|重叠"):
        select_training_history(frame, "2026-11-04T12:00Z")
    assert len(select_training_history(frame, "2026-11-06T00:00Z")) == 19


def test_present_day_numeric_binding_is_not_historical_identity_evidence():
    frame = history()
    paid = frame.source == "apif"
    frame.loc[paid, "identity_verified_utc"] = None
    assert len(select_training_history(frame, "2026-11-02T00:00Z")) == 18
    frame.loc[paid, "identity_verified_utc"] = "2026-11-02T00:00Z"
    assert len(select_training_history(frame, "2026-11-02T00:00Z")) == 18
    assert len(select_training_history(frame, "2026-11-02T00:01Z")) == 19


def test_unsettled_added_source_anchor_cannot_supply_timeless_historical_links():
    frame = history().loc[lambda value: value.source != "apif"].copy()
    frame["counterpart_id"] = None
    frame.attrs["verified_result_links"] = [
        ("fd:synthetic:late", "apif:anchor-without-result"),
        ("jc:synthetic:late", "apif:anchor-without-result"),
    ]
    with pytest.raises(ValueError, match="重复|重叠"):
        select_training_history(frame, "2026-11-05T00:00Z")


def test_evaluation_final_label_and_training_fold_use_different_source_arrival_cutoffs():
    from zucai.predict.backtest import evaluate

    frame = history()
    report = evaluate(frame, {}, since="2026-11-01T00:00Z", as_of="2026-11-05T00:00Z")
    assert report["in_period"] == 1
    assert report["predictions"][0]["match_id"] == "jc:synthetic:late"


def test_source_expansion_invalidates_evidence_but_preserves_all_numeric_model_policy():
    baseline = json.loads((ROOT / "data/fixtures/t63/baseline-policy.json").read_text())
    current = json.loads((ROOT / "data/model/params.json").read_text())
    assert current["gate"] == baseline["gate"]
    policy = apif_history_sources()
    legacy = {"英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲"}
    for league in FIXTURE["leagues"]:
        expected = {"fd", "jc", "apif"}
        if league in FIXTURE["fdorg_leagues"]:
            expected.add("fdorg")
        assert policy[league] == expected
        if league in legacy:
            assert current["leagues"][league]["sources"] == ["fd", "jc"]
            assert current["leagues"][league]["p2_ready"] is True
            continue
        assert set(current["leagues"][league]["sources"]) == expected
        assert current["leagues"][league]["p2_ready"] is False
        assert current["leagues"][league]["p2_evidence"]["p2_ready"] is False
        assert "source_corpus_changed" in current["leagues"][league]["p2_evidence"]["reasons"]
    for league, fields in baseline["leagues"].items():
        assert {key: current["leagues"][league][key] for key in fields} == fields
