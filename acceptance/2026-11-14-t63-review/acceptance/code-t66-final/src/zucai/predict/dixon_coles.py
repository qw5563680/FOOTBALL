"""Dixon–Coles wrapper for penaltyblog 1.13.1 (MIT)."""

import pickle
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from math import isfinite, log
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Self

import numpy as np
import pandas as pd
from penaltyblog.models import DixonColesGoalModel, dixon_coles_weights
from penaltyblog.models.probabilities import compute_dixon_coles_probabilities

from zucai.predict.base import (
    Model,
    availability_sources,
    model_sources,
    reject_cross_source_overlaps,
    utc,
)
from zucai.predict.calibration import fit_draw_offset
from zucai.predict.pooled_fit import FITTING_METHOD, fit_pooled_engine


class InsufficientHistory(ValueError):
    """No defensible estimate for this population or fixture."""


class DixonColes(Model):
    def __init__(
        self,
        half_life_days: float = 90,
        *,
        unknown_team_prior=True,
        draw_calibration=False,
        pooled=False,
    ):
        if not isfinite(half_life_days) or half_life_days <= 0:
            raise ValueError("半衰期必须是正数")
        self.half_life_days = half_life_days
        self.unknown_team_prior = unknown_team_prior
        self.draw_calibration = draw_calibration
        self.pooled = pooled
        if pooled:
            self.prediction_leagues = ["欧冠", "欧罗巴", "欧协联"]
        self.engine = None

    def fit(self, history: pd.DataFrame, *, as_of: datetime) -> Self:
        cutoff = utc(as_of)
        if not getattr(self, "pooled", False) and history.league.nunique() > 1:
            raise ValueError("每个模型须指定一个联赛，不能混合多个联赛")
        sources = model_sources(
            {"sources": history.attrs.get("sources", sorted(history.source.unique()))}
        )
        if not history.source.isin(sources).all():
            raise ValueError("历史数据来源超出显式选择的来源范围")
        reject_cross_source_overlaps(history)
        known = history["available_utc"]
        eligible = history.loc[
            (history.kickoff_utc < cutoff) & (known < cutoff) & (known > history.kickoff_utc)
        ].copy()
        if getattr(self, "pooled", False) and "training_leagues" in history.attrs:
            declared = history.attrs["training_leagues"]
            if (
                not isinstance(declared, (list, tuple, set))
                or any(not isinstance(league, str) or not league.strip() for league in declared)
                or not set(eligible.league).issubset(declared)
            ):
                raise ValueError("可用训练联赛超出声明的训练联赛范围")
        teams = set(eligible.home) | set(eligible.away)
        if len(teams) < 2 or len(eligible) < 2 * len(teams) + 1:
            raise InsufficientHistory("截点前可用赛果不足以拟合球队参数")
        scores = eligible[["home_goals", "away_goals"]].to_numpy(dtype=float)
        if (
            not np.isfinite(scores).all()
            or (scores < 0).any()
            or (scores != np.floor(scores)).any()
            or (eligible.home == eligible.away).any()
        ):
            raise ValueError("无效的常规时间赛果或主客队")
        # Calendar-day decay matches the documented public acceptance fixture.
        weights = dixon_coles_weights(
            eligible.kickoff_utc.dt.normalize(),
            xi=log(2) / self.half_life_days,
            base_date=cutoff.normalize(),
        )
        engine = DixonColesGoalModel(
            eligible.home_goals, eligible.away_goals, eligible.home, eligible.away, weights=weights
        )
        if getattr(self, "pooled", False) and eligible.league.nunique() > 1:
            diagnostics = fit_pooled_engine(engine)
            fitting_method = FITTING_METHOD
        else:
            # Preserve upstream arithmetic for domestic and single-league pooled fits.
            engine.fit()
            if getattr(self, "pooled", False):
                fitting_method = "penaltyblog_slsqp"
                diagnostics = dict(
                    optimizer="SLSQP",
                    gradient="penaltyblog",
                    initialization="penaltyblog_default",
                    maxiter=1000,
                    ftol=1e-6,
                    success=bool(engine._res.success),
                    iterations=int(engine._res.nit),
                )
        if not np.isfinite(engine.loglikelihood):
            raise ValueError("拟合未得到有限的对数似然")
        self.engine, self.teams = engine, teams
        if getattr(self, "pooled", False):
            self.fitting_method = fitting_method
            self.solver_diagnostics = diagnostics
        params = engine.get_params()
        attack = np.sort([params[f"attack_{team}"] for team in teams])
        defence = np.sort([params[f"defence_{team}"] for team in teams])
        count = max(1, int(np.ceil(len(teams) / 4)))
        # Weak attack and weak defence, each halfway back to the league mean.
        # Higher log defence means more goals conceded in penaltyblog.
        self.prior_parameters = {
            "attack": float((attack[:count].mean() + attack.mean()) / 2),
            "defence": float((defence[-count:].mean() + defence.mean()) / 2),
            "method": "weakest_quartile_half_shrinkage",
        }
        self.draw_offset = 0.0
        self.calibration_training_rows = 0
        self.calibration_skipped_training_rows = 0
        if self.draw_calibration:
            self._fit_draw_calibration(eligible, weights)
        self.as_of, self.training_rows = cutoff, len(eligible)
        provenance = availability_sources(eligible)
        self.assumed_training_rows = int((provenance == "assumed").sum())
        self.assumed_training_share = self.assumed_training_rows / self.training_rows
        self.replayed_training_rows = int((provenance == "replayed").sum())
        self.replayed_training_share = self.replayed_training_rows / self.training_rows
        # Only the training command can associate a walk-forward evaluation with this artifact.
        self.replayed_eval_share = None
        self.replay_delay = history.attrs.get("replay_delay")
        self.availability_basis = (
            "replayed"
            if self.replayed_training_rows
            else "strict"
            if provenance.isin(("published", "captured")).all()
            else "mixed"
        )
        self.latest_available = eligible.available_utc.max()
        self.league = "欧战" if getattr(self, "pooled", False) else eligible.league.iloc[0]
        if getattr(self, "pooled", False):
            self.training_leagues = {
                league: int(count)
                for league, count in eligible.league.value_counts().sort_index().items()
            }
            self.training_league_scope = sorted(
                set(history.attrs.get("training_leagues", self.training_leagues))
            )
            appearances = pd.concat(
                [
                    eligible[["league", "home"]].rename(columns={"home": "team"}),
                    eligible[["league", "away"]].rename(columns={"away": "team"}),
                ],
                ignore_index=True,
            )
            self.team_training_leagues = {
                team: {
                    league: int(count)
                    for league, count in group.league.value_counts().sort_index().items()
                }
                for team, group in appearances.groupby("team", sort=True)
            }
        # sources is the selected corpus, including sources without eligible rows.
        self.sources = sources
        self.training_source_counts = {
            source: int(count)
            for source, count in eligible.source.value_counts().sort_index().items()
        }
        return self

    def predict_score_matrix(self, row: Mapping) -> np.ndarray:
        return self.predict_with_metadata(row)[0]

    def predict_with_metadata(self, row: Mapping) -> tuple[np.ndarray, dict]:
        if self.engine is None:
            raise ValueError("请先训练或载入模型")
        if utc(row["kickoff_utc"]) <= self.as_of:
            raise ValueError("模型截点必须早于开球")
        if (
            not getattr(self, "pooled", False) and row.get("league", self.league) != self.league
        ) or ("source" in row and row["source"] not in model_sources(self)):
            raise ValueError("联赛或数据来源与模型不一致")
        home, away = row["home"], row["away"]
        unknown = home not in self.teams or away not in self.teams
        if unknown and not getattr(self, "unknown_team_prior", False):
            raise InsufficientHistory(f"模型缺少球队历史记录：{home} / {away}")
        if home == away:
            raise ValueError("主客队不能相同")
        raw, guard = self._guarded_grid(home, away)
        if not unknown and not guard["rho_clipped"]:
            # Keep the exact upstream path for unaffected known-team predictions.
            raw = self.engine.predict(home, away, max_goals=15, normalize=False).grid[:10, :10]
        retained = float(raw.sum())
        if (
            raw.shape != (10, 10)
            or not np.isfinite(raw).all()
            or raw.min() < 0
            or not 0 < retained <= 1 + 1e-9
        ):
            raise ValueError("拟合参数未产生有效的比分概率矩阵")
        matrix = raw / retained
        if getattr(self, "draw_calibration", False):
            matrix[np.diag_indices(10)] *= np.exp(self.draw_offset)
            matrix /= matrix.sum()
        metadata = {
            "draw_calibration": getattr(self, "draw_calibration", False),
            "draw_offset": getattr(self, "draw_offset", 0.0),
            "calibration_training_rows": getattr(self, "calibration_training_rows", 0),
            "calibration_skipped_training_rows": getattr(
                self, "calibration_skipped_training_rows", 0
            ),
            "unknown_team_prior": unknown,
            "home_unknown_team": home not in self.teams,
            "away_unknown_team": away not in self.teams,
            "prior_method": self.prior_parameters["method"] if unknown else None,
            "prior_rho_clipped": unknown and guard["rho_clipped"],
            **guard,
            "half_life_days": self.half_life_days,
            "parameter_source": getattr(self, "parameter_source", "explicit"),
            "as_of_utc": self.as_of.isoformat(),
            "last_available_utc": self.latest_available.isoformat(),
            "training_games": self.training_rows,
            "training_source_counts": getattr(self, "training_source_counts", None),
            "assumed_training_share": getattr(self, "assumed_training_share", None),
            "replayed_training_share": getattr(self, "replayed_training_share", 0.0),
            "replayed_eval_share": getattr(self, "replayed_eval_share", None),
            "replay_delay": getattr(self, "replay_delay", None),
            "availability_basis": getattr(self, "availability_basis", "unknown"),
            "excluded_score_mass": max(0.0, 1.0 - retained),
        }
        if getattr(self, "pooled", False):
            metadata.update(
                pooled=True,
                fitting_method=getattr(self, "fitting_method", "legacy_penaltyblog_slsqp"),
                solver_diagnostics=deepcopy(getattr(self, "solver_diagnostics", None)),
                training_leagues=dict(self.training_leagues),
                home_training_leagues=dict(self.team_training_leagues.get(home, {})),
                away_training_leagues=dict(self.team_training_leagues.get(away, {})),
                home_advantage=float(self.engine.get_params()["home_advantage"]),
            )
        return matrix, metadata

    def _fit_draw_calibration(self, eligible, weights):
        probabilities, outcomes, sample_weights = [], [], []
        pairs = {}
        for row, weight in zip(eligible.itertuples(), weights, strict=True):
            key = row.home, row.away
            if key not in pairs:
                try:
                    raw = self.engine.predict(*key, max_goals=15, normalize=False).grid[:10, :10]
                    pairs[key] = (
                        float(np.trace(raw) / raw.sum())
                        if np.isfinite(raw).all() and raw.min() >= 0 and raw.sum() > 0
                        else None
                    )
                except ValueError:
                    pairs[key] = None
            probability = pairs[key]
            if probability is not None:
                probabilities.append(probability)
                outcomes.append(row.home_goals == row.away_goals)
                sample_weights.append(weight)
        self.draw_offset = fit_draw_offset(probabilities, outcomes, sample_weights)
        self.calibration_training_rows = len(probabilities)
        self.calibration_skipped_training_rows = len(eligible) - len(probabilities)

    def _guarded_grid(self, home, away):
        """Constrain only this prediction's rho; never mutate fitted parameters.

        The four DC multipliers require max(-1/lambda, -1/mu) <= rho <=
        min(1, 1/(lambda*mu)). Use the upstream routine's rates so bounds and
        the matrix share the same exp arithmetic. Boundary roundoff is handled
        by at most eight representable steps toward zero, explicitly recorded.
        Other invalid matrices still fail the caller's checks; no cell is clipped.
        """
        params = self.engine.get_params()
        prior = getattr(self, "prior_parameters", {})
        strengths = tuple(
            float(params[f"{kind}_{team}"] if f"{kind}_{team}" in params else prior[kind])
            for kind, team in (
                ("attack", home),
                ("attack", away),
                ("defence", home),
                ("defence", away),
            )
        )
        advantage, original = float(params["home_advantage"]), float(params["rho"])
        if not np.isfinite((*strengths, advantage, original)).all():
            raise ValueError("拟合参数未产生有效的比分概率矩阵")
        grid = np.empty(15 * 15, dtype=np.float64)
        home_rate, away_rate = np.empty(1), np.empty(1)

        def compute(rho):
            compute_dixon_coles_probabilities(
                *strengths, advantage, rho, 15, grid, home_rate, away_rate
            )

        compute(original)
        h, a = float(home_rate[0]), float(away_rate[0])
        if not all(isfinite(v) and v > 0 for v in (h, a, h * a)):
            raise ValueError("拟合参数未产生有效的比分概率矩阵")
        lower, upper = max(-1 / h, -1 / a), min(1.0, 1 / (h * a))
        rho = float(np.clip(original, lower, upper))
        reason = "outside_legal_interval" if rho != original else None
        if rho != original:
            compute(rho)
        steps = 0
        while (grid.reshape(15, 15)[:2, :2] < 0).any() and steps < 8:
            rho = float(np.nextafter(rho, 0.0))
            compute(rho)
            steps += 1
        if steps and reason is None:
            reason = "boundary_roundoff"
        return grid.reshape(15, 15)[:10, :10], {
            "rho_clipped": rho != original,
            "rho_before": original,
            "rho_after": rho,
            "rho_legal_interval": [lower, upper],
            "rho_clip_reason": reason,
            "rho_roundoff_steps": steps,
        }

    def save(self, path: Path) -> None:
        if self.engine is None:
            raise ValueError("请先训练模型")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = NamedTemporaryFile(dir=path.parent, delete=False)
        temp = Path(staged.name)
        try:
            with staged:
                pickle.dump(self, staged, protocol=pickle.HIGHEST_PROTOCOL)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load only artifacts produced by a trusted local training run."""
        with Path(path).open("rb") as stream:
            model = pickle.load(stream)
        if not isinstance(model, cls) or model.engine is None:
            raise ValueError("模型文件类型不正确")
        model.sources = model_sources(model)
        model.pooled = getattr(model, "pooled", False)
        return model
