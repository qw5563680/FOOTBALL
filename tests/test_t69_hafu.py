"""T69 half-time/full-time derivation: consistency, legacy equality, tiered share."""

import math
from math import comb

import numpy as np
import pytest

from zucai.predict.derive import had, hafu
from zucai.predict.hafu_share import (
    MIN_HT_MATCHES,
    combine_shares,
    estimate_share,
    ht_outcome_from_scores,
    share_from_rows,
    tiered_share,
    validate_share,
)

OUTCOMES = ["h", "d", "a"]


def _matrix(seed=7):
    rng = np.random.default_rng(seed)
    grid = rng.random((9, 9))
    grid[0, 0] *= 3
    grid[1, 0] *= 2
    grid[0, 1] *= 2
    grid[1, 1] *= 2
    return grid / grid.sum()


def _legacy_hafu(matrix):
    grid = np.asarray(matrix, dtype=float)
    split = [[comb(n, k) * 0.5**n for k in range(n + 1)] for n in range(grid.shape[0])]
    values = {h + f: 0.0 for h in OUTCOMES for f in OUTCOMES}
    for (h, a), p in np.ndenumerate(grid):
        full = "d" if h == a else ("h" if h > a else "a")
        for ht_h, w_h in enumerate(split[h]):
            for ht_a, w_a in enumerate(split[a]):
                ht = "d" if ht_h == ht_a else ("h" if ht_h > ht_a else "a")
                values[ht + full] += float(p) * w_h * w_a
    return values


@pytest.mark.parametrize("share", [0.5, 0.42, 0.61, 0.27, 0.73])
def test_hafu_sums_to_one_and_preserves_full_time_margins(share):
    matrix = _matrix()
    values = hafu(matrix, share)
    assert set(values) == {h + f for h in OUTCOMES for f in OUTCOMES}
    assert all(0 <= v <= 1 for v in values.values())
    assert math.isclose(sum(values.values()), 1, rel_tol=0, abs_tol=1e-12)
    full = had(matrix)
    for f in OUTCOMES:
        merged = sum(v for k, v in values.items() if k[1] == f)
        assert math.isclose(merged, full[f], rel_tol=0, abs_tol=1e-12)


@pytest.mark.parametrize("share", [0.42, 0.61, 0.73])
def test_hafu_half_time_marginal_matches_thinned_grid(share):
    matrix = _matrix(seed=11)
    values = hafu(matrix, share)
    # Independent thinning: half-time grid = binomial mixture of the full grid.
    grid = np.asarray(matrix, dtype=float)
    thinned = np.zeros_like(grid)
    for (h, a), p in np.ndenumerate(grid):
        for ht_h in range(h + 1):
            for ht_a in range(a + 1):
                w = (
                    comb(h, ht_h)
                    * comb(a, ht_a)
                    * share ** (ht_h + ht_a)
                    * (1 - share) ** (h + a - ht_h - ht_a)
                )
                thinned[ht_h, ht_a] += p * w
    half = had(thinned)
    for hf in OUTCOMES:
        merged = sum(v for k, v in values.items() if k[0] == hf)
        assert math.isclose(merged, half[hf], rel_tol=0, abs_tol=1e-12)


def test_hafu_share_half_matches_legacy_bitwise():
    matrix = _matrix(seed=19)
    assert hafu(matrix, 0.5) == _legacy_hafu(matrix)


def test_hafu_rejects_invalid_share():
    matrix = _matrix()
    for bad in (0, 1, -0.1, 1.1, math.nan, math.inf):
        with pytest.raises(ValueError):
            hafu(matrix, bad)


def test_share_from_rows_uses_only_rows_with_half_scores():
    rows = [
        dict(ht_home_goals=1, ht_away_goals=0, home_goals=2, away_goals=1),
        dict(ht_home_goals=0, ht_away_goals=0, home_goals=0, away_goals=0),  # counted match
        dict(ht_home_goals=None, ht_away_goals=None, home_goals=3, away_goals=0),  # no half
        dict(ht_home_goals=-1, ht_away_goals=0, home_goals=1, away_goals=0),  # invalid
        dict(ht_home_goals=2, ht_away_goals=3, home_goals=1, away_goals=1),  # ht > full
        dict(ht_home_goals=1, ht_away_goals=1, home_goals=2, away_goals=2),
    ]
    result = share_from_rows(rows)
    assert result["ht_matches"] == 3
    assert result["ht_goals"] == 3
    assert result["full_goals"] == 7
    assert math.isclose(result["share"], 3 / 7)


def test_share_from_rows_accepts_integral_floats():
    rows = [
        dict(ht_home_goals=1.0, ht_away_goals=0.0, home_goals=2.0, away_goals=1.0),
        dict(ht_home_goals=float("nan"), ht_away_goals=0.0, home_goals=1.0, away_goals=0.0),
    ]
    result = share_from_rows(rows)
    assert result["ht_matches"] == 1
    assert result["share"] == 1 / 3


def test_ht_outcome_accepts_integral_floats_and_nan():
    assert ht_outcome_from_scores(2.0, 1.0) == "h"
    assert ht_outcome_from_scores(1, 1) == "d"
    assert ht_outcome_from_scores(0, 1) == "a"
    assert ht_outcome_from_scores(np.nan, 1.0) is None
    assert ht_outcome_from_scores(None, 0) is None
    assert ht_outcome_from_scores(0.5, 0) is None
    assert ht_outcome_from_scores(-1, 0) is None


def test_combine_and_tiered_share():
    a = dict(share=0.44, ht_matches=200, ht_goals=88, full_goals=200)
    b = dict(share=0.40, ht_matches=200, ht_goals=80, full_goals=200)
    combined = combine_shares([a, b])
    assert combined["ht_matches"] == 400
    assert math.isclose(combined["share"], 168 / 400)
    # Domestic model below threshold falls back to the combined sample.
    assert tiered_share(a, combined)["tier"] == "all"
    # Sufficient own sample wins for a domestic model.
    big = dict(share=0.46, ht_matches=MIN_HT_MATCHES, ht_goals=138, full_goals=300)
    assert tiered_share(big, combined)["tier"] == "league"
    # A pooled model's own rows are its pool.
    assert tiered_share(big, combined, pooled=True)["tier"] == "pool"
    # Nothing estimable anywhere.
    assert tiered_share(None, None) is None


def test_estimate_share_tiers_fallback():
    own = [dict(ht_home_goals=1, ht_away_goals=0, home_goals=2, away_goals=1)] * 10
    pool = [dict(ht_home_goals=1, ht_away_goals=1, home_goals=2, away_goals=2)] * 400
    combined = [dict(ht_home_goals=0, ht_away_goals=1, home_goals=1, away_goals=1)] * 500
    # Own below the threshold: fall back to the pool.
    assert estimate_share(own, pool, combined)["tier"] == "pool"
    # Own at the threshold wins.
    own = [dict(ht_home_goals=1, ht_away_goals=0, home_goals=2, away_goals=1)] * MIN_HT_MATCHES
    assert estimate_share(own, pool, combined)["tier"] == "league"
    # Pool insufficient too: everything combined.
    pool = [dict(ht_home_goals=1, ht_away_goals=1, home_goals=2, away_goals=2)] * 10
    assert estimate_share(own[:10], pool, combined)["tier"] == "all"


def test_estimate_share_requires_some_half_scores():
    with pytest.raises(ValueError):
        estimate_share([], [], [])


def test_validate_share():
    assert validate_share(0.5) == 0.5
    for bad in (0, 1, math.nan, math.inf, -1):
        with pytest.raises(ValueError):
            validate_share(bad)
