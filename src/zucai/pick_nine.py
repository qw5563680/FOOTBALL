"""Optimize nine-match coverage from frozen team-model probabilities."""

from fractions import Fraction
from itertools import accumulate

from zucai.report.render import OUTCOMES, _triplet

DEFAULT_PICK_NINE_MAX_LINES = 64
_MATCH_COUNT = 9


def validate_pick_nine_budget(max_lines):
    if type(max_lines) is not int or not 1 <= max_lines <= 3**_MATCH_COUNT:
        raise ValueError("任九注数上限须为 1 至 19683 的整数")


def optimize_pick_nine(rows, max_lines=DEFAULT_PICK_NINE_MAX_LINES):
    """Maximize the probability all nine results are covered, under independence."""
    validate_pick_nine_budget(max_lines)
    if not _MATCH_COUNT <= len(rows) <= 14:
        raise ValueError("任九须提供9至14场可用预测")
    sequences = [row["seq"] for row in rows]
    if any(type(seq) is not int or not 1 <= seq <= 14 for seq in sequences) or len(
        set(sequences)
    ) != len(rows):
        raise ValueError("任九场号必须为 1 至 14 的唯一整数")

    options = {}
    for row in sorted(rows, key=lambda row: row["seq"]):
        probabilities = row["probabilities"]
        _triplet(probabilities, "球队模型", probabilities=True)
        ranked = sorted(OUTCOMES, key=lambda key: -probabilities[key])
        # Compare products exactly using the decimals stored in the frozen JSON.
        mass = [
            min(1, total)
            for total in accumulate(Fraction(str(probabilities[key])) for key in ranked)
        ]
        options[row["seq"]] = ranked, mass

    # At a fixed match count and line product, higher coverage always dominates.
    # Copy the states so skipping this match remains a candidate too.
    states = {(0, 1): (Fraction(1), (), ())}
    for seq, (_, masses) in options.items():
        updated = states.copy()
        for (count, lines), (coverage, chosen, sizes) in states.items():
            if count == _MATCH_COUNT:
                continue
            for size, mass in enumerate(masses, 1):
                total_lines = lines * size
                if total_lines > max_lines:
                    continue
                key = count + 1, total_lines
                candidate = coverage * mass, (*chosen, seq), (*sizes, size)
                previous = updated.get(key)
                if (
                    previous is None
                    or candidate[0] > previous[0]
                    or (candidate[0] == previous[0] and candidate[1:] < previous[1:])
                ):
                    updated[key] = candidate
        states = updated

    lines, (coverage, chosen, sizes) = max(
        ((lines, state) for (count, lines), state in states.items() if count == _MATCH_COUNT),
        key=lambda item: (item[1][0], -item[0]),
    )
    return {
        "lines": lines,
        "max_lines": max_lines,
        "coverage_probability": float(coverage),
        "selections": [
            {
                "seq": seq,
                "outcomes": options[seq][0][:size],
                "probability": float(options[seq][1][size - 1]),
            }
            for seq, size in zip(chosen, sizes, strict=True)
        ],
        "assumption": "cross_match_independence",
    }
