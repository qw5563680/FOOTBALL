"""T16 train output and fitted-artifact contracts on a checked-in offline corpus."""

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from zucai.cli import build_parser, main
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.command import train
from zucai.predict.dixon_coles import DixonColes

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures/t16"


@pytest.fixture
def training_db(tmp_path):
    database = tmp_path / "training.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in "ABCD"}
        session.add_all(teams.values())
        session.flush()
        for row in csv.DictReader((FIXTURES / "train_history.csv").open()):
            session.add(
                Match(
                    match_id=row["match_id"],
                    source=row["source"],
                    league=row["league"],
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=row["match_id"],
                    home_goals=int(row["home_goals"]),
                    away_goals=int(row["away_goals"]),
                    available_utc=row["available_utc"],
                    available_source=row["available_source"],
                )
            )
    engine.dispose()
    return database


def train_args(database, output):
    return [
        "train",
        "--db",
        str(database),
        "--source",
        "fd",
        "--league",
        "英超",
        "--since",
        "2026-10-01",
        "--as-of",
        "2026-11-03",
        "--output",
        str(output),
    ]


def test_train_default_stdout_keeps_summaries_without_prediction_arrays(
    training_db, tmp_path, capsys
):
    assert main(train_args(training_db, tmp_path / "model.pkl")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backtest"]["model"]["n"] > 0
    assert result["backtest"]["folds"] and result["backtest"]["seasons"]
    assert "predictions" not in result["backtest"]


@pytest.mark.parametrize("strict", [False, True])
def test_train_jsonl_keeps_full_records_and_distinguishes_strict_evaluation(
    training_db, tmp_path, capsys, strict
):
    target = tmp_path / "nested" / "predictions.jsonl"
    args = train_args(training_db, tmp_path / "model.pkl") + ["--predictions-out", str(target)]
    if strict:
        args += ["--strict-availability"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    records = [json.loads(line) for line in target.read_text().splitlines()]
    names = ["backtest", "strict_backtest"] if strict else ["backtest"]
    counts = Counter(record["evaluation"] for record in records)
    for name in names:
        summary = result[name]
        assert summary["predictions"] == {"path": str(target), "rows": summary["model"]["n"]}
        assert counts[name] == summary["model"]["n"]
    assert len(records) == sum(result[name]["model"]["n"] for name in names)
    assert all(record["match_id"].startswith("fd:t16-") for record in records)
    assert all("probabilities" in record for record in records)


def test_train_model_and_output_record_training_and_evaluation_database_hashes(
    training_db, tmp_path
):
    from zucai.predict.parameters import model_readiness

    output = tmp_path / "model.pkl"
    result = train(
        db=training_db,
        league="英超",
        source="fd",
        since="2026-10-01T00:00Z",
        as_of="2026-11-03T00:00Z",
        output=output,
    )
    model = DixonColes.load(output)
    digest = hashlib.sha256(training_db.read_bytes()).hexdigest()
    published = json.loads((ROOT / "data/model/params.json").read_text())
    evaluation = published["leagues"]["英超"].get("database_sha256")
    # The approved fd,jc definition keeps its original evaluation provenance.
    assert isinstance(evaluation, str) and len(evaluation) == 64
    assert digest != evaluation
    assert model.training_database_sha256 == result["training_database_sha256"] == digest
    assert model.evaluation_database_sha256 == result["evaluation_database_sha256"] == evaluation
    assert model.database_sha256 == digest
    assert Path(result["parameter_provenance"]["params_path"]).is_file()
    # This deliberately fd-only model cannot borrow that full definition's approval.
    assert not model_readiness(model, as_of="2026-11-03T00:00Z")["p2_ready"]


@pytest.mark.parametrize("target", ["database", "model", "parameters"])
def test_predictions_output_cannot_overwrite_training_inputs_or_model(
    training_db, tmp_path, capsys, target
):
    model = tmp_path / "model.pkl"
    config = tmp_path / "params.json"
    config.write_text('{"schema_version":1,"leagues":{}}')
    destination = {"database": training_db, "model": model, "parameters": config}[target]
    original = {p: p.read_bytes() for p in (training_db, config)}
    args = train_args(training_db, model) + [
        "--params-file",
        str(config),
        "--predictions-out",
        str(destination),
    ]
    assert main(args) == 2
    assert not capsys.readouterr().out
    assert all(path.read_bytes() == content for path, content in original.items())
    assert not model.exists()


def test_train_invalid_configuration_has_readable_error_without_traceback(
    training_db, tmp_path, caplog, capsys
):
    config = tmp_path / "invalid.json"
    config.write_text(
        json.dumps({"schema_version": 1, "leagues": {"英超": {"half_life_days": 90}}})
    )
    assert (
        main(train_args(training_db, tmp_path / "model.pkl") + ["--params-file", str(config)]) == 2
    )
    assert not capsys.readouterr().out
    assert "英超" in caplog.text and "selection_as_of_utc" in caplog.text
    assert "Traceback" not in caplog.text


def test_predictions_out_parser_keeps_train_and_tune_options_separate():
    parser = build_parser()
    args = parser.parse_args(["train", "--predictions-out", "rows.jsonl", "--strict-availability"])
    assert args.predictions_out == "rows.jsonl" and args.strict_availability
    tune = parser.parse_args(["tune", "--league", "英超", "--no-draw-calibration"])
    assert tune.draw_calibration is False


def test_published_jpn_legacy_window_only_maps_to_transition(training_db, tmp_path):
    with sqlite3.connect(training_db) as connection:
        connection.execute("UPDATE matches SET league='日职'")
    config = tmp_path / "jpn.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    "日职": {
                        "half_life_days": 180,
                        "selection_as_of_utc": "2026-01-01T00:00Z",
                        "evaluation_windows": [
                            {
                                "validation_season": "2026",
                                "selection_as_of_utc": "2026-01-01T00:00Z",
                                "half_life_days": 180,
                            }
                        ],
                    }
                },
            }
        )
    )
    before = config.read_bytes()
    result = train(
        db=training_db,
        league="日职",
        source="fd",
        as_of="2026-11-03T00:00Z",
        seasons=["2627"],
        params_file=config,
        output=tmp_path / "jpn.pkl",
    )
    assert result["half_life_days"] == 180
    assert result["evaluation_parameters"] == {"2026T": 180}
    assert set(result["backtest"]["seasons"]) == {"2627"}
    assert config.read_bytes() == before
