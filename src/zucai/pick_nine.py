"""Optimize rectangular coverage from frozen team-model probabilities."""

from fractions import Fraction
from itertools import accumulate

from zucai.report.render import OUTCOMES, _triplet

DEFAULT_PICK_NINE_MAX_LINES = 64
PICK_NINE_RULE_VERSION = 2
_MATCH_COUNT = 9
_PROBABILITY_SCALE = 1_000_000


def validate_pick_nine_budget(max_lines):
    if type(max_lines) is not int or not 1 <= max_lines <= 3**_MATCH_COUNT:
        raise ValueError("任九注数上限须为 1 至 19683 的整数")


def optimize_pick_nine(
    rows, max_lines=DEFAULT_PICK_NINE_MAX_LINES, *, rule_version=PICK_NINE_RULE_VERSION
):
    """Maximize the probability all nine results are covered, under independence."""
    validate_pick_nine_budget(max_lines)
    if not _MATCH_COUNT <= len(rows) <= 14:
        raise ValueError("任九须提供9至14场可用预测")
    sequences = [row["seq"] for row in rows]
    if any(type(seq) is not int or not 1 <= seq <= 14 for seq in sequences) or len(
        set(sequences)
    ) != len(rows):
        raise ValueError("任九场号必须为 1 至 14 的唯一整数")
    if rule_version not in (1, PICK_NINE_RULE_VERSION):
        raise ValueError("不支持的任九计算规则")
    return optimize_match_coverage(rows, _MATCH_COUNT, max_lines, legacy=rule_version == 1)


def _match_options(rows, legacy):
    options = {}
    for row in sorted(rows, key=lambda row: row["seq"]):
        probabilities = row["probabilities"]
        _triplet(probabilities, "球队模型", probabilities=True)
        precise = {key: Fraction(str(probabilities[key])) for key in OUTCOMES}
        if legacy:
            scale = 1
        else:
            scale = _PROBABILITY_SCALE
            precise = {key: round(value * scale) for key, value in precise.items()}
        ranked = sorted(OUTCOMES, key=precise.__getitem__, reverse=True)
        masses = [min(scale, total) for total in accumulate(precise[key] for key in ranked)]
        if not legacy:
            # All three possible results are covered even if their rounded sum is < 1.
            masses[2] = scale
        options[row["seq"]] = ranked, masses
    return options


def _tie_key(choices, options):
    triples = tuple(seq for seq, size in choices if size == 3)
    doubles = tuple(seq for seq, size in choices if size == 2)
    return (
        tuple(sorted(options[seq][1][0] for seq in triples)),
        tuple(sorted(options[seq][1][0] for seq in doubles)),
        triples,
        doubles,
        tuple(seq for seq, _ in choices),
    )


def _legacy_tie_key(choices):
    # Preserve the original ordering for reports frozen before T102.
    return tuple(seq for seq, _ in choices), tuple(size for _, size in choices)


def optimize_match_coverage(rows, match_count, max_lines, *, legacy=False):
    """Choose a fixed number of matches and maximize their joint coverage."""
    options = _match_options(rows, legacy)
    can_skip = len(rows) > match_count
    # At fixed match count and lines, a higher exact product always dominates.
    # The line product 2^d * 3^t fixes both group sizes. Each sorted component
    # of the tie key therefore keeps its order when both plans add the same legs.
    states = {(0, 1): (1, (), ())}
    for seq, (_, masses) in options.items():
        updated = states.copy() if can_skip else {}
        for (count, lines), (coverage, choices, _) in states.items():
            if count == match_count:
                continue
            for size, mass in enumerate(masses, 1):
                total_lines = lines * size
                if total_lines > max_lines:
                    continue
                key = count + 1, total_lines
                candidate_coverage = coverage * mass
                previous = updated.get(key)
                if previous is not None and candidate_coverage < previous[0]:
                    continue
                candidate = (*choices, (seq, size))
                tie_key = _legacy_tie_key(candidate) if legacy else _tie_key(candidate, options)
                if previous is None or candidate_coverage > previous[0] or tie_key < previous[2]:
                    updated[key] = candidate_coverage, candidate, tie_key
        states = updated

    lines, (coverage, choices, _) = max(
        ((lines, state) for (count, lines), state in states.items() if count == match_count),
        key=lambda item: (item[1][0], -item[0]),
    )
    scale = 1 if legacy else _PROBABILITY_SCALE
    return {
        "lines": lines,
        "max_lines": max_lines,
        "coverage_probability": float(coverage / scale**match_count),
        "selections": [
            {
                "seq": seq,
                "outcomes": options[seq][0][:size],
                "probability": float(options[seq][1][size - 1] / scale),
            }
            for seq, size in choices
        ],
        "assumption": "cross_match_independence",
    }
