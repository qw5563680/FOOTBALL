from copy import deepcopy
from itertools import combinations, product
from math import prod

import pytest

from zucai.pick_nine import optimize_pick_nine
from zucai.report.sfc import optimize_cover, pick_nine_multi_lines, pick_nine_multi_plan

SCALE = 1_000_000
CERTAIN = (SCALE, 0, 0)


def _rows(millionths):
    return [
        {
            "seq": seq,
            "probabilities": {
                outcome: value / SCALE for outcome, value in zip("hda", probabilities, strict=True)
            },
            "home": f"主队{seq}",
            "away": f"客队{seq}",
            "kickoff_utc": "2026-11-28T18:00:00+00:00",
        }
        for seq, probabilities in enumerate(millionths, 1)
    ]


def _report(rows, cap):
    deadline = "2026-11-28T16:00:00+00:00"
    return {
        "rows": rows,
        "published_utc": "2026-11-28T12:00:00+00:00",
        "sale_state": "published_before_deadline",
        "sale_end_utc": deadline,
        "source": {"sale_end_utc": deadline},
        "mode": "prematch",
        "pick_nine_max_lines": cap,
        "pick_nine_rule": 2,
    }


def _multi_sequences(plan, size):
    return [item["seq"] for item in plan["selections"] if len(item["outcomes"]) == size]


@pytest.mark.parametrize(
    ("uncertain", "cap", "expected_multi"),
    [
        (
            [(450_000, 350_000, 200_000), (800_000, 150_000, 50_000), (400_000, 350_000, 250_000)],
            3,
            10,
        ),
        (
            [(450_000, 350_000, 200_000), (800_000, 150_000, 50_000), (450_000, 330_000, 220_000)],
            3,
            8,
        ),
        (
            [(500_000, 300_000, 200_000), (800_000, 100_000, 100_000), (430_000, 370_000, 200_000)],
            2,
            10,
        ),
    ],
)
def test_matches_brute_force_and_prefers_lower_confidence_then_slot(uncertain, cap, expected_multi):
    millionths = [CERTAIN] * 7 + uncertain
    expected_coverage = max(
        prod(sum(sorted(row, reverse=True)[:size]) for row, size in zip(chosen, sizes, strict=True))
        for chosen in combinations(millionths, 9)
        for sizes in product((1, 2, 3), repeat=9)
        if prod(sizes) <= cap
    )
    plan = optimize_pick_nine(_rows(millionths), cap)
    assert plan["coverage_probability"] == expected_coverage / SCALE**9
    assert plan["lines"] == cap
    assert _multi_sequences(plan, cap) == [expected_multi]


def test_exact_coverage_wins_before_tie_breaking():
    rows = _rows([CERTAIN] * 7 + [(550_001, 275_000, 174_999), (549_999, 274_999, 175_002)])
    # Doubling the more certain match adds exactly one unit to the scaled product.
    plan = optimize_pick_nine(rows, 2)
    assert plan["coverage_probability"] == 825_001 * 549_999 / SCALE**2
    assert _multi_sequences(plan, 2) == [8]


def test_equal_confidence_with_the_same_nine_matches_prefers_lower_full_cover_slot():
    rows = _rows([CERTAIN] * 7 + [(420_000, 340_000, 240_000)] * 2)
    assert _multi_sequences(optimize_pick_nine(rows, 3), 3) == [8]


def test_full_cover_is_certainty_after_rounding_each_outcome():
    rows = _rows([CERTAIN] * 8 + [(333_333, 333_333, 333_334)])
    rows[-1]["probabilities"] = {"h": 0.3333334, "d": 0.3333333, "a": 0.3333333}
    plan = optimize_pick_nine(rows, 3)
    assert plan["coverage_probability"] == 1.0
    assert plan["selections"][-1] == {"seq": 9, "outcomes": ["h", "d", "a"], "probability": 1.0}


def test_decimal_half_even_rounding_precedes_ranking():
    rows = _rows([CERTAIN] * 8 + [(400_000, 400_000, 200_000)])
    rows[-1]["probabilities"] = {"h": 0.3999995, "d": 0.4000005, "a": 0.2}
    assert optimize_pick_nine(rows, 1)["selections"][-1] == {
        "seq": 9,
        "outcomes": ["h"],
        "probability": 0.4,
    }


def test_display_comparison_uses_six_decimals_at_percent_boundary():
    rows = _rows([CERTAIN] * 8 + [(500_050, 299_950, 200_000)])
    report = _report(rows, 1)
    jittered = deepcopy(report)
    jittered["rows"][-1]["probabilities"]["h"] -= 1e-8
    jittered["rows"][-1]["probabilities"]["d"] += 1e-8
    assert pick_nine_multi_lines(jittered) == pick_nine_multi_lines(report)


def test_frozen_report_uses_its_original_plan_for_display_and_replay():
    rows = _rows(
        [CERTAIN] * 7
        + [(450_000, 350_000, 200_000), (800_000, 150_000, 50_000), (400_000, 350_000, 250_000)]
    )
    current = _report(rows, 3)
    legacy = deepcopy(current)
    legacy.pop("pick_nine_rule")
    assert _multi_sequences(pick_nine_multi_plan(legacy), 3) == [8]
    assert _multi_sequences(pick_nine_multi_plan(current), 3) == [10]
    current_text = "\n".join(pick_nine_multi_lines(current))
    legacy_text = "\n".join(pick_nine_multi_lines(legacy))
    assert "全包优先放在最没把握的场次上" in current_text
    assert "按相同精度计算的一注方案九场全中概率" in current_text
    assert "全包优先放在最没把握的场次上" not in legacy_text
    assert "同一期单选九场全中概率" in legacy_text


def test_fourteen_match_cover_has_the_same_stable_rule():
    probabilities = [dict(h=1, d=0, a=0)] * 12
    p = dict(h=0.42, d=0.34, a=0.24)
    q = dict(h=0.42000002, d=0.33999998, a=0.24)
    plan = optimize_cover([*probabilities, p, q], 3)
    assert optimize_cover([*probabilities, q, p], 3) == plan
    assert plan["selections"] == [["h"]] * 12 + [["h", "d", "a"], ["h"]]
    assert plan["coverage_probability"] == 0.42
    assert plan["lines"] == 3
