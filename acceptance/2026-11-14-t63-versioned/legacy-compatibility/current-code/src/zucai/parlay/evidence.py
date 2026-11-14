"""Paper-only evidence states and counterfactual filters, never probability adjustments."""

from collections import Counter
from decimal import Decimal

from zucai.scoreboard.score import instant


def _before(value, cutoff):
    try:
        return bool(value) and instant(value) < instant(cutoff)
    except (TypeError, ValueError):
        return False


def injury_evidence(injury, cutoff):
    injury = injury or {}
    result = {}
    for side in ("home", "away"):
        original = injury.get(side) or {}
        value = original.get("confirmed_injured_out")
        valid = (
            type(value) is int
            and value >= 0
            and not original.get("missing_reason")
            and _before(original.get("captured_utc"), cutoff)
        )
        result[side] = {
            **original,
            "confirmed_injured_out": value if valid else None,
            "status": "confirmed_absences"
            if valid and value
            else "confirmed_none"
            if valid
            else "unknown",
            "missing_reason": None
            if valid
            else original.get("missing_reason") or "no_verified_list_before_cutoff",
        }
    evidence = injury.get("research_invalidation") or {}
    invalidated = (
        isinstance(evidence.get("reason"), str)
        and bool(evidence["reason"].strip())
        and isinstance(evidence.get("source"), str)
        and bool(evidence["source"].strip())
        and _before(evidence.get("observed_utc"), cutoff)
    )
    result["research_invalidation"] = evidence if invalidated else None
    result["coverage"] = (
        "missing" if any(result[s]["status"] == "unknown" for s in ("home", "away")) else "complete"
    )
    result["status"] = "research_invalidated" if invalidated else result["coverage"]
    return result


def legacy_filter_diagnostic(boards, config):
    """Evaluate removed v1 filters on exactly the prepared inputs; no ranking mutation."""
    matches, restored, leagues = [], [], {}
    for board in boards:
        reasons = {}
        for row in board["matches"]:
            single, double = [], []
            if row["single"]["direction_gap"] < config.single_direction_gap:
                single.append("legacy_single_gap")
            if row["injuries"] and row["injuries"]["coverage"] == "missing":
                single.append("legacy_injuries_missing")
            if Decimal(str(row["double"]["selected_min_minus_omitted"])) <= Decimal(
                str(config.double_fragile_gap)
            ):
                double.append("legacy_double_gap")
            reasons[row["match_id"]] = dict(single=single, double=double)
            if not single and not double:
                continue
            matches.append(
                dict(
                    match_id=row["match_id"],
                    league=row["league"],
                    board=board["name"],
                    old_exclusions=dict(single=single, double=double),
                    restored_roles=[
                        role
                        for role, why in (("single", single), ("double", double))
                        if why
                        and row[role]["eligible"]
                        and row["eligibility"]["status"] == "eligible"
                    ],
                )
            )
            counts = leagues.setdefault(
                row["league"], dict(affected_matches=0, restored_groups=0, reasons=Counter())
            )
            counts["affected_matches"] += 1
            counts["reasons"].update(single + double)
        for group in board["candidates"]:
            why = (
                reasons[group["single"]["match_id"]]["single"]
                + reasons[group["double"]["match_id"]]["double"]
            )
            if not why:
                continue
            restored.append(
                dict(
                    group_id=group["group_id"],
                    match_ids=group["match_ids"],
                    board=board["name"],
                    reasons=why,
                )
            )
            for league in {group[side]["league"] for side in ("single", "double")}:
                leagues.setdefault(
                    league, dict(affected_matches=0, restored_groups=0, reasons=Counter())
                )["restored_groups"] += 1
    return dict(
        status="未经验证的实验筛选参数；仅诊断旧筛选影响，不代表准确度或收益改善",
        affects_selection=False,
        parameters=dict(
            single_direction_gap=config.single_direction_gap,
            double_fragile_gap=config.double_fragile_gap,
        ),
        matches=matches,
        restored_groups=restored,
        leagues={k: {**v, "reasons": dict(v["reasons"])} for k, v in sorted(leagues.items())},
        counting_basis="比赛按主榜计一次；恢复组合指可参加排序的有序配对，非新增注单。跨联赛组合在涉及的每个联赛各计一次。",
    )
