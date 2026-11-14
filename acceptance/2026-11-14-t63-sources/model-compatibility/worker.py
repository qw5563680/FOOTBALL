"""Fixed-input model compatibility only: no prediction, scoring, market read or tuning."""
import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import responses
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import resolve_parameters


def normal(value):
    if isinstance(value, dict):
        return {str(key): normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [normal(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if value is None or pd.isna(value):
        return None
    return value


def encoded(value):
    return (json.dumps(normal(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_bytes(encoded(value))
    return sha(path)


def model_artifacts(frame, league, parameters, cutoff, output, label):
    model = DixonColes(
        parameters["half_life_days"],
        unknown_team_prior=league != "欧国联",
        draw_calibration=parameters["draw_calibration"],
        pooled=league == "欧战",
    ).fit(frame, as_of=cutoff)
    numerical = {
        "parameters": model.engine.get_params(),
        "prior_parameters": model.prior_parameters,
        "draw_offset": model.draw_offset,
        "calibration_training_rows": model.calibration_training_rows,
        "calibration_skipped_training_rows": model.calibration_skipped_training_rows,
    }
    metadata = {
        key: getattr(model, key, None) for key in (
            "league", "as_of", "half_life_days", "sources", "training_rows", "latest_available",
            "assumed_training_rows", "replayed_training_rows", "availability_basis",
            "training_source_counts", "training_leagues", "fitting_method", "solver_diagnostics",
        )
    }
    numbers = output / f"{league}-{label}-numeric.json"
    meta = output / f"{league}-{label}-metadata.json"
    model_path = output / f"{league}-{label}.pkl"
    model.save(model_path)
    return {
        "numeric_sha256": write(numbers, numerical), "metadata_sha256": write(meta, metadata),
        "pickle_sha256": sha(model_path), "training_rows": model.training_rows,
    }


def run(args):
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    database, params = Path(args.db), Path(args.params)
    before = sha(database)
    cutoff = pd.Timestamp(args.cutoff)
    config = json.loads(params.read_bytes())["leagues"]
    uri = database.resolve().as_uri() + "?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    result = {
        "started_utc": datetime.now(UTC).isoformat(), "database_sha256": before,
        "cutoff_utc": cutoff.isoformat(), "params_sha256": sha(params), "arm": args.arm,
        "no_prediction_scoring_or_tuning": True, "network_requests": 0, "models": {},
    }
    try:
        with responses.RequestsMock(assert_all_requests_are_fired=False):
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                with Session(bind=connection) as session:
                    for league, entry in config.items():
                        print(f"START {args.arm} {league}", flush=True)
                        row = {"sources": entry["sources"]}
                        try:
                            history = read_history(
                                session, league=league, source=",".join(entry["sources"]),
                                training_leagues=entry.get("training_leagues"),
                            )
                            if args.arm == "baseline":
                                selected = history.loc[(history.kickoff_utc < cutoff) & (history.available_utc < cutoff) & (history.available_utc > history.kickoff_utc)].copy()
                            else:
                                from zucai.predict.pooled import select_training_history
                                selected = select_training_history(history, cutoff)
                            columns = ["match_id", "source", "league", "counterpart_id", "kickoff_utc", "available_utc", "available_source", "home", "away", "home_goals", "away_goals"]
                            records = selected[columns].to_dict("records")
                            row["selected_sha256"] = write(output / f"{league}-selected.json", records)
                            row["selected_rows"] = len(records)
                            row["selected_source_counts"] = selected.source.value_counts().to_dict()
                            parameters = resolve_parameters(league, as_of=cutoff, params_file=params)
                            row["fit_parameters"] = {key: parameters[key] for key in ("half_life_days", "draw_calibration", "selection_as_of_utc")}
                            row["native"] = model_artifacts(history, league, parameters, cutoff, output, "native")
                            if args.baseline:
                                path = Path(args.baseline) / f"{league}-selected.json"
                                previous = json.loads(path.read_bytes())
                                old = {item["match_id"]: item for item in previous}
                                new = {item["match_id"]: normal(item) for item in records}
                                delta = {
                                    "removed": [old[key] for key in sorted(old.keys() - new.keys())],
                                    "added": [new[key] for key in sorted(new.keys() - old.keys())],
                                    "changed": [{"before": old[key], "after": new[key]} for key in sorted(old.keys() & new.keys()) if old[key] != new[key]],
                                }
                                row["differences"] = {key: len(value) for key, value in delta.items()}
                                write(output / f"{league}-input-differences.json", delta)
                                # A separate unchanged-input control: ensure fitting arithmetic
                                # still gives the same numbers, not a source-benefit experiment.
                                control = pd.DataFrame(previous)
                                for field in ("kickoff_utc", "available_utc"):
                                    control[field] = pd.to_datetime(control[field], utc=True, format="ISO8601")
                                control.attrs["sources"] = json.loads((Path(args.baseline) / "summary.json").read_bytes())["models"][league]["sources"]
                                if entry.get("training_leagues"):
                                    control.attrs["training_leagues"] = entry["training_leagues"]
                                row["same_input_control"] = model_artifacts(control, league, parameters, cutoff, output, "same-input-control")
                            row["status"] = "fitted"
                        except (ValueError, KeyError) as exc:
                            row.update(status="blocked", error=f"{type(exc).__name__}: {exc}")
                        result["models"][league] = row
                        write(output / "summary.json", result)
                        print(f"DONE {args.arm} {league} {row['status']}", flush=True)
    finally:
        engine.dispose()
    result["completed_utc"] = datetime.now(UTC).isoformat()
    result["database_unchanged"] = before == sha(database)
    assert result["database_unchanged"]
    write(output / "summary.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("db", "params", "out", "arm"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--cutoff", default="2026-11-14T00:00:00Z")
    run(parser.parse_args())
