"""Walk-forward 1X2 evaluation; prices are held outside the fitting frame."""

import logging
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from zucai.models import ClosingOdds, Match, Odds, Result, Team
from zucai.predict.availability import replay_history
from zucai.predict.base import (
    availability_sources,
    normalize_sources,
    reject_cross_source_overlaps,
    utc,
)
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory
from zucai.predict.seasons import (
    parse_seasons,
    season_bounds,
    season_for,
    season_is_complete_eligible,
)

LOG = logging.getLogger(__name__)


def read_history(
    session: Session, league=None, source=None, *, replay_known_after=None
) -> pd.DataFrame:
    sources = normalize_sources(source)
    home, away = aliased(Team), aliased(Team)
    query = (
        select(
            Match.match_id,
            Match.source,
            Match.league,
            Match.counterpart_id,
            Match.kickoff_utc,
            Result.available_utc,
            Result.available_source,
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
    if sources is not None:
        query = query.where(Match.source.in_(sources))
    result = session.execute(query.order_by(Match.kickoff_utc, Match.match_id))
    frame = pd.DataFrame(result.all(), columns=result.keys())
    if frame.empty:
        raise InsufficientHistory("没有已完赛的赛果。可先运行公开样例")
    if frame.league.nunique() > 1:
        raise ValueError("存在多个联赛，请用 --league 选择单一联赛")
    if sources is None and frame.source.nunique() > 1:
        raise ValueError("存在多个来源，请用 --source 显式选择来源范围")
    frame.attrs["sources"] = sources or sorted(frame.source.unique())
    for key in ("kickoff_utc", "available_utc"):
        frame[key] = pd.to_datetime(frame[key], utc=True, format="ISO8601")
    reject_cross_source_overlaps(frame)
    frame["available_source"] = availability_sources(frame)
    frame.attrs["replay_delay"] = None
    if replay_known_after is not None:
        return replay_history(frame, replay_known_after)
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
    query = (
        select(Odds.match_id, Odds.snapshot_utc, Odds.selection, Odds.price)
        .join(Match, Match.match_id == Odds.match_id)
        .where(
            Match.match_id.in_(games.match_id.tolist()),
            Odds.pool == "had",
            Odds.line == 0,
            Odds.snapshot_utc < Match.kickoff_utc,
        )
    )
    return market_probabilities(
        session.execute(query), dict(zip(games.match_id, games.kickoff_utc, strict=True))
    )


def closing_probabilities(rows):
    """Publisher closing prices: comparison only, never executable or model inputs."""
    choices = {}
    preference = {"B365": 0, "PS": 1, "Avg": 2}
    for key, book, home, draw, away in rows:
        if book not in preference:
            continue
        values = np.asarray([home, draw, away], dtype=float)
        if not np.isfinite(values).all() or not (values > 1).all():
            continue
        if key not in choices or preference[book] < choices[key][0]:
            inverse = 1 / values
            choices[key] = preference[book], tuple(inverse / inverse.sum())
    return {key: value[1] for key, value in choices.items()}


def read_closing(session: Session, games: pd.DataFrame):
    return closing_probabilities(
        session.execute(
            select(
                ClosingOdds.match_id,
                ClosingOdds.bookmaker,
                ClosingOdds.h,
                ClosingOdds.d,
                ClosingOdds.a,
            ).where(
                ClosingOdds.match_id.in_(games.match_id.tolist()),
                ClosingOdds.pool == "had",
                ClosingOdds.basis == "publisher_closing",
            )
        )
    )


def _metrics(observations):
    if not observations:
        return {
            "n": 0,
            "log_loss": None,
            "brier": None,
            "calibration": [],
            "draw_mean": None,
            "draw_rate": None,
            "draw_bias": None,
        }
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
        "draw_mean": float(probs[:, 1].mean()),
        "draw_rate": float(target[:, 1].mean()),
        "draw_bias": float(probs[:, 1].mean() - target[:, 1].mean()),
    }


def _summary(records):
    observations = [(r["actual"], r["probabilities"]) for r in records]
    result = {
        "model": _metrics(observations),
        "uniform": _metrics([(r["actual"], (1 / 3,) * 3) for r in records]),
        "without_prior": _metrics(
            [(r["actual"], r["probabilities"]) for r in records if not r["unknown_team_prior"]]
        ),
        "unknown_team": _metrics(
            [(r["actual"], r["probabilities"]) for r in records if r["unknown_team_prior"]]
        ),
        "jc_linked": _metrics(
            [(r["actual"], r["probabilities"]) for r in records if r["jc_linked"]]
        ),
    }
    result["unknown_team_share"] = result["unknown_team"]["n"] / len(records) if records else 0.0
    for field, paired in (("market", "paired_model"), ("closing", "closing_paired_model")):
        rows = [r for r in records if r[field] is not None]
        result[field] = _metrics([(r["actual"], r[field]) for r in rows])
        result[paired] = _metrics([(r["actual"], r["probabilities"]) for r in rows])
    return result


def evaluate(
    history,
    market,
    *,
    since: datetime | None = None,
    as_of: datetime,
    half_life_days=90,
    strict_availability=False,
    seasons=None,
    fold="weekly",
    closing=None,
    unknown_team_prior=True,
    parameters_by_season=None,
    draw_calibration=False,
    calibration_by_season=None,
):
    if fold not in ("weekly", "monthly"):
        raise ValueError("fold 须为weekly或monthly")
    if history.empty or history.league.nunique() != 1:
        raise ValueError("评估须指定有历史的单一联赛")
    league = history.league.iloc[0]
    selected = parse_seasons(seasons, league)
    start = (
        utc(since)
        if since is not None
        else (
            season_bounds(selected[0], league)[0]
            if selected
            else history.kickoff_utc.min().normalize()
        )
    )
    end = utc(as_of)
    if start >= end:
        raise ValueError("回测开始时间须早于截止时间")
    if strict_availability:
        if (
            history.attrs.get("replay_delay") is not None
            or (availability_sources(history) == "replayed").any()
        ):
            raise ValueError("--strict-availability 与 --replay-known-after 互斥")
        history = history.loc[availability_sources(history) != "assumed"]
    period = history.loc[(history.kickoff_utc >= start) & (history.kickoff_utc < end)].copy()
    period["season"] = [season_for(k, league) for k in period.kickoff_utc]
    if selected:
        period = period.loc[period.season.isin(selected)]
    evaluation = period.loc[
        (period.available_utc < end) & (period.available_utc > period.kickoff_utc)
    ].copy()
    if fold == "weekly":
        boundaries = evaluation.kickoff_utc.dt.normalize() - pd.to_timedelta(
            evaluation.kickoff_utc.dt.day_of_week, unit="D"
        )
    else:
        boundaries = evaluation.kickoff_utc.map(lambda k: k.normalize().replace(day=1))
    # A week crossing the season boundary must not use the new season's chosen
    # parameter before that choice existed. Split it at the season boundary.
    evaluation["fold_start"] = [
        max(boundary, season_bounds(season, league)[0])
        for boundary, season in zip(boundaries, evaluation.season, strict=True)
    ]
    records, folds = [], []
    skipped = Counter()
    season_skipped = defaultdict(Counter)
    training_count = assumed_training_count = replayed_training_count = 0
    evaluated_count = assumed_eval_count = replayed_eval_count = 0
    closing = closing or {}
    for (season, boundary), fixtures in evaluation.groupby(["season", "fold_start"], sort=True):
        days = (parameters_by_season or {}).get(season, half_life_days)
        calibrate = (calibration_by_season or {}).get(season, draw_calibration)
        cutoff = boundary - pd.Timedelta(microseconds=1)
        info = dict(
            season=season,
            cutoff_utc=cutoff.isoformat(),
            half_life_days=days,
            draw_calibration=calibrate,
            eligible=len(fixtures),
            skipped={},
        )
        folds.append(info)
        try:
            model = DixonColes(
                days, unknown_team_prior=unknown_team_prior, draw_calibration=calibrate
            ).fit(history, as_of=cutoff)
        except (InsufficientHistory, ValueError) as exc:
            reason = "warmup" if isinstance(exc, InsufficientHistory) else "fit_failed"
            skipped[reason] += len(fixtures)
            season_skipped[season][reason] += len(fixtures)
            info.update(status=reason, skipped={reason: len(fixtures)}, **_summary([]))
            LOG.warning("截点 %s %s：%s", cutoff, reason, exc)
            continue
        info.update(
            status="fitted",
            training_games=model.training_rows,
            last_available_utc=model.latest_available.isoformat(),
            assumed_training_share=model.assumed_training_share,
            replayed_training_share=model.replayed_training_share,
            draw_offset=model.draw_offset,
            calibration_training_rows=model.calibration_training_rows,
            calibration_skipped_training_rows=model.calibration_skipped_training_rows,
        )
        training_count += model.training_rows
        assumed_training_count += model.assumed_training_rows
        replayed_training_count += model.replayed_training_rows
        fold_records = []
        for row in fixtures.to_dict("records"):
            try:
                matrix, meta = model.predict_with_metadata(row)
                probs = had(matrix)
            except (InsufficientHistory, ValueError) as exc:
                reason = "unknown_team" if isinstance(exc, InsufficientHistory) else "invalid_grid"
                skipped[reason] += 1
                season_skipped[season][reason] += 1
                info["skipped"][reason] = info["skipped"].get(reason, 0) + 1
                continue
            score = row["home_goals"] - row["away_goals"]
            counterpart = row.get("counterpart_id")
            counterpart = counterpart if isinstance(counterpart, str) else ""
            record = dict(
                match_id=row["match_id"],
                season=season,
                actual=0 if score > 0 else 1 if score == 0 else 2,
                probabilities=tuple(probs[k] for k in "hda"),
                unknown_team_prior=meta["unknown_team_prior"],
                jc_linked=(row["source"] == "jc" and bool(counterpart))
                or counterpart.startswith("jc:"),
                available_source=row.get("available_source") or "captured",
                closing=closing.get(row["match_id"]),
                market=market.get(row["match_id"]),
            )
            fold_records.append(record)
        records.extend(fold_records)
        n = len(fold_records)
        assumed = sum(r["available_source"] == "assumed" for r in fold_records)
        replayed = sum(r["available_source"] == "replayed" for r in fold_records)
        mixed = model.availability_basis != "strict" or any(
            r["available_source"] not in ("published", "captured") for r in fold_records
        )
        info.update(_summary(fold_records))
        info.update(
            assumed_eval_share=assumed / n if n else 0.0,
            replayed_eval_share=replayed / n if n else 0.0,
            availability_basis="replayed"
            if model.replayed_training_rows or replayed
            else "mixed"
            if mixed
            else "strict",
        )
        evaluated_count += n
        assumed_eval_count += assumed
        replayed_eval_count += replayed
    by_season = {}
    for season in selected or sorted(period.season.unique()):
        subset = [r for r in records if r["season"] == season]
        low, high = season_bounds(season, league)
        by_season[season] = dict(
            **_summary(subset),
            complete=start <= low and high <= end and season_is_complete_eligible(season, league),
            skipped=dict(season_skipped[season]),
            in_period=int((period.season == season).sum()),
            unavailable_labels=int(
                (period.season == season).sum() - (evaluation.season == season).sum()
            ),
        )
    return dict(
        **_summary(records),
        availability_basis="replayed"
        if replayed_training_count or replayed_eval_count
        else "mixed"
        if any(f.get("availability_basis") == "mixed" for f in folds)
        else "strict",
        assumed_training_share=assumed_training_count / training_count if training_count else 0.0,
        assumed_eval_share=assumed_eval_count / evaluated_count if evaluated_count else 0.0,
        replayed_training_share=replayed_training_count / training_count if training_count else 0.0,
        replayed_eval_share=replayed_eval_count / evaluated_count if evaluated_count else 0.0,
        replay_delay=history.attrs.get("replay_delay"),
        since_utc=start.isoformat(),
        as_of_utc=end.isoformat(),
        fold=fold,
        in_period=len(period),
        unavailable_labels=len(period) - len(evaluation),
        skipped=dict(skipped),
        folds=folds,
        seasons=by_season,
        predictions=records,
        closing_note="收盘价是赛前最后报价，由出版方赛后发布，仅作参照上限",
    )
