from itertools import combinations, product
from math import prod

import pytest

_PERCENTAGES = [
    (90, 6, 4),
    (82, 12, 6),
    (72, 18, 10),
    (66, 21, 13),
    (62, 28, 10),
    (58, 25, 17),
    (54, 30, 16),
    (52, 30, 18),
    (50, 28, 22),
    (41, 40, 19),
]


def _rows(percentages):
    return [
        {
            "seq": seq,
            "probabilities": {key: value / 100 for key, value in zip("hda", values, strict=True)},
        }
        for seq, values in enumerate(percentages, 1)
    ]


def test_pick_nine_optimizer_matches_joint_exhaustive_search():
    from zucai.pick_nine import optimize_pick_nine

    def products(indices):
        for sizes in product((1, 2, 3), repeat=9):
            if prod(sizes) <= 8:
                yield prod(
                    sum(sorted(_PERCENTAGES[index], reverse=True)[:size])
                    for index, size in zip(indices, sizes, strict=True)
                )

    n = len(_PERCENTAGES)
    expected = max(value for indices in combinations(range(n), 9) for value in products(indices))
    result = optimize_pick_nine(_rows(_PERCENTAGES), max_lines=8)
    assert result["coverage_probability"] == pytest.approx(expected / 100**9)
    assert result["coverage_probability"] > max(products(tuple(range(9)))) / 100**9
    assert len(result["selections"]) == 9
    assert 10 in [selection["seq"] for selection in result["selections"]]


def test_one_line_matches_existing_single_order_and_outcome_tie_break():
    from zucai.pick_nine import optimize_pick_nine

    rows = _rows([(40, 40, 20)] * 7 + [(35, 35, 30), (35, 30, 35), (25, 30, 45)])
    single = sorted(rows, key=lambda r: (-max(r["probabilities"].values()), r["seq"]))[:9]
    single.sort(key=lambda r: r["seq"])
    outcomes = [max("hda", key=row["probabilities"].__getitem__) for row in single]
    result = optimize_pick_nine(list(reversed(rows)), max_lines=1)
    assert result["lines"] == 1
    assert [row["seq"] for row in result["selections"]] == [row["seq"] for row in single]
    assert [row["outcomes"] for row in result["selections"]] == [[key] for key in outcomes]
    assert result["coverage_probability"] == pytest.approx(
        prod(row["probabilities"][key] for row, key in zip(single, outcomes, strict=True))
    )


def test_default_budget_uses_only_highest_probability_outcomes_and_is_replayable():
    from zucai.pick_nine import optimize_pick_nine

    rows = _rows([(23, 43, 34), (38, 24, 38), (34, 33, 33)] * 4 + [(44, 18, 38)] * 2)
    result = optimize_pick_nine(rows)
    assert result == optimize_pick_nine(list(reversed(rows)))
    assert result["max_lines"] == 64
    assert 1 < result["lines"] <= 64
    choices = result["selections"]
    assert len(choices) == 9
    assert result["lines"] == prod(len(selection["outcomes"]) for selection in choices)
    assert result["coverage_probability"] == pytest.approx(
        prod(selection["probability"] for selection in choices)
    )
    for selection in choices:
        probs = rows[selection["seq"] - 1]["probabilities"]
        ranked = sorted("hda", key=lambda key: -probs[key])
        assert selection["outcomes"] == ranked[: len(selection["outcomes"])]
        assert selection["probability"] == pytest.approx(
            sum(probs[k] for k in selection["outcomes"])
        )


def test_equal_coverage_prefers_fewer_lines_and_then_lower_slot_numbers():
    from zucai.pick_nine import optimize_pick_nine

    result = optimize_pick_nine(_rows([(100, 0, 0)] * 14))
    assert result["lines"] == result["coverage_probability"] == 1
    assert [selection["seq"] for selection in result["selections"]] == list(range(1, 10))


@pytest.mark.parametrize("cap", [0, -1, True, 8.0, 3**9 + 1])
def test_invalid_budgets_are_refused(cap):
    from zucai.pick_nine import validate_pick_nine_budget

    with pytest.raises(ValueError, match="注数上限"):
        validate_pick_nine_budget(cap)


def test_insufficient_or_duplicate_candidates_are_refused():
    from zucai.pick_nine import optimize_pick_nine

    rows = _rows(_PERCENTAGES)
    with pytest.raises(ValueError, match="9至14"):
        optimize_pick_nine(rows[:8])
    with pytest.raises(ValueError, match="场号"):
        optimize_pick_nine(rows[:9] + [rows[0]])
