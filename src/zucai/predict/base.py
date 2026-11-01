"""Model contract for an explicit pre-match cutoff and a regular-time score matrix."""

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


def normalize_sources(value: str | None) -> list[str] | None:
    """Canonicalize an explicitly selected corpus; None keeps ambiguity checks enabled."""
    if value is None:
        return None
    if not isinstance(value, str) or any(not part.strip() for part in value.split(",")):
        raise ValueError("数据来源须为非空、逗号分隔的来源名称")
    return sorted({part.strip() for part in value.split(",")})


def model_sources(model) -> list[str]:
    """Read current corpus metadata or a legacy singleton without changing frozen data."""
    if isinstance(model, Mapping):
        sources, legacy = model.get("sources"), model.get("source")
    else:
        sources, legacy = getattr(model, "sources", None), getattr(model, "source", None)
    if sources is None:
        sources = [legacy]
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(s, str) or not s.strip() or "," in s for s in sources)
        or len(set(sources)) != len(sources)
    ):
        raise ValueError("模型数据来源须为非空且不重复的来源列表")
    return sorted(sources)


def reject_cross_source_overlaps(frame: pd.DataFrame) -> None:
    """Reject known canonical fixtures duplicated across any training feeds."""
    selected = frame.loc[frame.source.isin(("jc", "fd", "fdorg"))]
    if selected.source.nunique() < 2:
        return
    window = pd.Timedelta(hours=3)
    recent = defaultdict(deque)
    for row in selected.sort_values("kickoff_utc").itertuples():
        group = recent[row.league]
        while group and group[0].kickoff_utc < row.kickoff_utc - window:
            group.popleft()
        for other in group:
            if row.source == other.source:
                continue
            same_pair = (row.home, row.away) == (other.home, other.away)
            api = row if row.source == "fdorg" else other if other.source == "fdorg" else None
            # Follow the importer's quarantine when higher-priority data arrives afterwards.
            # An opaque provider team cannot clear an official fixture in the same time window.
            unresolved = api is not None and (
                any(str(name).startswith("fdorg:team:") for name in (api.home, api.away))
                or not {row.home, row.away}.isdisjoint((other.home, other.away))
            )
            if same_pair or unresolved:
                raise ValueError(
                    "同联赛三小时内存在跨来源重复或待核对的身份重叠："
                    f"{other.match_id} / {row.match_id}"
                )
        group.append(row)


def utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("截点及开赛时间须包含时区")
    return stamp.tz_convert("UTC")


def availability_sources(history: pd.DataFrame) -> pd.Series:
    """Old timestamped rows have captured provenance; missing times stay unavailable."""
    if "available_source" not in history:
        return pd.Series("captured", index=history.index)
    return history["available_source"].fillna("captured")


class Model(ABC):
    @abstractmethod
    def fit(self, history: pd.DataFrame, *, as_of: datetime):
        """Train only on outcomes already available strictly before as_of."""

    @abstractmethod
    def predict_score_matrix(self, row: Mapping) -> np.ndarray:
        """Rows are home goals, columns away goals, both ordered from zero."""

    @abstractmethod
    def save(self, path: Path) -> None: ...

    @classmethod
    @abstractmethod
    def load(cls, path: Path): ...
