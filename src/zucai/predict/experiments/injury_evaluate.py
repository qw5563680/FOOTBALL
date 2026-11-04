"""Frozen T38 replay-training / capture-time-target injury experiment only."""

import hashlib
import json
import logging
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pandas as pd

from zucai.features.stats import intensity_inputs
from zucai.predict.base import utc
from zucai.predict.dixon_coles import InsufficientHistory
from zucai.predict.experiments.evaluate import _variant, fold_cutoff
from zucai.predict.experiments.injury_intensity import AVAILABILITY_MODES, InjuryIntensity
from zucai.predict.experiments.intensity import INPUTS as HOST_INPUTS
from zucai.predict.experiments.intensity import StatsIntensity
from zucai.predict.experiments.metrics import is_common_experiment, paired, summarize
from zucai.predict.seasons import season_for

LOG = logging.getLogger(__name__)
DEFAULT_PROTOCOL = (
    Path(__file__).resolve().parents[4] / "docs/experiments/T38-evaluation-protocol.json"
)
COLUMNS = (
    "baseline_raw",
    "baseline_calibrated",
    "candidate_raw",
    "candidate_calibrated",
    "ablation_raw",
    "ablation_calibrated",
)
# Independent of the mutable path on disk: reject accidental protocol changes.
FROZEN_PROTOCOL = {
    "protocol_version": "t38-injuries-v1",
    "source_task": "docs/tasks/T38-injuries-capture-table-and-features.md",
    "base_commit": "f7fa6c5",
    "host": "dc-sot-v1",
    "host_reason": "T34b not merged at protocol freeze; T38 permitted T33 host",
    "training_start": "2021-07-01T00:00:00Z",
    "selection_start": "2024-07-01T00:00:00Z",
    "selection_end": "2025-07-01T00:00:00Z",
    "evaluation_start": "2025-07-01T00:00:00Z",
    "evaluation_end": "2026-07-01T00:00:00Z",
    "selection_fold": "monthly",
    "evaluation_fold": "weekly",
    "half_life_days": 90,
    "windows": [5, 10],
    "window_selection": "common_candidate_rows_raw_log_loss_then_smaller_window",
    "providers": {
        "fd": {
            "stat_key": "shots_on_target",
            "leagues": ["英超", "英冠", "西甲", "意甲", "德甲", "法甲", "法乙", "荷甲", "葡超"],
        }
    },
    "candidate": "dc-sot-injuries-v1",
    "injury_inputs": ["missing_diff", "missing_last_starters_diff"],
    "ablation": "remove_both_injury_inputs_same_host_same_complete_mask",
    "ablation_common_full_input_mask": True,
    **AVAILABILITY_MODES,
    "injury_replay_assumption": (
        "latest_archived_fixture_list_assumed_known_immediately_before_kickoff"
    ),
    "backfill_delay_hours": 3,
    "primary_metric": "log_loss",
    "secondary_metrics": [
        "brier",
        "top_accuracy",
        "calibration_10_bins",
        "coverage",
        "score_brier",
    ],
    "groups": ["league", "season", "cold_start", "statistics_missing", "injuries_missing"],
    "paired_comparisons": [
        "candidate_raw-baseline_raw",
        "ablation_raw-baseline_raw",
        "candidate_raw-ablation_raw",
        "candidate_calibrated-baseline_calibrated",
        "ablation_calibrated-baseline_calibrated",
        "candidate_calibrated-ablation_calibrated",
    ],
    "bootstrap": {"unit": "utc_week", "replicates": 2000, "seed": 3301, "confidence": 0.95},
    "score_grid": 10,
    "market": "jc_last_complete_had_strictly_before_fixture_cutoff_comparison_only",
    "bet_allowed": False,
    "production_adoption": False,
}


def load_protocol(value=DEFAULT_PROTOCOL):
    protocol = deepcopy(value) if isinstance(value, dict) else json.loads(Path(value).read_bytes())
    if protocol != FROZEN_PROTOCOL:
        raise ValueError("T38 冻结协议不允许修改宿主、切分、输入、可知口径或评价规则")
    return protocol


def protocol_fingerprint(protocol):
    return hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _comparison_summary(rows, protocol):
    common = [row for row in rows if is_common_experiment(row)]
    options = {key: protocol["bootstrap"][key] for key in ("replicates", "seed")}
    comparisons = {}
    for name in protocol["paired_comparisons"]:
        left, right = name.split("-")
        comparisons[name] = {
            scope: paired(rows, left, right, common_experiment=flag, **options)
            for scope, flag in (("all", False), ("common_experiment", True))
        }
    return dict(
        targets=len(rows),
        labeled_targets=sum(row.get("actual") is not None for row in rows),
        common_targets=len(common),
        common_coverage=len(common) / len(rows) if rows else 0.0,
        models={name: summarize(rows, name) for name in COLUMNS},
        common_experiment={name: summarize(common, name) for name in COLUMNS},
        paired=comparisons,
    )


def group_summary(rows, protocol):
    summary = _comparison_summary(rows, protocol)
    groups = {}
    for field in protocol["groups"]:
        groups[field] = {}
        values = (
            (True, False, None)
            if field in {"cold_start", "statistics_missing", "injuries_missing"}
            else sorted({row[field] for row in rows})
        )
        for value in values:
            key = "unknown" if value is None else str(value).lower()
            groups[field][key] = _comparison_summary(
                [row for row in rows if row.get(field) == value], protocol
            )
    summary.update(
        groups=groups,
        comparisons={name: summarize(rows, name) for name in ("uniform", "market")},
        missing_reasons=dict(Counter(reason for row in rows for reason in row["missing_reasons"])),
        status="available_historical_comparison"
        if summary["common_targets"]
        else "insufficient_evidence",
        conclusion=(
            "历史共同样本仅供比较，尚未前瞻验证，不自动采用"
            if summary["common_targets"]
            else "证据不足，尚不能判断是否改善"
        ),
        **AVAILABILITY_MODES,
    )
    return summary


class InjuryWalkForwardExperiment:
    def __init__(self, history, statistics, injuries, *, protocol):
        self.history = history
        self.statistics = statistics
        self.injuries = injuries
        self.protocol = load_protocol(protocol)
        self.feature_cache = {}
        self.injury_cache = {}
        self.sample_cache = {}

    def feature(self, row, window, mode):
        from zucai.features.injuries import injury_inputs

        key = row["match_id"], window, mode
        if key not in self.feature_cache:
            # Aligned copies may disagree by <=3h. The earliest kickoff is the
            # conservative feature cutoff; the latest remains the label clock.
            at = row.get("earliest_kickoff_utc", row["kickoff_utc"])
            stats = self.statistics.build(
                row,
                as_of=at,
                mode=mode,
                windows=(window,),
                stat_keys=("shots_on_target",),
                research=False,
                history_source="aligned",
            )
            injury_key = row["match_id"], mode
            if injury_key not in self.injury_cache:
                self.injury_cache[injury_key] = self.injuries.build(row, as_of=at, mode=mode)
            injuries = self.injury_cache[injury_key]
            technical, stat_reasons = intensity_inputs(stats, "shots_on_target", window)
            injury, injury_reasons = injury_inputs(injuries)
            self.feature_cache[key] = dict(
                feature_cutoff=min(
                    utc(stats["feature_cutoff"]), utc(injuries["feature_cutoff"])
                ).isoformat(),
                availability_mode="strict_capture_time" if mode == "strict" else mode,
                statistics=stats,
                injuries=injuries,
                inputs={**technical, **injury},
                statistics_missing=any(value is None for value in technical.values()),
                injuries_missing=any(value is None for value in injury.values()),
                missing_reasons=[
                    *(f"statistics:{r}" for r in stat_reasons),
                    *(f"injuries:{r}" for r in injury_reasons),
                ],
            )
        return self.feature_cache[key]

    def samples(self, history, window):
        samples = []
        for row in history.to_dict("records"):
            key = row["match_id"], window
            if key not in self.sample_cache:
                features = self.feature(row, window, "backfill_replay")
                self.sample_cache[key] = dict(
                    match_id=row["match_id"],
                    kickoff_utc=row["kickoff_utc"].isoformat(),
                    available_utc=row["available_utc"].isoformat(),
                    home_goals=int(row["home_goals"]),
                    away_goals=int(row["away_goals"]),
                    feature_cutoff=features["feature_cutoff"],
                    inputs=features["inputs"],
                    window=window,
                    features=features,
                    **AVAILABILITY_MODES,
                )
            samples.append(self.sample_cache[key])
        return samples

    @staticmethod
    def _fit(samples, cutoff, window, *, arms=("baseline", "candidate", "ablation")):
        models, errors = {}, {}
        for name in ("baseline", "candidate", "ablation"):
            if name not in arms:
                errors[name] = "fit_not_attempted:no_eligible_target_inputs"
                continue
            try:
                if name == "baseline":
                    observations = [
                        {**row, "inputs": {k: row["inputs"][k] for k in HOST_INPUTS}}
                        for row in samples
                    ]
                    model = StatsIntensity(stat_key="shots_on_target", window=window)
                else:
                    observations = samples
                    model = InjuryIntensity(window=window, ablation=name == "ablation")
                models[name] = model.fit(observations, as_of=cutoff)
            except (InsufficientHistory, ValueError) as exc:
                errors[name] = f"{type(exc).__name__}:{exc}"
        if {"candidate", "ablation"} <= models.keys():
            if models["candidate"].training_ids != models["ablation"].training_ids:
                raise ValueError("T38 消融与候选的完整训练样本集合不一致")
        return models, errors

    def run(self, *, window, start, end, fold):
        start, end = utc(start), utc(end)
        history = self.history.loc[
            (self.history.kickoff_utc >= utc(self.protocol["training_start"]))
            & (self.history.kickoff_utc < end)
            & (self.history.available_utc > self.history.kickoff_utc)
            & (self.history.available_utc < end)
        ].copy()
        period = self.history.loc[
            (self.history.kickoff_utc >= start) & (self.history.kickoff_utc < end)
        ].copy()
        period["fold_cutoff"] = [fold_cutoff(stamp, fold) for stamp in period.kickoff_utc]
        samples = None
        rows, folds = [], []
        for cutoff, fixtures in period.groupby("fold_cutoff", sort=True):
            targets = fixtures.to_dict("records")
            snapshots = [self.feature(row, window, "strict") for row in targets]
            target_counts = {
                name: sum(
                    not feature["statistics_missing"]
                    and (name == "baseline" or not feature["injuries_missing"])
                    for feature in snapshots
                )
                for name in ("baseline", "candidate", "ablation")
            }
            arms = [name for name, count in target_counts.items() if count]
            # A fit whose arm has no eligible strict target cannot contribute to
            # any comparison or selection. Preserve the nulls and record the
            # skipped work, without constructing historical replay inputs.
            if arms and samples is None:
                samples = self.samples(history, window)
            models, errors = self._fit(samples or [], cutoff, window, arms=arms)
            learned = history.loc[history.available_utc < cutoff]
            seen = set(learned.home) | set(learned.away)
            folds.append(
                dict(
                    fit_cutoff=cutoff.isoformat(),
                    window=window,
                    errors=errors,
                    model_status={
                        name: dict(
                            status="fitted"
                            if name in models
                            else "fit_failed"
                            if name in arms
                            else "fit_not_attempted",
                            reason=errors.get(name),
                            eligible_target_inputs=count,
                        )
                        for name, count in target_counts.items()
                    },
                    models={
                        name: dict(
                            model_name=model.name,
                            model_version=model.version,
                            coefficients=model.coefficients,
                            draw_offset=model.draw_offset,
                            training_ids=model.training_ids,
                            training_rows=model.training_rows,
                            diagnostics=model.diagnostics,
                        )
                        for name, model in models.items()
                    },
                    feature_cutoff_basis="each_fixture_earliest_aligned_kickoff",
                    training_evidence="training-features.jsonl.zst, keyed by match_id and window",
                    protocol_fingerprint=protocol_fingerprint(self.protocol),
                    **AVAILABILITY_MODES,
                )
            )
            for row, features in zip(targets, snapshots, strict=True):
                known = row["available_utc"]
                available = (
                    not pd.isna(known)
                    and row["kickoff_utc"] < known < end
                    and row.get("label_rejection_reason") is None
                )
                diff = row["home_goals"] - row["away_goals"] if available else None
                record = dict(
                    **{k: row[k] for k in ("match_id", "home", "away", "league")},
                    kickoff_utc=row["kickoff_utc"].isoformat(),
                    season=season_for(row["kickoff_utc"], row["league"]),
                    actual=(0 if diff > 0 else 1 if diff == 0 else 2) if available else None,
                    home_goals=int(row["home_goals"]) if available else None,
                    away_goals=int(row["away_goals"]) if available else None,
                    label_available_utc=None if pd.isna(known) else known.isoformat(),
                    fit_cutoff=cutoff.isoformat(),
                    feature_cutoff=features["feature_cutoff"],
                    availability_mode="strict_capture_time",
                    window=window,
                    provider="fd",
                    injury_provider="apif",
                    features=features,
                    statistics_missing=features["statistics_missing"],
                    injuries_missing=features["injuries_missing"],
                    missing_reasons=list(features["missing_reasons"]),
                    prediction_errors=dict(errors),
                    cold_start=bool(row["home"] not in seen or row["away"] not in seen),
                    cold_start_definition="team_absent_from_pre_fit_eligible_result_history",
                    experimental=False,
                    fallback_reason=None,
                    bet_allowed=False,
                    p2_ready=False,
                    model_type="TEAM",
                    production_adoption=False,
                    protocol_fingerprint=protocol_fingerprint(self.protocol),
                    variants={name: None for name in (*COLUMNS, "uniform", "market")},
                    **AVAILABILITY_MODES,
                )
                if not available:
                    record["missing_reasons"].append(
                        row.get("label_rejection_reason") or "unavailable_result_label"
                    )
                    rows.append(record)
                    continue
                for name, model in models.items():
                    missing = features["statistics_missing"] or (
                        name != "baseline" and features["injuries_missing"]
                    )
                    if missing:
                        continue
                    inputs = features["inputs"]
                    if name == "baseline":
                        inputs = {key: inputs[key] for key in HOST_INPUTS}
                    predicted = {}
                    try:
                        for calibrated in (False, True):
                            matrix, metadata = model.predict(inputs, calibrated=calibrated)
                            metadata.update(AVAILABILITY_MODES)
                            suffix = "calibrated" if calibrated else "raw"
                            predicted[f"{name}_{suffix}"] = _variant(matrix, metadata, row)
                    except ValueError as exc:
                        record["prediction_errors"][name] = f"prediction_failed:{exc}"
                    else:
                        record["variants"].update(predicted)
                record["experimental"] = all(
                    record["variants"][name] is not None for name in COLUMNS[2:]
                )
                if not record["experimental"]:
                    record["missing_reasons"].extend(
                        f"{name}:{reason}" for name, reason in record["prediction_errors"].items()
                    )
                record["variants"]["uniform"] = dict(
                    probabilities=[1 / 3] * 3, model_type="UNIFORM"
                )
                rows.append(record)
            LOG.info("T38 %s N=%s fit=%s targets=%s", fold, window, cutoff, len(fixtures))
        return rows, folds

    def select(self):
        trials = {}
        for window in self.protocol["windows"]:
            rows, folds = self.run(
                window=window,
                start=self.protocol["selection_start"],
                end=self.protocol["selection_end"],
                fold=self.protocol["selection_fold"],
            )
            trials[str(window)] = dict(predictions=rows, folds=folds)
        eligible = [
            {
                row["match_id"]
                for row in trial["predictions"]
                if row.get("actual") is not None
                and row["variants"].get("candidate_raw") is not None
            }
            for trial in trials.values()
        ]
        common = set.intersection(*eligible)
        for trial in trials.values():
            trial["full_target_metrics"] = summarize(trial["predictions"], "candidate_raw")
            trial["metrics"] = summarize(
                [row for row in trial["predictions"] if row["match_id"] in common], "candidate_raw"
            )
        selected = (
            min(
                self.protocol["windows"],
                key=lambda n: (trials[str(n)]["metrics"]["log_loss"], n),
            )
            if common
            else 5
        )
        return dict(
            window=selected,
            evidence_sufficient=bool(common),
            common_candidate_n=len(common),
            common_candidate_ids=sorted(common),
            frozen_at=self.protocol["selection_end"],
            reason=None if common else "no_common_candidate_selection_observations",
            trials=trials,
            **AVAILABILITY_MODES,
        )


def evaluate_group(history, statistics, injuries, *, protocol):
    experiment = InjuryWalkForwardExperiment(history, statistics, injuries, protocol=protocol)
    selection = experiment.select()
    rows, folds = experiment.run(
        window=selection["window"],
        start=protocol["evaluation_start"],
        end=protocol["evaluation_end"],
        fold=protocol["evaluation_fold"],
    )
    return dict(
        selection=selection,
        predictions=rows,
        folds=folds,
        training_features=list(experiment.sample_cache.values()),
    )
