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

from zucai.predict.base import Model, utc


class InsufficientHistory(ValueError):
    """No defensible estimate for this population or fixture."""


class DixonColes(Model):
    def __init__(self, half_life_days: float = 90):
        if not isfinite(half_life_days) or half_life_days <= 0:
            raise ValueError("半衰期必须是正数")
        self.half_life_days = half_life_days
        self.engine = None

    def fit(self, history: pd.DataFrame, *, as_of: datetime) -> Self:
        cutoff = utc(as_of)
        known = history["available_utc"]
        eligible = history.loc[
            (history.kickoff_utc < cutoff) & (known < cutoff) & (known > history.kickoff_utc)
        ].copy()
        scope = eligible[["league", "source"]].drop_duplicates()
        if len(scope) > 1:
            raise ValueError("每个模型须指定一个联赛和一个数据来源")
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
        self.as_of, self.training_rows = cutoff, len(eligible)
        self.latest_available = eligible.available_utc.max()
        self.league, self.source = scope.iloc[0]
        return self

    def predict_score_matrix(self, row: Mapping) -> np.ndarray:
        return self.predict_with_metadata(row)[0]

    def predict_with_metadata(self, row: Mapping) -> tuple[np.ndarray, dict]:
        if self.engine is None:
            raise ValueError("请先训练或载入模型")
        if utc(row["kickoff_utc"]) <= self.as_of:
            raise ValueError("模型截点必须早于开球")
        if (
            row.get("league", self.league) != self.league
            or row.get("source", self.source) != self.source
        ):
            raise ValueError("联赛或数据来源与模型不一致")
        home, away = row["home"], row["away"]
        if home not in self.teams or away not in self.teams:
            raise InsufficientHistory(f"模型缺少球队历史记录：{home} / {away}")
        if home == away:
            raise ValueError("主客队不能相同")
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
            "as_of_utc": self.as_of.isoformat(),
            "last_available_utc": self.latest_available.isoformat(),
            "training_games": self.training_rows,
            "excluded_score_mass": max(0.0, 1.0 - retained),
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
        return model
