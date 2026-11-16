"""First-half goal share for the half-time/full-time derivation.

The share is estimated exclusively from the model's own training rows (the
rows the fit actually consumes, same cutoff and same availability rules).
Market information never enters this module.
"""

from __future__ import annotations

import math

MIN_HT_MATCHES = 300
HAFU_SELECTIONS = tuple(h + f for h in "hda" for f in "hda")
HAFU_LABELS = {
    "hh": "胜/胜",
    "hd": "胜/平",
    "ha": "胜/负",
    "dh": "平/胜",
    "dd": "平/平",
    "da": "平/负",
    "ah": "负/胜",
    "ad": "负/平",
    "aa": "负/负",
}
HAFU_TIER_LABELS = {"league": "联赛", "pool": "池", "all": "全部模型"}


def _integer(value):
    """Accept integer or integral float; None/NaN/infinity/negatives are invalid."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number < 0 or number != int(number):
        return None
    return int(number)


def share_from_rows(rows) -> dict | None:
    """Sum half-time and full-time goals over rows carrying valid half scores.

    Rows whose half-time scores are missing or invalid are not counted;
    goalless draws still count toward the sample size.
    """
    ht_goals = 0
    full_goals = 0
    matches = 0
    for row in rows:
        ht_h = _integer(row.get("ht_home_goals"))
        ht_a = _integer(row.get("ht_away_goals"))
        if ht_h is None or ht_a is None:
            continue
        full_h = _integer(row.get("home_goals"))
        full_a = _integer(row.get("away_goals"))
        if full_h is None or full_a is None or full_h + full_a < ht_h + ht_a:
            continue
        ht_goals += ht_h + ht_a
        full_goals += full_h + full_a
        matches += 1
    if not matches or full_goals == 0:
        return None
    return dict(
        share=float(ht_goals) / float(full_goals),
        ht_matches=matches,
        ht_goals=ht_goals,
        full_goals=full_goals,
    )


def estimate_share(own_rows, pool_rows=(), all_rows=()) -> dict:
    """Tiered share: own training rows, then the model's pool, then all models.

    Output carries the tier used, the share, and the sample size so the report
    can disclose exactly what the derivation is based on. No artificial
    constant is ever substituted.
    """
    own = share_from_rows(own_rows)
    if own is not None and own["ht_matches"] >= MIN_HT_MATCHES:
        return dict(tier="league", **own)
    pool = share_from_rows(pool_rows)
    if pool is not None and pool["ht_matches"] >= MIN_HT_MATCHES:
        return dict(tier="pool", **pool)
    combined = share_from_rows(all_rows)
    if combined is not None:
        return dict(tier="all", **combined)
    raise ValueError("没有带半场比分的训练行可估计半场进球占比")


def combine_shares(parts):
    """Aggregate per-league share samples into one joint sample."""
    present = [p for p in parts if p is not None]
    if not present:
        return None
    ht_matches = sum(p["ht_matches"] for p in present)
    ht_goals = sum(p["ht_goals"] for p in present)
    full_goals = sum(p["full_goals"] for p in present)
    if not ht_matches or full_goals == 0:
        return None
    return dict(
        share=float(ht_goals) / float(full_goals),
        ht_matches=ht_matches,
        ht_goals=ht_goals,
        full_goals=full_goals,
    )


def tiered_share(own, combined, *, pooled=False) -> dict | None:
    """Daily-run tiers over pre-aggregated samples.

    Domestic models have no pool: below 300 half-scored training matches they
    fall back directly to the combined sample of every model in the run. The
    pooled model's own rows already are its pool, so a sufficient own sample
    is labelled ``pool``.
    """
    if pooled:
        if own is not None and own["ht_matches"] >= MIN_HT_MATCHES:
            return dict(tier="pool", **own)
    elif own is not None and own["ht_matches"] >= MIN_HT_MATCHES:
        return dict(tier="league", **own)
    if combined is not None:
        return dict(tier="all", **combined)
    return None


def validate_share(value) -> float:
    share = float(value)
    if not math.isfinite(share) or not 0 < share < 1:
        raise ValueError("半场进球占比须为 0 与 1 之间的有限数值")
    return share


def ht_outcome(row) -> str | None:
    """Half-time outcome code for a settled row with half-time scores."""
    return ht_outcome_from_scores(row.get("ht_home_goals"), row.get("ht_away_goals"))


def ht_outcome_from_scores(ht_h, ht_a) -> str | None:
    """Half-time outcome; missing half scores yield None, floats are accepted.

    pandas promotes a mixed half-score column to float when a single row lacks
    the half score, so every caller must tolerate integral floats and NaN.
    """
    ht_h, ht_a = _integer(ht_h), _integer(ht_a)
    if ht_h is None or ht_a is None:
        return None
    if ht_h == ht_a:
        return "d"
    return "h" if ht_h > ht_a else "a"
