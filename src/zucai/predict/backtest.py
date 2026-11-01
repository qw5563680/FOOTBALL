"""Weekly walk-forward 1X2 evaluation. The market never enters the fitting frame."""

import logging
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from zucai.models import Match, Odds, Result, Team
from zucai.predict.base import utc
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory

LOG = logging.getLogger(__name__)


def read_history(session: Session, league=None, source=None) -> pd.DataFrame:
    home, away = aliased(Team), aliased(Team)
    query = (
        select(
            Match.match_id,
            Match.source,
            Match.league,
            Match.kickoff_utc,
            Result.available_utc,
            home.name.label("home"),
            away.name.label("away"),
            Result.home_goals,
            Result.away_goals,
        )
        .select_from(Match)
        .join(Result, Result.match_id == Match.match_id)
        .join(home, home.id == Match.home_team_id)
        .join(away, away.id == Match.away_team_id)
        .where(Match.status == "finished")
    )
    if league is not None:
        query = query.where(Match.league == league)
    if source is not None:
        query = query.where(Match.source == source)
    result = session.execute(query.order_by(Match.kickoff_utc, Match.match_id))
    frame = pd.DataFrame(result.all(), columns=result.keys())
    if frame.empty:
        raise InsufficientHistory("没有已完赛的赛果。可先运行公开样例")
    if len(frame[["league", "source"]].drop_duplicates()) > 1:
        raise ValueError("存在多个联赛或来源，请用 --league 和 --source 选择单一范围")
    for key in ("kickoff_utc", "available_utc"):
        frame[key] = pd.to_datetime(frame[key], utc=True, format="ISO8601")
    return frame


def market_probabilities(rows, kickoffs):
    """Latest complete, coherent, strictly pre-kickoff three-way decimal quote per match."""
    snapshots = defaultdict(dict)
    for key, stamp, selection, price in rows:
        if key in kickoffs and selection in ("h", "d", "a"):
            time = utc(stamp)
            if time < kickoffs[key]:
                snapshots[key, time][selection] = price
    latest = {}
    for (key, _time), snapshot in sorted(snapshots.items()):
        if set(snapshot) != {"h", "d", "a"}:
            continue
        prices = np.array([snapshot[k] for k in "hda"], dtype=float)
        if np.isfinite(prices).all() and (prices > 1).all():
            inverse = 1 / prices
            latest[key] = tuple(inverse / inverse.sum())
    return latest


def read_market(session: Session, games: pd.DataFrame):
    first = games.iloc[0]
    query = (
        select(Odds.match_id, Odds.snapshot_utc, Odds.selection, Odds.price)
        .join(Match, Match.match_id == Odds.match_id)
        .where(
            Match.league == first.league,
            Match.source == first.source,
            Odds.pool == "had",
            Odds.line == 0,
            Odds.snapshot_utc < Match.kickoff_utc,
        )
    )
    return market_probabilities(
        session.execute(query), dict(zip(games.match_id, games.kickoff_utc, strict=True))
    )


def _metrics(observations):
    if not observations:
        return {"n": 0, "log_loss": None, "brier": None, "calibration": []}
    actual, forecasts = zip(*observations, strict=True)
    probs = np.asarray(forecasts)
    target = np.eye(3)[list(actual)]
    bins = np.minimum((probs * 10).astype(int), 9)
    calibration = []
    for i in range(10):
        mask = bins == i
        if mask.any():
            calibration.append(
                {
                    "lower": i / 10,
                    "n": int(mask.sum()),
                    "mean_p": float(probs[mask].mean()),
                    "observed": float(target[mask].mean()),
                }
            )
    assigned = probs[np.arange(len(actual)), list(actual)]
    return {
        "n": len(actual),
        "log_loss": float(-np.log(np.maximum(assigned, 1e-15)).mean()),
        "brier": float(np.square(probs - target).sum(axis=1).mean()),
        "calibration": calibration,
    }


def evaluate(history, market, *, since: datetime, as_of: datetime, half_life_days=90):
    start, end = utc(since), utc(as_of)
    if start >= end:
        raise ValueError("回测开始时间须早于截止时间")
    period = history.loc[(history.kickoff_utc >= start) & (history.kickoff_utc < end)]
    evaluation = period.loc[
        (period.available_utc < end) & (period.available_utc > period.kickoff_utc)
    ]
    weeks = evaluation.kickoff_utc.dt.normalize() - pd.to_timedelta(
        evaluation.kickoff_utc.dt.day_of_week, unit="D"
    )
    all_model, paired_model, baseline = [], [], []
    folds = []
    skipped = Counter()
    for week, fixtures in evaluation.groupby(weeks, sort=True):
        try:
            model = DixonColes(half_life_days).fit(
                history, as_of=week - pd.Timedelta(microseconds=1)
            )
        except InsufficientHistory:
            skipped["warmup"] += len(fixtures)
            continue
        except ValueError as exc:
            skipped["fit_failed"] += len(fixtures)
            LOG.warning("周截点 %s 拟合失败：%s", week, exc)
            continue
        folds.append(
            {
                "cutoff_utc": model.as_of.isoformat(),
                "training_games": model.training_rows,
                "last_available_utc": model.latest_available.isoformat(),
            }
        )
        for row in fixtures.to_dict("records"):
            try:
                probs = had(model.predict_score_matrix(row))
            except InsufficientHistory:
                skipped["unknown_team"] += 1
                continue
            except ValueError as exc:
                skipped["invalid_grid"] += 1
                LOG.warning("比赛 %s 无可用比分分布：%s", row["match_id"], exc)
                continue
            result = row["home_goals"] - row["away_goals"]
            actual = 0 if result > 0 else 1 if result == 0 else 2
            observation = (actual, tuple(probs[k] for k in "hda"))
            all_model.append(observation)
            if row["match_id"] in market:
                paired_model.append(observation)
                baseline.append((actual, market[row["match_id"]]))
    return {
        "since_utc": start.isoformat(),
        "as_of_utc": end.isoformat(),
        "in_period": len(period),
        "unavailable_labels": len(period) - len(evaluation),
        "skipped": dict(skipped),
        "folds": folds,
        "model": _metrics(all_model),
        "paired_model": _metrics(paired_model),
        "market": _metrics(baseline),
    }
