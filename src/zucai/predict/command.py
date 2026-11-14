"""Training entry point used by the CLI and the standalone public replay."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.predict.backtest import evaluate, read_closing, read_history, read_market
from zucai.predict.base import availability_sources, utc
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import (
    TRAINING_POLICY,
    parameters_path,
    read_parameters,
    resolve_parameters,
)
from zucai.predict.pooled import POOL, SOURCES, configuration, default_scope
from zucai.predict.seasons import season_bounds


def parse_cutoff(value):
    if len(value) == 10:
        value += "T00:00:00+00:00"
    return utc(value)


def load_corpus(
    db=None, league=None, source=None, *, replay_known_after=None, params_file=None, as_of=None
):
    config, _ = read_parameters(params_file)
    entry = config["leagues"].get(league, {})
    pool = configuration(league, entry)
    if pool and source is None:
        source = ",".join(entry.get("sources", SOURCES))
    database = Path(db or os.environ.get("ZUCAI_DB", "data/zucai.db")).expanduser().resolve()
    if not database.is_file():
        raise ValueError("输入数据库不存在，请先恢复已有归档或采集数据")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    engine = get_engine(database)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only=ON")
            conn.exec_driver_sql("BEGIN")
            with Session(bind=conn) as session:
                scope = None
                if pool:
                    scope = pool["training_leagues"] or default_scope(
                        session,
                        [name for name in config["leagues"] if name != POOL],
                        as_of=utc(as_of if as_of is not None else datetime.now(UTC)),
                    )
                games = read_history(
                    session,
                    league,
                    source,
                    replay_known_after=replay_known_after,
                    training_leagues=scope,
                )
                market = read_market(session, games, jc_only=bool(pool))
                closing = {} if pool else read_closing(session, games)
                if pool:
                    games.attrs["prediction_leagues"] = pool["leagues"]
    finally:
        engine.dispose()
    if hashlib.sha256(database.read_bytes()).hexdigest() != digest:
        raise ValueError("数据库在读取期间发生变化，请使用静态副本")
    return games, market, closing, digest


def train(
    *,
    db=None,
    league=None,
    source=None,
    since=None,
    as_of=None,
    half_life_days=None,
    output=Path("models/dixon-coles.pkl"),
    strict_availability=False,
    replay_known_after=None,
    seasons=None,
    fold="weekly",
    params_file=None,
    draw_calibration=None,
    predictions_out=None,
):
    if strict_availability and replay_known_after is not None:
        raise ValueError("--strict-availability 与 --replay-known-after 互斥")
    cutoff = utc(as_of if as_of is not None else datetime.now(UTC))
    database = Path(db or os.environ.get("ZUCAI_DB", "data/zucai.db")).expanduser().resolve()
    if database == Path(output).resolve():
        raise ValueError("模型输出文件与数据库路径相同")
    if predictions_out is not None:
        destination = Path(predictions_out).expanduser().resolve()
        if destination in {database, Path(output).resolve(), parameters_path(params_file)}:
            raise ValueError("逐场输出不能与数据库、模型或参数文件路径相同")
    games, prices, closing, digest = load_corpus(
        db,
        league,
        source,
        replay_known_after=replay_known_after,
        params_file=params_file,
        as_of=cutoff,
    )
    pool = league == POOL
    league = POOL if pool else games.league.iloc[0]
    parameters = resolve_parameters(
        league, as_of=cutoff, params_file=params_file, half_life_days=half_life_days
    )
    training = games.loc[availability_sources(games) != "assumed"] if strict_availability else games
    calibrate = parameters["draw_calibration"] if draw_calibration is None else draw_calibration
    parameters["draw_calibration"] = calibrate
    parameters["draw_calibration_source"] = (
        "cli"
        if draw_calibration is not None
        else "params.json"
        if parameters["params_sha256"]
        else "default"
    )
    model = DixonColes(
        parameters["half_life_days"],
        unknown_team_prior=league != "欧国联",
        draw_calibration=calibrate,
        **({"pooled": True} if pool else {}),
    ).fit(training, as_of=cutoff)
    model.parameter_source = parameters["source"]
    model.parameter_provenance = parameters
    model.database_sha256 = digest
    model.training_database_sha256 = digest
    model.training_policy = {
        **TRAINING_POLICY,
        "strict_availability": strict_availability,
        "replay_known_after": model.replay_delay,
    }
    config, _ = read_parameters(params_file)
    model.evaluation_database_sha256 = config["leagues"].get(league, {}).get("database_sha256")
    mapping, calibration_mapping = {}, {}
    for window in config["leagues"].get(league, {}).get("evaluation_windows", []):
        season = window["validation_season"]
        # The published T15 JPN file used a calendar-year label before B1 segmentation.
        if league in {"日职", "JPN"} and season == "2026":
            season = "2026T"
        if utc(window["selection_as_of_utc"]) <= season_bounds(season, league)[0]:
            if half_life_days is None:
                mapping[season] = window["half_life_days"]
            if draw_calibration is None:
                calibration_mapping[season] = window.get("draw_calibration", False)
    # Do not back-project today's recommended parameter into its own selection data.
    evaluation_days = half_life_days if half_life_days is not None else 90
    if league == "欧国联":
        # Fixed in advance for this research model, never selected by historical outcomes.
        evaluation_days = parameters["half_life_days"]
    options = dict(
        since=since,
        as_of=cutoff,
        half_life_days=evaluation_days,
        unknown_team_prior=league != "欧国联",
        seasons=seasons,
        fold=fold,
        closing=closing,
        parameters_by_season=mapping,
        draw_calibration=draw_calibration if draw_calibration is not None else False,
        calibration_by_season=calibration_mapping,
        **({"target_leagues": games.attrs["prediction_leagues"]} if pool else {}),
    )
    report = evaluate(games, prices, **options)
    strict_report = (
        evaluate(games, prices, strict_availability=True, **options)
        if strict_availability
        else None
    )
    model.replayed_eval_share = (strict_report or report)["replayed_eval_share"]
    summaries = _prediction_summaries(report, strict_report, predictions_out)
    from zucai.predict.model_definitions import definition_metadata

    definition = definition_metadata(model, config["leagues"].get(league, {}))
    for key, value in definition.items():
        setattr(model, key, value)
    model.save(output)
    return {
        **definition,
        "model_name": "dixon-coles",
        "engine": "penaltyblog 1.13.1",
        "league": model.league,
        "sources": model.sources,
        "training_source_counts": model.training_source_counts,
        "training_rows": model.training_rows,
        "availability_basis": model.availability_basis,
        "assumed_training_share": model.assumed_training_share,
        "replayed_training_share": model.replayed_training_share,
        "replayed_eval_share": model.replayed_eval_share,
        "replay_delay": model.replay_delay,
        "strict_availability": strict_availability,
        "cutoff_utc": cutoff.isoformat(),
        "last_available_utc": model.latest_available.isoformat(),
        "half_life_days": model.half_life_days,
        "parameter_source": model.parameter_source,
        "parameter_provenance": parameters,
        "draw_calibration": calibrate,
        "draw_calibration_source": parameters["draw_calibration_source"],
        "draw_offset": model.draw_offset,
        "calibration_training_rows": model.calibration_training_rows,
        "evaluation_parameters": mapping,
        "evaluation_calibration": calibration_mapping,
        "evaluation_fallback_half_life_days": evaluation_days,
        "database_sha256": digest,
        "training_database_sha256": model.training_database_sha256,
        "evaluation_database_sha256": model.evaluation_database_sha256,
        "market_source": "pre-kickoff had snapshots",
        "closing_source": "publisher closing; B365 > PS > Avg; comparison only",
        "model_path": str(output),
        **(
            {
                "pooled": True,
                "fitting_method": model.fitting_method,
                "solver_diagnostics": model.solver_diagnostics,
                "prediction_leagues": model.prediction_leagues,
                "training_leagues": model.training_leagues,
                "training_league_scope": model.training_league_scope,
                "linked_rows_excluded": games.attrs.get("linked_rows_excluded", []),
                "home_advantage": float(model.engine.get_params()["home_advantage"]),
                "market_source": "jc pre-kickoff had snapshots only",
                "closing_source": None,
            }
            if pool
            else {}
        ),
        **summaries,
    }


def _prediction_summaries(backtest, strict_backtest, predictions_out):
    """Keep evaluate() full fidelity while train exposes summaries and optional JSONL."""
    reports = {"backtest": backtest, "strict_backtest": strict_backtest}
    stream = None
    if predictions_out is not None:
        path = Path(predictions_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("x", encoding="utf-8")
    result = {}
    try:
        for name, report in reports.items():
            if report is None:
                result[name] = None
                continue
            summary = dict(report)
            records = summary.pop("predictions")
            if stream is not None:
                for record in records:
                    stream.write(
                        json.dumps(
                            {**record, "evaluation": name}, ensure_ascii=False, allow_nan=False
                        )
                        + "\n"
                    )
                summary["predictions"] = {"path": str(predictions_out), "rows": len(records)}
            result[name] = summary
    except BaseException:
        if stream is not None:
            stream.close()
            path.unlink(missing_ok=True)
        raise
    finally:
        if stream is not None:
            stream.close()
    return result
