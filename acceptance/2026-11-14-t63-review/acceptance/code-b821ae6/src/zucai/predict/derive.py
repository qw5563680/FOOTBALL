"""90 minutes plus stoppage time. All markets use the same normalized final grid."""

from math import comb
from numbers import Integral

import numpy as np


def _grid(matrix):
    grid = np.asarray(matrix, dtype=float)
    if (
        grid.ndim != 2
        or not grid.size
        or grid.shape[0] != grid.shape[1]
        or not np.isfinite(grid).all()
        or grid.min() < 0
        or not np.isclose(grid.sum(), 1, rtol=0, atol=1e-8)
    ):
        raise ValueError("需要归一化的方形比分矩阵")
    return grid


def _outcome(h, a):
    if h == a:
        return "d"
    return "h" if h > a else "a"


def hhad(matrix, line: int) -> dict[str, float]:
    """The given integer handicap is added to the home team's score."""
    if not isinstance(line, Integral):
        raise ValueError("竞彩让球线须为整数")
    grid = _grid(matrix)
    h, a = np.indices(grid.shape)
    return {
        "h": float(grid[h + line > a].sum()),
        "d": float(grid[h + line == a].sum()),
        "a": float(grid[h + line < a].sum()),
    }


def had(matrix):
    return hhad(matrix, 0)


def crs(matrix):
    grid = _grid(matrix)
    wins = [(h, a) for h in range(1, 6) for a in range(min(h, 3))]
    listed = wins + [(a, h) for h, a in wins] + [(g, g) for g in range(4)]
    values = {f"{h}:{a}": 0.0 for h, a in listed}
    others = {"h": "胜其他", "d": "平其他", "a": "负其他"}
    values.update(dict.fromkeys(others.values(), 0.0))
    for (h, a), p in np.ndenumerate(grid):
        label = f"{h}:{a}"
        values[label if label in values else others[_outcome(h, a)]] += float(p)
    return values


def ttg(matrix):
    grid = _grid(matrix)
    h, a = np.indices(grid.shape)
    values = {str(n): float(grid[h + a == n].sum()) for n in range(7)}
    return {**values, "7+": float(grid[h + a >= 7].sum())}


def hafu(matrix):
    """Approximate HT given FT by allocating each goal to either half with probability 1/2."""
    grid = _grid(matrix)
    split = [[comb(n, k) * 0.5**n for k in range(n + 1)] for n in range(grid.shape[0])]
    values = {half + full: 0.0 for half in "hda" for full in "hda"}
    for (h, a), p in np.ndenumerate(grid):
        full = _outcome(h, a)
        for ht_h, w_h in enumerate(split[h]):
            for ht_a, w_a in enumerate(split[a]):
                values[_outcome(ht_h, ht_a) + full] += float(p) * w_h * w_a
    return values
