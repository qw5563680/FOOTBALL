"""First-half goal share for the half-time/full-time derivation.

The share is estimated exclusively from training rows known before the model
cutoff (same kickoff/availability predicates as the Dixon-Coles fit). Market
information never enters this module.
"""

from __future__ import annotations

import math

import numpy as np

MIN_HT_MATCHES = 300


def share_from_rows(rows) -> dict | None:
    """Sum half-time and full-time goals over rows carrying valid half scores.

    Only rows whose half-time scores are non-negative integers and whose
    full-time goals are positive integers contribute; rows without half-time
    scores are simply not counted.
    """
    ht_goals = 0
    full_goals = 0
    matches = 0
    for row in rows:
        values = [row.get(name) for name in ("ht_home_goals", "ht_away_goals")]
        if any(v is None for v in values):
            continue
        try:
            ht_h, ht_a = int(values[0]), int(values[1])
            full_h, full_a = int(row["home_goals"]), int(row["away_goals"])
        except (TypeError, ValueError, KeyError):
            continue
        if ht_h < 0 or ht_a < 0 or full_h < 0 or full_a < 0:
            continue
        if full_h + full_a < ht_h + ht_a:
            continue
        if full_h + full_a == 0:
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


def validate_share(value) -> float:
    share = float(value)
    if not math.isfinite(share) or not 0 < share < 1:
        raise ValueError("半场进球占比须为 0 与 1 之间的有限数值")
    return share


def ht_outcome(row) -> str | None:
    """Half-time outcome code for a settled row with half-time scores."""
    try:
        ht_h, ht_a = int(row["ht_home_goals"]), int(row["ht_away_goals"])
    except (TypeError, ValueError, KeyError):
        return None
    if ht_h < 0 or ht_a < 0:
        return None
    if ht_h == ht_a:
        return "d"
    return "h" if ht_h > ht_a else "a"


def ht_outcome_from_scores(ht_h: int, ht_a: int) -> str | None:
    if not isinstance(ht_h, (int, np.integer)) or not isinstance(ht_a, (int, np.integer)):
        return None
    ht_h, ht_a = int(ht_h), int(ht_a)
    if ht_h < 0 or ht_a < 0:
        return None
    if ht_h == ht_a:
        return "d"
    return "h" if ht_h > ht_a else "a"
