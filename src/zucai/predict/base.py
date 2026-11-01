"""Model contract for an explicit pre-match cutoff and a regular-time score matrix."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


def utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("截点及开赛时间须包含时区")
    return stamp.tz_convert("UTC")


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
