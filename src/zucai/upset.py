"""Read-only upset diagnostics derived from one frozen team-model row."""

from zucai.digest_display import _number
from zucai.identity_pending import identity_pending
from zucai.prediction_disclosure import uses_unknown_team_prior
from zucai.report.render import OUTCOMES, _triplet

HIGH_FAVORITE_LIMIT = 0.45
LOW_FAVORITE_MINIMUM = 0.60
TIERS = ("高", "中", "低")
OUTCOME_LABELS = dict(h="主胜", d="平", a="客胜")


def _legacy_own_fixture(row):
    """Legacy c500 imports used 'unmatched' to mean no cross-source link."""
    identity = row.get("matched_identity")
    fixture_id = row.get("fixture_id")
    return (
        isinstance(identity, dict)
        and isinstance(fixture_id, str)
        and fixture_id.isdigit()
        and row.get("match_id") == "c500:" + fixture_id
        and identity.get("source") == "c500"
        and all(identity.get(key) for key in ("home", "away", "league", "kickoff_utc"))
        and identity["home"] != identity["away"]
        and all(identity[key] == row.get(key) for key in ("league", "kickoff_utc"))
    )


def _exclusion(row, sfc):
    if row.get("kickoff_basis") == "placeholder":
        return "占位开球时间"
    mapping = row.get("mapping")
    evidence = row.get("mapping_evidence")
    if (
        identity_pending(row)
        or mapping == "ambiguous"
        or (isinstance(evidence, dict) and evidence.get("basis") == "conflicting_identity")
        or (mapping == "unmatched" and not (sfc and _legacy_own_fixture(row)))
        or (sfc and "match_id" in row and row["match_id"] is None)
    ):
        return "身份待核"
    if uses_unknown_team_prior(row):
        return "使用默认实力"


def assess(row, *, sfc=False):
    """Return the fixed tier and the two leading outcomes in h/d/a tie order."""
    values = row.get("probabilities" if sfc else "model_probabilities")
    reason = _exclusion(row, sfc)
    if not reason:
        try:
            _triplet(values, "球队模型", probabilities=True)
        except (ValueError, OverflowError):
            reason = "没有模型概率"
    if reason:
        return dict(probability=None, favorite=None, alternative=None, tier=None, reason=reason)

    favorite, alternative, _ = sorted(OUTCOMES, key=values.__getitem__, reverse=True)
    largest = values[favorite]
    if largest < HIGH_FAVORITE_LIMIT:
        tier = "高"
    elif largest < LOW_FAVORITE_MINIMUM:
        tier = "中"
    else:
        tier = "低"
    return dict(
        probability=1 - largest, favorite=favorite, alternative=alternative, tier=tier, reason=None
    )


def cell(row, *, sfc=False):
    assessment = assess(row, sfc=sfc)
    if assessment["reason"]:
        return f"不评（{assessment['reason']}）"
    percentage = _number(assessment["probability"], 2, percent=True)
    alternative = OUTCOME_LABELS[assessment["alternative"]]
    return f"{percentage} · 最可能冷在{alternative} · {assessment['tier']}"
