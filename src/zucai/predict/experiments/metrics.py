"""Paired historical diagnostics with shared UTC-week bootstrap blocks."""

from collections import defaultdict

import numpy as np

from zucai.predict.base import utc

VARIANTS = (
    "baseline_raw",
    "baseline_calibrated",
    "candidate_raw",
    "candidate_calibrated",
    "ablation_raw",
    "ablation_calibrated",
    "uniform",
    "market",
)


def is_common_experiment(row):
    return bool(
        row.get("experimental")
        and row.get("actual") is not None
        and all(row["variants"].get(name) is not None for name in VARIANTS[:6])
    )


def losses(row, name):
    item = row["variants"].get(name)
    if item is None or row.get("actual") is None:
        return None
    p = np.asarray(item["probabilities"], dtype=float)
    if p.shape != (3,) or not np.isfinite(p).all() or p.min() < 0 or abs(p.sum() - 1) > 1e-8:
        raise ValueError("评估概率必须为归一化的胜平负三项")
    target = np.eye(3)[row["actual"]]
    winners = p == p.max()
    with np.errstate(divide="ignore"):
        ll = float(-np.log(p[row["actual"]]))
    return dict(
        log_loss=ll,
        brier=float(np.square(p - target).sum()),
        top_accuracy=float(winners[row["actual"]] / winners.sum()),
        score_brier=item.get("score_brier"),
    )


def summarize(rows, name):
    scored = [(row, loss) for row in rows if (loss := losses(row, name)) is not None]
    result = dict(
        n=len(scored),
        total=len(rows),
        coverage=len(scored) / len(rows) if rows else 0.0,
        experimental_n=sum(row.get("experimental", False) for row, _ in scored),
        top_tie_rule="fractional_credit_across_equal_maxima",
        calibration=[],
    )
    for key in ("log_loss", "brier", "top_accuracy", "score_brier"):
        values = [loss[key] for _, loss in scored if loss[key] is not None]
        result[key] = float(np.mean(values)) if values else None
    if not scored:
        result.update(draw_mean=None, draw_rate=None, draw_bias=None)
        return result
    p = np.asarray([row["variants"][name]["probabilities"] for row, _ in scored])
    targets = np.eye(3)[[row["actual"] for row, _ in scored]]
    result.update(
        draw_mean=float(p[:, 1].mean()),
        draw_rate=float(targets[:, 1].mean()),
        draw_bias=float((p[:, 1] - targets[:, 1]).mean()),
    )
    bins = np.minimum((p * 10).astype(int), 9)
    for index in range(10):
        mask = bins == index
        if mask.any():
            result["calibration"].append(
                dict(
                    lower=index / 10,
                    n=int(mask.sum()),
                    mean_p=float(p[mask].mean()),
                    observed=float(targets[mask].mean()),
                )
            )
    return result


def paired(rows, left, right, *, common_experiment=False, replicates=2000, seed=3301):
    blocks = defaultdict(list)
    for row in rows:
        if common_experiment and not is_common_experiment(row):
            continue
        a, b = losses(row, left), losses(row, right)
        if a is None or b is None:
            continue
        stamp = utc(row["kickoff_utc"]).normalize()
        week = (stamp - np.timedelta64(stamp.day_of_week, "D")).isoformat()
        blocks[week].append([a[key] - b[key] for key in ("log_loss", "brier", "top_accuracy")])
    n = sum(len(group) for group in blocks.values())
    result = dict(
        n=n,
        blocks=len(blocks),
        left=left,
        right=right,
        common_experiment=common_experiment,
        unit="utc_week",
        seed=seed,
        replicates=replicates,
    )
    if not n:
        return dict(
            result, log_loss=None, brier=None, top_accuracy=None, reason="no_paired_observations"
        )
    counts = np.array([len(blocks[key]) for key in sorted(blocks)])
    sums = np.array([np.sum(blocks[key], axis=0) for key in sorted(blocks)])
    picks = np.random.default_rng(seed).integers(0, len(counts), size=(replicates, len(counts)))
    for i, key in enumerate(("log_loss", "brier", "top_accuracy")):
        finite = bool(np.isfinite(sums[:, i]).all())
        estimates = sums[picks, i].sum(axis=1) / counts[picks].sum(axis=1) if finite else None
        result[key] = dict(
            difference=float(sums[:, i].sum() / n) if finite else None,
            interval=[float(x) for x in np.quantile(estimates, [0.025, 0.975])]
            if finite and len(counts) >= 2
            else None,
            reason=None if finite and len(counts) >= 2 else "insufficient_blocks_or_nonfinite_loss",
        )
    return result


def diagnostics(rows, *, replicates=2000, seed=3301):
    comparisons = {}
    for suffix in ("raw", "calibrated"):
        for a, b in (
            ("candidate", "baseline"),
            ("ablation", "baseline"),
            ("candidate", "ablation"),
        ):
            left, right = f"{a}_{suffix}", f"{b}_{suffix}"
            comparisons[f"{left}-{right}"] = {
                scope: paired(
                    rows, left, right, common_experiment=common, replicates=replicates, seed=seed
                )
                for scope, common in (("all", False), ("common_experiment", True))
            }
    groups = {"cold_start": {}, "statistics_missing": {}}
    for key in groups:
        for value in (True, False):
            subset = [r for r in rows if r.get(key) == value]
            groups[key][str(value).lower()] = {name: summarize(subset, name) for name in VARIANTS}
    return dict(
        models={name: summarize(rows, name) for name in VARIANTS}, paired=comparisons, groups=groups
    )
