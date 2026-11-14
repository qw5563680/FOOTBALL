"""Replay-trained daily candidates with capture-time strict targets; no market inputs."""

import hashlib
import json
from collections import Counter

import numpy as np
from sqlalchemy import select

from zucai.features.stats import intensity_inputs
from zucai.models import Result
from zucai.predict.base import utc
from zucai.predict.experiments.history import unified_history

SOT_LEAGUES = ("英超", "英冠", "西甲", "意甲", "德甲", "法甲", "法乙", "荷甲", "葡超")
XG_WINDOWS = {"英超": 10, "西甲": 10, "意甲": 10, "德甲": 5}
NAMES = ("dc-sot-v1", "dc-xg-v1")
VERSION = "t34a-strict-v1"
AVAILABILITY_MODES = {
    "training_availability_mode": "backfill_replay",
    "target_availability_mode": "strict_capture_time",
}


def raw_json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(value):
    return hashlib.sha256(raw_json(value)).hexdigest()


def specifications():
    return [("dc-sot-v1", "fd", "shots_on_target", league, 10) for league in SOT_LEAGUES] + [
        ("dc-xg-v1", "apif", "expected_goals", league, n) for league, n in XG_WINDOWS.items()
    ]


def empty_row(target, name, cutoff, reason):
    # Explicit whitelist: SP and other market fields never enter this module.
    return {
        **{
            key: target[key]
            for key in ("match_id", "source", "league", "home", "away", "kickoff_utc")
        },
        "model_name": name,
        "model_version": VERSION,
        "model_type": "TEAM",
        "p2_ready": False,
        "p2_label": "P2 实验",
        "bet_allowed": False,
        "validation_status": "experimental_unadopted",
        "availability_mode": "strict",
        **AVAILABILITY_MODES,
        "training_cutoff_utc": cutoff,
        "feature_cutoff": cutoff,
        "status": "missing",
        "model_probabilities": None,
        "top_score": None,
        "fallback_reason": reason,
        "missing_details": [],
        "features": None,
        "coefficients_sha256": None,
    }


def unavailable(targets, cutoff, reason):
    return [empty_row(target, name, cutoff, reason) for target in targets for name in NAMES]


def compute(session, targets, *, cutoff):
    """Return prediction rows and full fit evidence for a separate Release bundle."""
    from zucai.predict.experiments.intensity import StatsIntensity, complete

    at = utc(cutoff)
    features = {p: unified_history(session, provider=p) for p in ("fd", "apif")}
    labels = {r.match_id: r for r in session.scalars(select(Result))}
    rows = {
        (r["match_id"], r["model_name"]): r
        for r in unavailable(targets, at.isoformat(), "unsupported_league")
    }
    fits = []
    for name, provider, stat, league, window in specifications():
        selected = [t for t in targets if t["league"] == league]
        if not selected:
            continue
        builder, games = features[provider]
        training, rejected = [], Counter()
        fit = dict(
            model_name=name,
            league=league,
            window=window,
            half_life_days=90,
            training_cutoff_utc=at.isoformat(),
            history_source="aligned_exact_ids",
            provider=provider,
            availability_mode="backfill_replay",
            **AVAILABILITY_MODES,
            calibrated=False,
            training=[],
        )
        fitted, fit_reason = None, None
        try:
            for game in games:
                label = labels.get(game["match_id"])
                if game["league"] != league or game["status"] != "finished":
                    continue
                sibling_labels = [labels[k] for k in game["history_match_ids"] if k in labels]
                if len({(r.home_goals, r.away_goals) for r in sibling_labels}) > 1:
                    rejected["counterpart_result_conflict"] += 1
                    continue
                if (
                    label is None
                    or label.available_utc is None
                    or not game["kickoff_utc"] < utc(label.available_utc) < at
                ):
                    rejected["label_not_known_before_fit"] += 1
                    continue
                snapshot = builder.build(
                    game,
                    as_of=game["earliest_kickoff_utc"],
                    mode="backfill_replay",
                    windows=(window,),
                    stat_keys=(stat,),
                    research=False,
                    history_source="aligned",
                )
                snapshot.update(AVAILABILITY_MODES)
                inputs, reasons = intensity_inputs(snapshot, stat, window)
                if reasons or not complete(inputs):
                    rejected["training_features_missing"] += 1
                    continue
                observation = dict(
                    match_id=game["match_id"],
                    kickoff_utc=game["kickoff_utc"].isoformat(),
                    available_utc=utc(label.available_utc).isoformat(),
                    feature_cutoff=snapshot["feature_cutoff"],
                    home_goals=label.home_goals,
                    away_goals=label.away_goals,
                    inputs=inputs,
                    history_match_ids=game["history_match_ids"],
                    **AVAILABILITY_MODES,
                )
                training.append(observation)
                fit["training"].append(dict(observation, features=snapshot))
            fitted = StatsIntensity(stat_key=stat, window=window).fit(training, as_of=at)
            fit.update(
                coefficients=fitted.coefficients,
                model_version=fitted.version,
                diagnostics=fitted.diagnostics,
                training_rows=fitted.training_rows,
            )
            fit["coefficients_sha256"] = sha(fitted.coefficients)
        except Exception as exc:
            fit_reason = "fit_failed:" + type(exc).__name__
            fit["failure_reason"] = fit_reason
        fit["training_rejected"] = dict(rejected)
        fits.append(fit)
        for target in selected:
            row = rows[target["match_id"], name]
            row.update(
                window=window,
                provider=provider,
                history_source="aligned_exact_ids",
                half_life_days=90,
                training_rows=len(training),
                training_rejected=dict(rejected),
            )
            try:
                snapshot = builder.build(
                    target,
                    as_of=at,
                    mode="strict",
                    windows=(window,),
                    stat_keys=(stat,),
                    research=False,
                    history_source="aligned",
                )
                snapshot.update(AVAILABILITY_MODES)
                inputs, reasons = intensity_inputs(snapshot, stat, window)
                row.update(
                    features=snapshot,
                    feature_cutoff=snapshot["feature_cutoff"],
                    missing_details=reasons,
                )
                if utc(target["kickoff_utc"]) <= at:
                    row["fallback_reason"] = "already_started"
                elif reasons or not complete(inputs):
                    row["fallback_reason"] = "strict_features_missing"
                elif fitted is None:
                    row["fallback_reason"] = fit_reason
                else:
                    matrix, meta = fitted.predict(inputs, calibrated=False)
                    probabilities = dict(
                        h=float(np.tril(matrix, -1).sum()),
                        d=float(np.trace(matrix)),
                        a=float(np.triu(matrix, 1).sum()),
                    )
                    if (
                        not np.isfinite(list(probabilities.values())).all()
                        or abs(sum(probabilities.values()) - 1) > 1e-8
                    ):
                        raise ValueError("invalid probabilities")
                    h, a = np.unravel_index(np.argmax(matrix), matrix.shape)
                    row.update(
                        status="predicted",
                        fallback_reason=None,
                        model_probabilities=probabilities,
                        top_score=dict(home=int(h), away=int(a), probability=float(matrix[h, a])),
                        model_version=fitted.version,
                        coefficients_sha256=fit["coefficients_sha256"],
                        score_metadata=meta,
                    )
            except Exception as exc:
                row.update(
                    fallback_reason="prediction_failed:" + type(exc).__name__,
                    model_probabilities=None,
                    top_score=None,
                    status="missing",
                )
    return list(rows.values()), fits
