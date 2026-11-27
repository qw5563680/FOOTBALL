import json
from copy import deepcopy
from pathlib import Path
from random import Random

from zucai.report.sfc import optimize_cover, pick_nine_multi_lines, pick_nine_multi_plan

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data/fixtures/t102_pick_nine_26172.json"


def _reports():
    return [archive["report"] for archive in json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))]


def test_new_rule_makes_the_two_archived_pick_nine_plans_identical():
    first, second = [{**report, "pick_nine_rule": 2} for report in _reports()]
    plan = pick_nine_multi_plan(first)

    assert plan == pick_nine_multi_plan(second)
    assert plan["lines"] == 54
    assert plan["coverage_probability"] == 0.08014875118654977
    assert [(pick["seq"], "".join(pick["outcomes"])) for pick in plan["selections"]] == [
        (1, "ahd"),
        (4, "had"),
        (6, "a"),
        (7, "h"),
        (8, "had"),
        (9, "h"),
        (11, "a"),
        (12, "h"),
        (13, "ah"),
    ]
    assert pick_nine_multi_lines(first) == pick_nine_multi_lines(second)


def test_fifty_sub_micro_jitters_preserve_the_archived_plan_and_render():
    report = {**_reports()[0], "pick_nine_rule": 2}
    expected_plan = pick_nine_multi_plan(report)
    expected_lines = pick_nine_multi_lines(report)
    random = Random(26172)

    for _ in range(50):
        jittered = deepcopy(report)
        for row in jittered["rows"]:
            probabilities = row["probabilities"]
            if probabilities is None:
                continue
            home_delta, draw_delta = [random.randint(-20, 20) * 1e-10 for _ in range(2)]
            deltas = (home_delta, draw_delta, -home_delta - draw_delta)
            for outcome, delta in zip("hda", deltas, strict=True):
                original = probabilities[outcome]
                probabilities[outcome] += delta
                assert round(probabilities[outcome], 6) == round(original, 6)
        assert pick_nine_multi_plan(jittered) == expected_plan
        assert pick_nine_multi_lines(jittered) == expected_lines


def test_pick_nine_replay_preserves_the_two_frozen_legacy_plans():
    for report, full_cover, probability in zip(
        _reports(),
        [(2, 3, 8), (1, 3, 4)],
        [0.08014882870862573, 0.08014882893418328],
        strict=True,
    ):
        plan = pick_nine_multi_plan(report)
        assert plan["lines"] == 54
        assert plan["coverage_probability"] == probability
        assert tuple(pick["seq"] for pick in plan["selections"] if len(pick["outcomes"]) == 3) == (
            full_cover
        )


def test_sfc14_replay_keeps_each_archived_selection_with_a_forced_missing_match():
    for report in _reports():
        probabilities = [row["probabilities"] for row in report["rows"]]
        assert probabilities[-1] is None
        forced_lines = report["minimum_lines"]
        choice = optimize_cover(probabilities[:-1], report["max_lines"] // forced_lines)
        assert choice["lines"] * forced_lines == report["combo"]["lines"]
        assert [*choice["selections"], ["h", "d", "a"]] == report["combo"]["selections"]
        assert choice["coverage_probability"] == 0.0015071142106469633
