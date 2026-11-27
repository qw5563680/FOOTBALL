"""T100 offline ratings from cutoff-selected 90-minute results."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from math import isfinite, log
from numbers import Integral
from types import MappingProxyType

import numpy as np
import pandas as pd
from scipy.special import expit

from zucai.predict.base import utc

RESULT_INPUT_FIELDS = (
    "match_id",
    "source",
    "league",
    "home_id",
    "away_id",
    "kickoff_utc",
    "available_utc",
    "available_source",
    "home_goals",
    "away_goals",
)
SOURCES = frozenset(("jc", "fd", "fdorg", "apif"))
AVAILABILITY_BASES = frozenset(("assumed", "captured", "published"))
INITIAL_RATING = 1500.0


@dataclass(frozen=True, slots=True, order=True)
class RatingParameters:
    update_scale: float
    home_advantage: float
    logistic_scale: float
    draw_ratio: float

    def __post_init__(self):
        if (
            not all(isfinite(value) for value in asdict(self).values())
            or min(self.update_scale, self.logistic_scale, self.draw_ratio) <= 0
            or not isfinite(self.draw_threshold)
            or self.home_advantage < 0
        ):
            raise ValueError("跨联赛评分参数须为有限值，更新幅度、斜率尺度与平局比例须为正数")

    @property
    def draw_threshold(self) -> float:
        return self.draw_ratio * self.logistic_scale


def candidate_parameters() -> tuple[RatingParameters, ...]:
    """Return the preregistered grid in (K, H, S, r) tie-break order."""
    return tuple(
        RatingParameters(*values)
        for values in product((16, 24, 32), (35, 65, 95), (150, 200), (0.5, 0.6, 0.7))
    )


def _integers(values, *, minimum):
    return all(
        isinstance(value, Integral) and not isinstance(value, bool) and value >= minimum
        for value in values
    )


@dataclass(frozen=True, slots=True)
class RatingResult:
    match_id: str
    source: str
    league: str
    home_id: int
    away_id: int
    kickoff_utc: datetime
    available_utc: datetime | None
    available_source: str
    home_goals: int
    away_goals: int

    def __post_init__(self):
        if (
            self.source not in SOURCES
            or not isinstance(self.match_id, str)
            or not self.match_id.startswith(self.source + ":")
            or len(self.match_id) == len(self.source) + 1
            or not isinstance(self.league, str)
            or not self.league.strip()
        ):
            raise ValueError("跨联赛评分赛果须有可核实的来源、编号及赛事")
        if self.available_source not in AVAILABILITY_BASES:
            raise ValueError(f"{self.match_id} 赛果可知时间的依据无效")
        if not _integers((self.home_id, self.away_id), minimum=1) or self.home_id == self.away_id:
            raise ValueError(f"{self.match_id} 赛果须有两个不同的归档球队 ID")
        if not _integers((self.home_goals, self.away_goals), minimum=0):
            raise ValueError(f"{self.match_id} 赛果须有非负整数的常规时间比分")
        object.__setattr__(self, "kickoff_utc", utc(self.kickoff_utc))
        object.__setattr__(
            self,
            "available_utc",
            None if pd.isna(self.available_utc) else utc(self.available_utc),
        )

    @classmethod
    def from_row(cls, row: Mapping):
        """Read only the rating fields from one eligible source representative.

        Callers use the existing availability and source-link rules to obtain
        the effective availability and select one result per match entity.
        """
        return cls(**{field: row[field] for field in RESULT_INPUT_FIELDS})


def _probabilities(home, away, advantage, scale, threshold):
    difference = home - away + advantage
    win = expit((difference - threshold) / scale)
    loss = expit((-difference - threshold) / scale)
    # Calculate the draw mass in the smaller tail to avoid subtracting near 1.
    nearer = expit((threshold - abs(difference)) / scale)
    draw = nearer - np.minimum(win, loss)
    return win, draw, loss


@dataclass(frozen=True, slots=True)
class RatingPrediction:
    probabilities: tuple[float, float, float] | None = None
    reason: str | None = None
    missing_team_ids: tuple[int, ...] = ()

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    parameters: RatingParameters
    as_of_utc: datetime
    ratings: Mapping[int, float]
    training_rows: int
    latest_available_utc: datetime | None

    def predict(self, home_id: int, away_id: int, *, kickoff_utc) -> RatingPrediction:
        if utc(kickoff_utc) <= self.as_of_utc:
            return RatingPrediction(reason="cutoff_not_pre_match")
        missing = tuple(int(team) for team in (home_id, away_id) if team not in self.ratings)
        if missing:
            return RatingPrediction(reason="missing_team_rating", missing_team_ids=missing)
        params = self.parameters
        win, draw, loss = _probabilities(
            self.ratings[home_id],
            self.ratings[away_id],
            params.home_advantage,
            params.logistic_scale,
            params.draw_threshold,
        )
        return RatingPrediction(probabilities=(float(win), float(draw), float(loss)))

    def as_dict(self):
        return {
            "parameters": asdict(self.parameters),
            "as_of_utc": self.as_of_utc.isoformat(),
            "ratings": dict(self.ratings),
            "training_rows": self.training_rows,
            "latest_available_utc": (
                self.latest_available_utc.isoformat() if self.latest_available_utc else None
            ),
        }


def _events(history, cutoff):
    events = []
    seen = set()
    for match in history:
        known = match.available_utc
        if known is None or not match.kickoff_utc < known < cutoff:
            continue
        if match.match_id in seen:
            raise ValueError(f"跨联赛评分赛果编号重复：{match.match_id}")
        seen.add(match.match_id)
        events.append((match.kickoff_utc, False, match.kickoff_utc, match.match_id, match))
        events.append((known, True, match.kickoff_utc, match.match_id, match))
    events.sort()
    return events


def _initial_rating(members, ratings, count):
    if not members:
        return np.full(count, INITIAL_RATING)
    weakest_count = (len(members) + 3) // 4
    values = np.array([ratings[team] for team in members])
    return np.sort(values, axis=0)[:weakest_count].mean(axis=0)


def fit_ratings(
    history: Iterable[RatingResult], *, as_of, parameters: Sequence[RatingParameters]
) -> tuple[RatingSnapshot, ...]:
    """Replay each eligible match once, applying the parameter grid in parallel."""
    cutoff = utc(as_of)
    candidates = tuple(parameters)
    if not candidates:
        raise ValueError("跨联赛评分须至少指定一组参数")
    update_scale = np.array([entry.update_scale for entry in candidates], dtype=float)
    advantage = np.array([entry.home_advantage for entry in candidates], dtype=float)
    scale = np.array([entry.logistic_scale for entry in candidates], dtype=float)
    threshold = np.array([entry.draw_threshold for entry in candidates], dtype=float)
    ratings: dict[int, np.ndarray] = {}
    league_members: dict[str, set[int]] = defaultdict(set)
    pending: dict[str, np.ndarray] = {}
    events = _events(history, cutoff)
    for _, settled, _, _, match in events:
        home, away = int(match.home_id), int(match.away_id)
        members = league_members[match.league]
        if settled:
            margin = int(match.home_goals) - int(match.away_goals)
            actual = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
            update = update_scale * (1 + log(max(1, abs(margin))))
            update = update * (actual - pending.pop(match.match_id))
            ratings[home] += update
            ratings[away] -= update
            members.update((home, away))
            continue
        for team in (home, away):
            if team not in ratings:
                ratings[team] = _initial_rating(members, ratings, len(candidates))
        win, draw, _ = _probabilities(ratings[home], ratings[away], advantage, scale, threshold)
        pending[match.match_id] = win + 0.5 * draw

    teams = sorted(ratings)
    return tuple(
        RatingSnapshot(
            parameters=entry,
            as_of_utc=cutoff,
            ratings=MappingProxyType({team: float(ratings[team][index]) for team in teams}),
            training_rows=len(events) // 2,
            latest_available_utc=events[-1][0] if events else None,
        )
        for index, entry in enumerate(candidates)
    )
