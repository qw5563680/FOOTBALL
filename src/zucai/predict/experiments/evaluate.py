"""Frozen T33 protocol and T15-style monthly selection / weekly evaluation."""

import hashlib
import json
import logging
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from zucai.features.stats import intensity_inputs
from zucai.predict.base import utc
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory
from zucai.predict.experiments.intensity import StatsIntensity
from zucai.predict.experiments.metrics import diagnostics, summarize
from zucai.predict.seasons import season_bounds, season_for

LOG = logging.getLogger(__name__)


def load_protocol(value):
    p = json.loads(Path(value).read_bytes()) if not isinstance(value, dict) else deepcopy(value)
    if not (
        utc(p["training_start"])
        < utc(p["selection_start"])
        < utc(p["selection_end"])
        <= utc(p["evaluation_start"])
        < utc(p["evaluation_end"])
    ):
        raise ValueError("训练/选择/最终评价切分重叠或顺序错误")
    if (
        p["half_life_days"] != 90
        or p["windows"] != [5, 10]
        or not p["ablation_common_full_input_mask"]
        or p["bet_allowed"]
        or p["production_adoption"]
        or p["selection_fold"] != "monthly"
        or p["evaluation_fold"] != "weekly"
        or p["backfill_delay_hours"] != 3
    ):
        raise ValueError("不支持改变首轮冻结协议的模型/时间/安全约束")
    return p


def fold_cutoff(kickoff, fold):
    stamp = utc(kickoff)
    if fold == "weekly":
        start = stamp.normalize() - np.timedelta64(stamp.day_of_week, "D")
    elif fold == "monthly":
        start = stamp.normalize().replace(day=1)
    else:
        raise ValueError("须使用 T15 weekly/monthly 折")
    return max(start, season_bounds(season_for(stamp, "英超"), "英超")[0]) - np.timedelta64(1, "us")


def _fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _variant(matrix, metadata, row):
    probabilities = had(matrix)
    y, z = int(row["home_goals"]), int(row["away_goals"])
    score_brier = float(
        np.square(matrix).sum() + 1 - (2 * matrix[y, z] if y < 10 and z < 10 else 0)
    )
    return dict(
        probabilities=[probabilities[k] for k in "hda"],
        score_grid=matrix.tolist(),
        score_brier=score_brier,
        **metadata,
    )


class WalkForwardExperiment:
    def __init__(self, history, features, *, protocol, stat_key, mode, baseline_cache=None):
        self.history = history
        self.features = features
        self.protocol = protocol
        self.stat_key = stat_key
        self.mode = mode
        self.baselines = baseline_cache if baseline_cache is not None else {}
        self.feature_cache = {}

    def feature(self, row, window):
        key = row["match_id"], window
        if key not in self.feature_cache:
            self.feature_cache[key] = self.features.build(
                row,
                as_of=row["kickoff_utc"],
                mode=self.mode,
                windows=(window,),
                stat_keys=(self.stat_key,),
                research=False,
            )
        return self.feature_cache[key]

    def samples(self, history, window):
        result = []
        for row in history.to_dict("records"):
            features = self.feature(row, window)
            values, reasons = intensity_inputs(features, self.stat_key, window)
            result.append(
                dict(
                    match_id=row["match_id"],
                    kickoff_utc=row["kickoff_utc"],
                    available_utc=row["available_utc"],
                    feature_cutoff=features["feature_cutoff"],
                    home_goals=row["home_goals"],
                    away_goals=row["away_goals"],
                    inputs=values,
                    missing_reasons=reasons,
                )
            )
        return result

    def run(self, *, window, start, end, fold):
        start, end = utc(start), utc(end)
        # Selection physically excludes every evaluation-season label.
        history = self.history.loc[
            (self.history.kickoff_utc < end)
            & (self.history.available_utc < end)
            & (self.history.kickoff_utc >= utc(self.protocol["training_start"]))
        ].copy()
        history.attrs = self.history.attrs.copy()
        period = self.history.loc[
            (self.history.kickoff_utc >= start) & (self.history.kickoff_utc < end)
        ].copy()
        period["fold_cutoff"] = [fold_cutoff(stamp, fold) for stamp in period.kickoff_utc]
        samples = self.samples(history, window)
        rows, folds = [], []
        for cutoff, fixtures in period.groupby("fold_cutoff", sort=True):
            label = cutoff.isoformat()
            if label not in self.baselines:
                try:
                    self.baselines[label] = DixonColes(
                        90, unknown_team_prior=True, draw_calibration=True
                    ).fit(history, as_of=cutoff)
                except (InsufficientHistory, ValueError) as exc:
                    self.baselines[label] = str(exc)
            baseline = self.baselines[label]
            base_error = baseline if isinstance(baseline, str) else None
            full = ablated = None
            failure = None
            try:
                full = StatsIntensity(stat_key=self.stat_key, window=window).fit(
                    samples, as_of=cutoff
                )
                ablated = StatsIntensity(stat_key=self.stat_key, window=window, ablation=True).fit(
                    samples, as_of=cutoff
                )
                if full.training_ids != ablated.training_ids:
                    raise ValueError("消融训练样本集合与全模型不一致")
            except (InsufficientHistory, ValueError) as exc:
                failure = str(exc)
                full = ablated = None
            base_payload = (
                None
                if base_error
                else dict(
                    parameters={k: float(v) for k, v in baseline.engine.get_params().items()},
                    draw_offset=baseline.draw_offset,
                    as_of=label,
                    training_rows=baseline.training_rows,
                )
            )
            info = dict(
                fit_cutoff=label,
                window=window,
                baseline=base_payload,
                baseline_error=base_error,
                candidate_error=failure,
                models={},
            )
            for name, model in (("candidate", full), ("ablation", ablated)):
                if model:
                    info["models"][name] = dict(
                        model_version=model.version,
                        coefficients=model.coefficients,
                        draw_offset=model.draw_offset,
                        training_ids=model.training_ids,
                        diagnostics=model.diagnostics,
                    )
            folds.append(info)
            for row in fixtures.to_dict("records"):
                diff = row["home_goals"] - row["away_goals"]
                features = self.feature(row, window)
                inputs, missing = intensity_inputs(features, self.stat_key, window)
                record = dict(
                    match_id=row["match_id"],
                    home=row["home"],
                    away=row["away"],
                    league=row["league"],
                    season=season_for(row["kickoff_utc"], row["league"]),
                    kickoff_utc=row["kickoff_utc"].isoformat(),
                    fit_cutoff=label,
                    feature_cutoff=features["feature_cutoff"],
                    actual=0 if diff > 0 else 1 if diff == 0 else 2,
                    home_goals=int(row["home_goals"]),
                    away_goals=int(row["away_goals"]),
                    provider=self.features.provider,
                    availability_mode=self.mode,
                    window=window,
                    statistics_missing=bool(missing),
                    cold_start=None,
                    experimental=False,
                    fallback_reason=None,
                    missing_reasons=missing,
                    features=features,
                    bet_allowed=False,
                    p2_ready=False,
                    model_type="TEAM",
                    variants={},
                )
                variants = record["variants"]
                if (
                    pd.isna(row["available_utc"])
                    or row["available_utc"] >= end
                    or row["available_utc"] <= row["kickoff_utc"]
                ):
                    record["fallback_reason"] = "unavailable_result_label"
                    record["actual"] = None
                    rows.append(record)
                    continue
                for calibrated in (False, True):
                    name = "baseline_calibrated" if calibrated else "baseline_raw"
                    variants[name] = None
                    if not base_error:
                        try:
                            baseline.draw_calibration = calibrated
                            matrix, meta = baseline.predict_with_metadata(row)
                            record["cold_start"] = bool(meta["unknown_team_prior"])
                            metadata = dict(
                                meta,
                                model_name="dixon-coles",
                                model_type="TEAM",
                                bet_allowed=False,
                                p2_ready=False,
                                model_version=_fingerprint(
                                    dict(base_payload, calibrated=calibrated)
                                ),
                            )
                            variants[name] = _variant(matrix, metadata, row)
                        except (InsufficientHistory, ValueError) as exc:
                            record["baseline_prediction_error"] = str(exc)
                        finally:
                            baseline.draw_calibration = True
                reason = (
                    "statistics_missing"
                    if missing
                    else "candidate_fit_unavailable"
                    if failure
                    else None
                )
                candidates = {}
                if reason is None:
                    try:
                        for name, model in (("candidate", full), ("ablation", ablated)):
                            for calibrated in (False, True):
                                matrix, meta = model.predict(inputs, calibrated=calibrated)
                                suffix = "calibrated" if calibrated else "raw"
                                candidates[f"{name}_{suffix}"] = _variant(matrix, meta, row)
                    except ValueError as exc:
                        reason = "candidate_prediction_failed:" + str(exc)
                if reason is not None:
                    for name in ("candidate", "ablation"):
                        for suffix in ("raw", "calibrated"):
                            value = deepcopy(variants[f"baseline_{suffix}"])
                            if value:
                                value.update(requested_model=name, fallback_reason=reason)
                            variants[f"{name}_{suffix}"] = value
                    record["fallback_reason"] = reason
                else:
                    variants.update(candidates)
                    record["experimental"] = True
                variants["uniform"] = dict(probabilities=[1 / 3] * 3, model_type="UNIFORM")
                variants["market"] = None  # filled only after every fit/window decision is over
                rows.append(record)
            LOG.info(
                "T33 %s/%s %s N=%s %s: %d games",
                self.features.provider,
                fixtures.league.iloc[0],
                self.mode,
                window,
                label,
                len(fixtures),
            )
        return rows, folds

    def select(self):
        trials = {}
        for window in self.protocol["windows"]:
            rows, folds = self.run(
                window=window,
                start=self.protocol["selection_start"],
                end=self.protocol["selection_end"],
                fold="monthly",
            )
            trials[str(window)] = dict(
                metrics=summarize(rows, "candidate_raw"), predictions=rows, folds=folds
            )
        eligible = [
            n
            for n in self.protocol["windows"]
            if trials[str(n)]["metrics"]["experimental_n"] > 0
            and trials[str(n)]["metrics"]["log_loss"] is not None
        ]
        chosen = (
            min(eligible, key=lambda n: (trials[str(n)]["metrics"]["log_loss"], n))
            if eligible
            else 5
        )
        return dict(
            window=chosen,
            evidence_sufficient=bool(eligible),
            frozen_at=self.protocol["selection_end"],
            reason=None if eligible else "no_selection_experiment",
            trials=trials,
        )


def evaluate_group(history, features, *, protocol):
    stat_key = protocol["providers"][features.provider]["stat_key"]
    output = {}
    cache = {}
    for mode in protocol["availability_modes"]:
        experiment = WalkForwardExperiment(
            history, features, protocol=protocol, stat_key=stat_key, mode=mode, baseline_cache=cache
        )
        selection = experiment.select()
        rows, folds = experiment.run(
            window=selection["window"],
            start=protocol["evaluation_start"],
            end=protocol["evaluation_end"],
            fold="weekly",
        )
        bootstrap = protocol["bootstrap"]
        output[mode] = dict(
            selection=selection,
            predictions=rows,
            folds=folds,
            summary=diagnostics(rows, replicates=bootstrap["replicates"], seed=bootstrap["seed"]),
        )
    return output
