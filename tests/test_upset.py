import copy

import pytest

from zucai.upset import assess, cell


@pytest.mark.parametrize(
    ("values", "chance", "favorite", "alternative", "tier"),
    [
        ((0.50, 0.30, 0.20), 0.50, "h", "d", "中"),
        ((0.44, 0.30, 0.26), 0.56, "h", "d", "高"),
        ((0.45, 0.30, 0.25), 0.55, "h", "d", "中"),
        ((0.60, 0.25, 0.15), 0.40, "h", "d", "低"),
        ((0.40, 0.40, 0.20), 0.60, "h", "d", "高"),
        ((0.20, 0.40, 0.40), 0.60, "d", "a", "高"),
        ((0.24, 0.24, 0.52), 0.48, "a", "h", "中"),
        ((0.449999, 0.30, 0.250001), 0.550001, "h", "d", "高"),
        ((0.599999, 0.25, 0.150001), 0.400001, "h", "d", "中"),
    ],
)
def test_upset_uses_frozen_probabilities_and_unrounded_tier(
    values, chance, favorite, alternative, tier
):
    # Deliberately serialize in a different order so ties follow h/d/a instead of insertion.
    row = {"model_probabilities": dict(zip("adh", reversed(values), strict=True))}
    assert assess(row) == {
        "probability": pytest.approx(chance),
        "favorite": favorite,
        "alternative": alternative,
        "tier": tier,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("context", "sfc", "reason"),
    [
        ({"metadata": {"away_unknown_team": True}}, False, "使用默认实力"),
        ({"prior_disclosure": {"used": True}}, True, "使用默认实力"),
        (
            {"identity_pending": {"away": {"name": "球队甲", "other": "球队甲英文名"}}},
            False,
            "身份待核",
        ),
        ({"mapping": "ambiguous", "match_id": None}, True, "身份待核"),
        ({"mapping": "unmatched", "match_id": None}, True, "身份待核"),
        (
            {"kickoff_basis": "placeholder", "mapping": "unmatched", "match_id": None},
            True,
            "占位开球时间",
        ),
        ({"model_probabilities": None}, False, "没有模型概率"),
        ({"probabilities": None}, True, "没有模型概率"),
    ],
)
def test_upset_exclusions_have_a_reason_and_no_tier(context, sfc, reason):
    row = {
        "match_id": "jc:one",
        "model_probabilities": {"h": 0.50, "d": 0.30, "a": 0.20},
        "probabilities": {"h": 0.50, "d": 0.30, "a": 0.20},
        **context,
    }
    assert assess(row, sfc=sfc) == {
        "probability": None,
        "favorite": None,
        "alternative": None,
        "tier": None,
        "reason": reason,
    }
    assert cell(row, sfc=sfc) == f"不评（{reason}）"


def test_sfc_resolved_own_fixture_is_eligible_when_legacy_bridge_is_unmatched():
    kickoff = "2026-11-20T11:45:00+00:00"
    identity = dict(home="甲队", away="乙队", league="英冠", source="c500", kickoff_utc=kickoff)
    row = dict(
        fixture_id="789",
        match_id="c500:789",
        mapping="unmatched",
        matched_identity=identity,
        league="英冠",
        kickoff_utc=kickoff,
        probabilities={"h": 0.50, "d": 0.30, "a": 0.20},
    )
    assert cell(row, sfc=True) == "50.00% · 最可能冷在平 · 中"

    for changes in (
        {"match_id": "c500:790"},
        {"mapping": "ambiguous"},
        {"matched_identity": None},
        {"matched_identity": {**identity, "source": "jc"}},
        {"matched_identity": {**identity, "kickoff_utc": "2026-11-20T11:46:00+00:00"}},
        {"mapping_evidence": {"basis": "conflicting_identity"}},
        {"identity_pending": {"home": {"name": "甲队", "other": "甲队英文名"}}},
    ):
        assert cell({**row, **changes}, sfc=True) == "不评（身份待核）"


@pytest.mark.parametrize(
    "probabilities",
    [
        {"h": 0.5, "d": 0.5},
        {"h": 0.5, "d": 0.5, "a": float("nan")},
        {"h": float("inf"), "d": 0.5, "a": 0.5},
        {"h": 10**1000, "d": 0.5, "a": 0.5},
        {"h": "0.5", "d": 0.3, "a": 0.20},
        {"h": 0.7, "d": 0.30, "a": 0.20},
        {"h": True, "d": 0, "a": 0},
    ],
)
def test_upset_rejects_invalid_frozen_triplets(probabilities):
    assert cell({"model_probabilities": probabilities}) == "不评（没有模型概率）"


def test_only_matching_model_fields_can_supply_probabilities_and_replay_is_identical():
    market = {"h": 0.1, "d": 0.2, "a": 0.7}
    row = {
        "market": {"probabilities": market},
        "market_reference": {"probabilities": market},
        "experimental_blend": {"probabilities": market},
    }
    assert cell(row) == cell(row, sfc=True) == "不评（没有模型概率）"

    row["model_probabilities"] = {"h": 0.50, "d": 0.30, "a": 0.20}
    original = copy.deepcopy(row)
    first = cell(row)
    assert first == cell(row) == "50.00% · 最可能冷在平 · 中"
    assert row == original
    assert cell(row, sfc=True) == "不评（没有模型概率）"

    row["probabilities"] = {"h": 0.15, "d": 0.60, "a": 0.25}
    assert cell(row) == first
    assert cell(row, sfc=True) == "40.00% · 最可能冷在客胜 · 低"
