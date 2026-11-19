"""Monte Carlo over frozen model probabilities: what several matches add up to.

A single match needs no simulation: its home/draw/away probabilities are exact, and
sampling them only adds noise. Simulation earns its place where outcomes of many matches
combine (how many of a ticket's picks come in). Every result here is reproducible: the
seed is derived from the frozen inputs, and an exact calculation is kept beside the
sampled one so the two can be checked against each other.

Nothing here reads odds or any market information.
"""

from __future__ import annotations

import hashlib

import numpy as np

RUNS = 10_000
OUTCOMES = "hda"


def seed_from(*parts):
    """A stable seed from the identifiers of the frozen inputs."""
    text = "\x1f".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _probabilities(values):
    p = np.asarray([values[key] for key in OUTCOMES], dtype=float)
    if p.shape != (3,) or not np.isfinite(p).all() or p.min() < 0 or abs(p.sum() - 1) > 1e-8:
        raise ValueError("模拟只接受归一化的胜平负概率")
    return p / p.sum()


def sample_scores(matrix, runs=RUNS, seed=0):
    """Home and away goals drawn from one frozen score matrix."""
    grid = np.asarray(matrix, dtype=float)
    if grid.ndim != 2 or not np.isfinite(grid).all() or grid.min() < 0 or grid.sum() <= 0:
        raise ValueError("模拟只接受有效的比分概率矩阵")
    flat = grid.ravel() / grid.sum()
    cells = np.random.default_rng(seed).choice(flat.size, size=runs, p=flat)
    return cells // grid.shape[1], cells % grid.shape[1]


def outcome_frequencies(matrix, runs=RUNS, seed=0):
    """Sampled home/draw/away shares of one score matrix (for the consistency check)."""
    home, away = sample_scores(matrix, runs, seed)
    return {
        "h": float(np.mean(home > away)),
        "d": float(np.mean(home == away)),
        "a": float(np.mean(home < away)),
    }


def exact_hit_distribution(hit_probabilities):
    """P(exactly k picks come in) for independent matches, by direct convolution."""
    distribution = np.array([1.0])
    for p in hit_probabilities:
        if not 0 <= p <= 1:
            raise ValueError("命中概率必须在 0 与 1 之间")
        distribution = np.convolve(distribution, [1 - p, p])
    return [float(value) for value in distribution]


def simulate_hits(matches, runs=RUNS, seed=0):
    """Share of runs in which exactly k of the top picks come in.

    ``matches`` are home/draw/away probability mappings; each match's pick is its most
    likely outcome (first of h, d, a on a tie). Matches are drawn independently.
    """
    if not matches:
        return [1.0]
    rng = np.random.default_rng(seed)
    hits = np.zeros(runs, dtype=int)
    for values in matches:
        p = _probabilities(values)
        hits += rng.choice(3, size=runs, p=p) == int(np.argmax(p))
    counts = np.bincount(hits, minlength=len(matches) + 1)
    return [float(value) for value in counts / runs]


def hit_summary(matches, *seed_parts, runs=RUNS):
    """Sampled and exact hit distributions for one set of picks, with the seed used."""
    seed = seed_from(*seed_parts)
    picks = [float(max(_probabilities(values))) for values in matches]
    return dict(
        runs=runs,
        seed=seed,
        matches=len(matches),
        simulated=simulate_hits(matches, runs, seed),
        exact=exact_hit_distribution(picks),
    )
