"""Fixed-input legacy compatibility only: no scores, market reads, tuning or P2."""

import argparse
import hashlib
import inspect
import json
import logging
import socket
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
LOG = logging.getLogger(__name__)
CORE_COLUMNS = [
    "match_id",
    "source",
    "league",
    "counterpart_id",
    "kickoff_utc",
    "available_utc",
    "available_source",
    "home",
    "away",
    "home_goals",
    "away_goals",
]


def normal(value):
    import pandas as pd

    if isinstance(value, dict):
        return {str(key): normal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normal(item) for item in value]
    if isinstance(value, set):
        return sorted(normal(item) for item in value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if value is None or pd.isna(value):
        return None
    return value


def encoded(value):
    return (
        json.dumps(
            normal(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    raw = encoded(value)
    path.write_bytes(raw)
    return digest_bytes(raw)


def reject_network(*args, **kwargs):
    raise RuntimeError("Network is forbidden in compatibility acceptance")


def prepare_targets(census, database):
    seven = {"英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲"}
    targets = []
    for row in json.loads(census.read_bytes()):
        if row["league"] in seven:
            targets.append(
                {
                    **{
                        key: row[key]
                        for key in (
                            "match_id",
                            "source",
                            "league",
                            "home",
                            "away",
                            "kickoff_utc",
                            "cutoff_utc",
                        )
                    },
                    "group": "original_seven",
                    "model_league": row["league"],
                    "fixed_cutoff_evidence": row.get("publication_proof"),
                    "forecast_path": row.get("forecast_path"),
                    "no_cutoff_reason": row.get("cutoff_exclusion"),
                }
            )
    with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT m.match_id,m.source,m.league,m.kickoff_utc,h.name home,a.name away FROM matches m JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id WHERE league IN ('欧冠','欧罗巴','欧协联') AND kickoff_utc>='2026-11-14T00:00:00Z' AND kickoff_utc<'2026-11-28T00:00:00Z' ORDER BY kickoff_utc,match_id"
        )
        for row in rows:
            targets.append(
                dict(
                    row,
                    group="european_pool",
                    model_league="欧战",
                    cutoff_utc="2026-11-14T00:00:00Z",
                    fixed_cutoff_evidence="predeclared fixed 00:00 input compatibility control; natural scheduled target",
                )
            )
    return targets


def run(args):
    import numpy as np
    import pandas as pd
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from zucai.predict.backtest import read_history
    from zucai.predict.dixon_coles import DixonColes
    from zucai.predict.parameters import resolve_parameters

    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    targets = json.loads(args.targets.read_bytes())
    config = json.loads(args.params.read_bytes())["leagues"]
    before = digest_bytes(args.database.read_bytes())
    uri = args.database.resolve().as_uri() + "?mode=ro&immutable=1"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    summary = {
        "arm": args.arm,
        "code_commit": args.code_sha,
        "database_sha256": before,
        "targets_sha256": digest_bytes(args.targets.read_bytes()),
        "params_sha256": digest_bytes(args.params.read_bytes()),
        "fixed_input": "e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4",
        "no_scoring_or_tuning_or_p2": True,
        "network_requests": 0,
        "modules": {
            "read_history": inspect.getfile(read_history),
            "DixonColes": inspect.getfile(DixonColes),
            "resolve_parameters": inspect.getfile(resolve_parameters),
        },
        "fits": {},
        "targets": [],
    }
    groups = sorted(
        {
            (row["model_league"], row["cutoff_utc"])
            for row in targets
            if row["cutoff_utc"]
        }
    )
    models = {}
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                for league, cutoff_text in groups:
                    cutoff = pd.Timestamp(cutoff_text)
                    entry = config[league]
                    key = digest_bytes(encoded([league, cutoff_text]))[:20]
                    record = {
                        "league": league,
                        "cutoff_utc": cutoff_text,
                        "sources": entry["sources"],
                        "complete_parameter_entry_sha256": digest_bytes(encoded(entry)),
                    }
                    try:
                        kwargs = dict(
                            league=league,
                            source=",".join(entry["sources"]),
                            training_leagues=entry.get("training_leagues"),
                        )
                        if (
                            args.arm != "baseline"
                            and "as_of" in inspect.signature(read_history).parameters
                        ):
                            kwargs["as_of"] = cutoff
                        if args.source_version:
                            kwargs["source_version"] = args.source_version
                        history = read_history(session, **kwargs)
                        record["read_history_rows"] = len(history)
                        record["read_history_core_sha256"] = digest_bytes(
                            encoded(history[CORE_COLUMNS].to_dict("records"))
                        )
                        record["read_history_all_columns_sha256"] = digest_bytes(
                            encoded(history.to_dict("records"))
                        )
                        record["read_history_attrs_sha256"] = digest_bytes(
                            encoded(history.attrs)
                        )
                        if args.arm == "baseline":
                            selected = history.loc[
                                (history.kickoff_utc < cutoff)
                                & (history.available_utc < cutoff)
                                & (history.available_utc > history.kickoff_utc)
                            ].copy()
                        else:
                            from zucai.predict.pooled import select_training_history

                            selected = select_training_history(history, cutoff)
                        record["selected_rows"] = len(selected)
                        record["selected_source_counts"] = (
                            selected.source.value_counts().to_dict()
                        )
                        record["selected_core_sha256"] = digest_bytes(
                            encoded(selected[CORE_COLUMNS].to_dict("records"))
                        )
                        record["training_id_sha256"] = write(
                            output / f"{key}-training-ids.json", list(selected.match_id)
                        )
                        parameters = resolve_parameters(
                            league, as_of=cutoff, params_file=args.params
                        )
                        record["resolved_parameters"] = {
                            name: parameters[name]
                            for name in (
                                "half_life_days",
                                "draw_calibration",
                                "selection_as_of_utc",
                                "source",
                            )
                        }
                        model = DixonColes(
                            parameters["half_life_days"],
                            unknown_team_prior=True,
                            draw_calibration=parameters["draw_calibration"],
                            pooled=league == "欧战",
                        ).fit(history, as_of=cutoff)
                        numerical = {
                            "engine_parameters": model.engine.get_params(),
                            "optimizer_parameters": model.engine._params,
                            "loglikelihood": model.engine.loglikelihood,
                            "prior_parameters": model.prior_parameters,
                            "draw_offset": model.draw_offset,
                            "calibration_training_rows": model.calibration_training_rows,
                            "calibration_skipped_training_rows": model.calibration_skipped_training_rows,
                        }
                        # Full float values are serialized, not rounded and not compared by tolerance.
                        numerical["optimizer_parameters"] = list(
                            numerical["optimizer_parameters"]
                        )
                        record["numeric_sha256"] = write(
                            output / f"{key}-numeric.json", numerical
                        )
                        record["model_training_rows"] = model.training_rows
                        if model.training_rows != len(selected):
                            raise ValueError(
                                "Selected IDs differ from actual model training row count"
                            )
                        record["status"] = "fitted"
                        models[league, cutoff_text] = model
                    except (ValueError, KeyError, TypeError) as exc:
                        record.update(
                            status="blocked", error=f"{type(exc).__name__}: {exc}"
                        )
                    summary["fits"][key] = record
                    LOG.info(
                        "%s %s %s %s", args.arm, league, cutoff_text, record["status"]
                    )
                    write(output / "summary.json", summary)
                for target in targets:
                    result = dict(
                        target,
                        status="not_generated",
                        validation_status="compatibility_control_only",
                        model_name="dixon-coles",
                    )
                    cutoff = target["cutoff_utc"]
                    if cutoff is None:
                        result["reason"] = "no_verified_pre_kickoff_cutoff"
                    else:
                        key = digest_bytes(encoded([target["model_league"], cutoff]))[
                            :20
                        ]
                        result["fit_key"] = key
                        model = models.get((target["model_league"], cutoff))
                        if model is None:
                            result["reason"] = summary["fits"][key]["error"]
                        else:
                            try:
                                grid, metadata = model.predict_with_metadata(target)
                                probabilities = [
                                    float(np.tril(grid, -1).sum()),
                                    float(np.trace(grid)),
                                    float(np.triu(grid, 1).sum()),
                                ]
                                result.update(
                                    status="predicted",
                                    probabilities=probabilities,
                                    score_matrix=grid.tolist(),
                                    prediction_metadata=metadata,
                                    model_version=summary["fits"][key][
                                        "numeric_sha256"
                                    ],
                                )
                            except ValueError as exc:
                                result["reason"] = f"{type(exc).__name__}: {exc}"
                    summary["targets"].append(result)
    finally:
        engine.dispose()
    summary["database_unchanged"] = before == digest_bytes(args.database.read_bytes())
    assert summary["database_unchanged"]
    summary["finished_utc"] = datetime.now(UTC).isoformat()
    write(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("database", "params", "targets", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--arm", required=True, choices=("baseline", "current"))
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--source-version")
    args = parser.parse_args()
    socket.create_connection = reject_network
    import requests

    requests.sessions.Session.request = reject_network
    run(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
