"""Dixon–Coles wrapper for penaltyblog 1.13.1 (MIT)."""

import pickle
from collections.abc import Mapping
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


class InsufficientHistory(ValueError):
    """No defensible estimate for this population or fixture."""


class DixonColes(Model):
    def __init__(self, half_life_days: float = 90, *, unknown_team_prior=True):
        if not isfinite(half_life_days) or half_life_days <= 0:
            raise ValueError("半衰期必须是正数")
        self.half_life_days = half_life_days
        self.unknown_team_prior = unknown_team_prior
        self.engine = None

    def fit(self, history: pd.DataFrame, *, as_of: datetime) -> Self:
        cutoff = utc(as_of)
        if history.league.nunique() > 1:
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
        engine.fit()
        if not np.isfinite(engine.loglikelihood):
            raise ValueError("拟合未得到有限的对数似然")
        self.engine, self.teams = engine, teams
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
        self.league = eligible.league.iloc[0]
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
        if row.get("league", self.league) != self.league or (
            "source" in row and row["source"] not in model_sources(self)
        ):
            raise ValueError("联赛或数据来源与模型不一致")
        home, away = row["home"], row["away"]
        unknown = home not in self.teams or away not in self.teams
        if unknown and not getattr(self, "unknown_team_prior", False):
            raise InsufficientHistory(f"模型缺少球队历史记录：{home} / {away}")
        if home == away:
            raise ValueError("主客队不能相同")
        clipped = False
        if unknown:
            raw, clipped = self._prior_grid(home, away)
        else:
            raw = self.engine.predict(home, away, max_goals=15, normalize=False).grid[:10, :10]
        retained = float(raw.sum())
        if (
            raw.shape != (10, 10)
            or not np.isfinite(raw).all()
            or raw.min() < 0
            or not 0 < retained <= 1 + 1e-9
        ):
            raise ValueError("拟合参数未产生有效的比分概率矩阵")
        return raw / retained, {
            "unknown_team_prior": unknown,
            "prior_method": self.prior_parameters["method"] if unknown else None,
            "prior_rho_clipped": clipped,
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

    def _prior_grid(self, home, away):
        params = self.engine.get_params()
        attack_home = params.get(f"attack_{home}", self.prior_parameters["attack"])
        attack_away = params.get(f"attack_{away}", self.prior_parameters["attack"])
        defence_home = params.get(f"defence_{home}", self.prior_parameters["defence"])
        defence_away = params.get(f"defence_{away}", self.prior_parameters["defence"])
        home_rate = np.exp(attack_home + defence_away + params["home_advantage"])
        away_rate = np.exp(attack_away + defence_home)
        # A synthetic team pair may exceed the fitted pairs' valid rho interval.
        rho = float(
            np.clip(
                params["rho"],
                max(-1 / home_rate, -1 / away_rate),
                min(1.0, 1 / (home_rate * away_rate)),
            )
        )
        grid = np.empty(15 * 15, dtype=np.float64)
        compute_dixon_coles_probabilities(
            float(attack_home),
            float(attack_away),
            float(defence_home),
            float(defence_away),
            float(params["home_advantage"]),
            rho,
            15,
            grid,
            np.empty(1),
            np.empty(1),
        )
        return grid.reshape(15, 15)[:10, :10], rho != params["rho"]

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
        return model
