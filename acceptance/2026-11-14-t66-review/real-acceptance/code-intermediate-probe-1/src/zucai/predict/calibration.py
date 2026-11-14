"""One draw log-odds intercept, fitted exclusively to eligible training results."""

import numpy as np
from scipy.optimize import brentq
from scipy.special import expit, logit


def fit_draw_offset(probabilities, draws, weights):
    probabilities, draws, weights = (
        np.asarray(values, dtype=float) for values in (probabilities, draws, weights)
    )
    if (
        not len(probabilities)
        or probabilities.shape != draws.shape
        or draws.shape != weights.shape
        or not all(np.isfinite(values).all() for values in (probabilities, draws, weights))
        or not np.isin(draws, [0, 1]).all()
        or (weights < 0).any()
        or weights.sum() <= 0
        or ((probabilities < 0) | (probabilities > 1)).any()
    ):
        raise ValueError("平局校准需要有效的训练概率、赛果和权重")
    logits = logit(np.clip(probabilities, 1e-9, 1 - 1e-9))

    def gradient(offset):
        return float(np.dot(weights, expit(logits + offset) - draws))

    # The convex weighted binary log loss has this monotone derivative.
    # Bounds keep all-draw/no-draw training samples finite.
    if gradient(-6) >= 0:
        return -6.0
    if gradient(6) <= 0:
        return 6.0
    return float(brentq(gradient, -6, 6))
