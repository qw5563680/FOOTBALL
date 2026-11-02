import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Prediction, Team
from zucai.predict.dixon_coles import DixonColes

CUTOFF = datetime(2026, 11, 1, 12, tzinfo=UTC)


def test_batch_provenance_records_config_path_and_both_database_hashes(setup_batch, tmp_path):
    from zucai.report.batch import predict_batch

    engine, path, model, _ = setup_batch
    model.training_database_sha256 = "b" * 64
    model.save(path)
    config = tmp_path / "params.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leagues": {
                    "L": {
                        "half_life_days": 90,
                        "selection_as_of_utc": "2026-10-01T00:00Z",
                        "database_sha256": "c" * 64,
                    }
                },
            }
        )
    )
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1"],
            params_file=config,
        )
    metadata = batch["models"][0]
    assert metadata["training_database_sha256"] == "b" * 64
    assert metadata["evaluation_database_sha256"] == "c" * 64
    assert metadata["parameter_provenance"]["params_path"] == str(config.resolve())


@pytest.fixture
def setup_batch(tmp_path):
    rng = np.random.default_rng(92)
    records = []
    for n in range(100):
        home = n % 4
        away = (home + 1 + (n // 4) % 3) % 4
        kickoff = CUTOFF - timedelta(days=110 - n)
        records.append(
            {
                "match_id": f"jc:history-{n}",
                "source": "jc",
                "league": "L",
                "home": "ABCD"[home],
                "away": "ABCD"[away],
                "kickoff_utc": kickoff,
                "available_utc": kickoff + timedelta(hours=3),
                "available_source": "captured",
                "home_goals": rng.poisson(1.5),
                "away_goals": rng.poisson(1.1),
            }
        )
    path = tmp_path / "dc.pkl"
    model = DixonColes(unknown_team_prior=False).fit(
        pd.DataFrame(records), as_of=CUTOFF - timedelta(days=1)
    )
    model.save(path)
    db = tmp_path / "matches.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in ("A", "B", "Unknown")}
        session.add_all(teams.values())
        session.flush()
        for key, home, kickoff, status in (
            ("jc:1", "A", CUTOFF + timedelta(hours=1), "scheduled"),
            ("jc:2", "Unknown", CUTOFF + timedelta(hours=2), "scheduled"),
            ("jc:3", "A", CUTOFF, "scheduled"),
            ("jc:4", "A", CUTOFF + timedelta(hours=2), "cancelled"),
        ):
            session.add(
                Match(
                    match_id=key,
                    source="jc",
                    league="L",
                    home_team_id=teams[home].id,
                    away_team_id=teams["B"].id,
                    kickoff_utc=kickoff,
                    status=status,
                    jc_num="周日001",
                )
            )
        session.flush()
        for stamp, quotes in (
            (CUTOFF - timedelta(minutes=10), {"h": 2.0, "d": 3.0, "a": 6.0}),
            (CUTOFF - timedelta(minutes=1), {"h": 9.0}),
            (CUTOFF + timedelta(seconds=1), {"h": 5.0, "d": 5.0, "a": 5.0}),
        ):
            for option, price in quotes.items():
                session.add(
                    Odds(
                        match_id="jc:1",
                        pool="had",
                        line=0,
                        selection=option,
                        price=price,
                        snapshot_utc=stamp,
                    )
                )
    yield engine, path, model, db
    engine.dispose()


def test_batch_really_runs_model_and_keeps_market_independent(setup_batch):
    from zucai.report.batch import predict_batch

    engine, path, model, _ = setup_batch
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1", "jc:2", "jc:3", "jc:4", "jc:missing"],
        )
    rows = {row["match_id"]: row for row in batch["predictions"]}
    good = rows["jc:1"]
    expected = model.predict_score_matrix(
        {"home": "A", "away": "B", "kickoff_utc": CUTOFF + timedelta(hours=1)}
    )
    np.testing.assert_array_equal(good["score_matrix"], expected)
    assert sum(good["model_probabilities"].values()) == pytest.approx(1)
    assert good["market"]["prices"] == {"h": 2.0, "d": 3.0, "a": 6.0}
    assert good["market"]["probabilities"] == pytest.approx({"h": 0.5, "d": 1 / 3, "a": 1 / 6})
    assert good["ev"]["h"] == pytest.approx(good["model_probabilities"]["h"] * 2 - 1)
    assert all(
        rows[key]["status"] == "excluded" and rows[key]["reason"]
        for key in ("jc:2", "jc:3", "jc:4", "jc:missing")
    )
    assert batch["probability_type"] == "P2" and batch["bet_allowed"] is False
    with session_scope(engine) as session:
        predictions = session.scalars(select(Prediction)).all()
        assert len(predictions) == 1 and predictions[0].model_name == "dixon-coles"
        assert predictions[0].mode == "prematch"
        assert predictions[0].batch_id == batch["batch_id"]
        assert datetime.fromisoformat(predictions[0].as_of_utc) == CUTOFF
        assert predictions[0].model_version == good["model_version"]
        assert len(good["model_version"]) == 64
        assert good["metadata"]["training_games"] == 100
        for quote in session.scalars(select(Odds)):
            session.delete(quote)
    with session_scope(engine) as session:
        second = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF + timedelta(seconds=1),
            replay=True,
            match_ids=["jc:1"],
        )
    assert second["predictions"][0]["model_probabilities"] == good["model_probabilities"]
    assert second["predictions"][0]["market"] is None
    with session_scope(engine) as session:
        replayed = session.scalars(
            select(Prediction).where(Prediction.batch_id == second["batch_id"])
        ).one()
        assert replayed.mode == "historical_replay"


def test_future_model_cutoff_excluded_and_beijing_day_selection(setup_batch):
    from zucai.report.batch import predict_batch

    engine, path, _, _ = setup_batch
    with session_scope(engine) as session:
        batch = predict_batch(
            session, model_files=[path], as_of=CUTOFF, generated=CUTOFF, day=date(2026, 11, 1)
        )
        assert len(batch["predictions"]) == 4
        future = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF - timedelta(days=2),
            generated=CUTOFF,
            replay=True,
            match_ids=["jc:1"],
        )
    assert future["predictions"][0]["status"] == "excluded"
    assert "截点" in future["predictions"][0]["reason"]


def test_experimental_mix_is_separate_and_requires_complete_quote(setup_batch):
    from zucai.report.batch import predict_batch

    engine, path, _, _ = setup_batch
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1"],
            market_weight=0.25,
        )
        stored = session.scalars(select(Prediction)).all()
        assert {row.model_name for row in stored} == {"dixon-coles", "dixon-coles+market"}
    row = batch["predictions"][0]
    blend = row["experimental_blend"]
    assert blend["model_version"] != row["model_version"]
    assert blend["probabilities"]["h"] == pytest.approx(
        0.75 * row["model_probabilities"]["h"] + 0.25 * 0.5
    )
    assert blend["market_dependent"] is True
    assert sum(
        np.asarray(next(p for p in stored if p.model_name.endswith("+market")).score_matrix).flat
    ) == pytest.approx(1)
    with session_scope(engine) as session:
        for quote in session.scalars(select(Odds)):
            session.delete(quote)
    with session_scope(engine) as session, pytest.raises(ValueError, match="完整"):
        predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF + timedelta(seconds=1),
            replay=True,
            match_ids=["jc:1"],
            market_weight=0.25,
        )


def test_historical_cutoff_requires_explicit_replay(setup_batch):
    from zucai.report.batch import predict_batch

    engine, path, _, _ = setup_batch
    with session_scope(engine) as session, pytest.raises(ValueError, match="回放"):
        predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF + timedelta(days=1),
            match_ids=["jc:1"],
        )


def test_cli_frozen_report_survives_database_refresh_and_refuses_overwrite(setup_batch, tmp_path):
    from zucai.cli import main

    engine, model, _, db = setup_batch
    output = tmp_path / "frozen"
    args = [
        "predict",
        "--db",
        str(db),
        "--model-file",
        str(model),
        "--match-id",
        "jc:1",
        "--as-of",
        CUTOFF.isoformat(),
        "--replay",
        "--out",
        str(output),
    ]
    assert main(args) == 0
    before = (output / "forecast.json").read_bytes()
    frozen = json.loads(before)
    assert frozen["mode"] == "historical_replay"
    assert frozen["generated_utc"] != frozen["as_of_utc"]
    with session_scope(engine) as session:
        session.get(Match, "jc:1").status = "finished"
        for price in session.scalars(select(Odds)):
            price.price = 99
    assert (
        main(
            ["report", "--batch", str(output / "forecast.json"), "--out", str(tmp_path / "export")]
        )
        == 0
    )
    assert (tmp_path / "export/forecast.json").read_bytes() == before
    with session_scope(engine) as session:
        count = len(session.scalars(select(Prediction)).all())
    assert main(args) != 0
    assert (output / "forecast.json").read_bytes() == before
    with session_scope(engine) as session:
        assert len(session.scalars(select(Prediction)).all()) == count


def test_report_write_failure_rolls_back_new_predictions(setup_batch, tmp_path, monkeypatch):
    from zucai.cli import main

    engine, model, _, db = setup_batch

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("zucai.report.command.write_report", fail)
    assert (
        main(
            [
                "predict",
                "--db",
                str(db),
                "--model-file",
                str(model),
                "--match-id",
                "jc:1",
                "--as-of",
                CUTOFF.isoformat(),
                "--replay",
                "--out",
                str(tmp_path / "failed"),
            ]
        )
        != 0
    )
    with session_scope(engine) as session:
        assert session.scalars(select(Prediction)).all() == []
    assert not (tmp_path / "failed").exists()


def test_commit_failure_removes_created_report_and_rolls_back_rows(setup_batch, tmp_path):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from zucai.cli import main

    engine, model, _, db = setup_batch
    output = tmp_path / "failed-commit"

    def fail_commit(session):
        raise SQLAlchemyError("commit unavailable")

    event.listen(Session, "before_commit", fail_commit)
    try:
        assert (
            main(
                [
                    "predict",
                    "--db",
                    str(db),
                    "--model-file",
                    str(model),
                    "--match-id",
                    "jc:1",
                    "--as-of",
                    CUTOFF.isoformat(),
                    "--replay",
                    "--out",
                    str(output),
                ]
            )
            != 0
        )
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert not output.exists()
    with session_scope(engine) as session:
        assert session.scalars(select(Prediction)).all() == []


def test_csv_batch_identity_and_missing_scope_are_reported(setup_batch, tmp_path):
    from zucai.cli import main

    engine, model, _, db = setup_batch
    ids = tmp_path / "matches.csv"
    ids.write_text("match_id,league\njc:1,L\njc:2,L\njc:missing,L\n", encoding="utf-8-sig")
    with session_scope(engine) as session:
        session.get(Match, "jc:1").league = "Different league"
    out = tmp_path / "csv-report"
    args = [
        "predict",
        "--db",
        str(db),
        "--model-file",
        str(model),
        "--matches",
        str(ids),
        "--as-of",
        CUTOFF.isoformat(),
        "--replay",
        "--out",
        str(out),
    ]
    assert main(args) == 0
    frozen = json.loads((out / "forecast.json").read_bytes())
    assert len(frozen["predictions"]) == 3
    assert all(row["status"] == "excluded" for row in frozen["predictions"])
    assert "不一致" in frozen["predictions"][0]["reason"]


def test_replay_default_day_uses_cutoff_day_instead_of_generation_day(
    setup_batch, tmp_path, monkeypatch
):
    from zucai.cli import main

    _, model, _, db = setup_batch

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CUTOFF + timedelta(days=3)

    monkeypatch.setattr("zucai.report.command.datetime", LaterClock)
    output = tmp_path / "replay-default"
    assert (
        main(
            [
                "predict",
                "--db",
                str(db),
                "--model-file",
                str(model),
                "--as-of",
                CUTOFF.isoformat(),
                "--replay",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    batch = json.loads((output / "forecast.json").read_bytes())
    assert batch["input"]["kickoff_date_beijing"] == "2026-11-01"
    assert len(batch["predictions"]) == 4


def test_prematch_crossing_kickoff_during_export_is_not_frozen(setup_batch, tmp_path, monkeypatch):
    from zucai.cli import main
    from zucai.report.render import write_report

    engine, model, _, db = setup_batch

    class Clock(datetime):
        instant = CUTOFF

        @classmethod
        def now(cls, tz=None):
            return cls.instant

    monkeypatch.setattr("zucai.report.command.datetime", Clock)

    def slow_export(batch, output):
        result = write_report(batch, output)
        Clock.instant = CUTOFF + timedelta(hours=2)
        return result

    monkeypatch.setattr("zucai.report.command.write_report", slow_export)
    output = tmp_path / "too-late"
    assert (
        main(
            [
                "predict",
                "--db",
                str(db),
                "--model-file",
                str(model),
                "--match-id",
                "jc:1",
                "--out",
                str(output),
            ]
        )
        != 0
    )
    assert not output.exists()
    with session_scope(engine) as session:
        assert session.scalars(select(Prediction)).all() == []


def test_generation_time_is_completion_time_and_cutoff_remains_frozen(
    setup_batch, tmp_path, monkeypatch
):
    from zucai.cli import main

    engine, model, _, db = setup_batch

    class Clock(datetime):
        ticks = iter([CUTOFF + timedelta(seconds=i) for i in (0, 5, 10, 15)])

        @classmethod
        def now(cls, tz=None):
            return next(cls.ticks)

    monkeypatch.setattr("zucai.report.command.datetime", Clock)
    output = tmp_path / "completed"
    assert (
        main(
            [
                "predict",
                "--db",
                str(db),
                "--model-file",
                str(model),
                "--match-id",
                "jc:1",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    batch = json.loads((output / "forecast.json").read_bytes())
    assert datetime.fromisoformat(batch["as_of_utc"]) == CUTOFF
    assert datetime.fromisoformat(batch["generated_utc"]) == CUTOFF + timedelta(seconds=5)
    with session_scope(engine) as session:
        row = session.scalars(select(Prediction)).one()
        assert datetime.fromisoformat(row.created_utc) == CUTOFF + timedelta(seconds=5)
        assert row.mode == "prematch"


def test_legacy_prediction_rows_remain_unknown_after_batch_columns_are_added(setup_batch):
    engine, _, _, _ = setup_batch
    with session_scope(engine) as session:
        session.add(
            Prediction(
                match_id="jc:1",
                model_name="old",
                model_version="old",
                p_home=0.5,
                p_draw=0.2,
                p_away=0.3,
                score_matrix=[],
            )
        )
    with engine.begin() as connection:
        for field in ("batch_id", "mode", "as_of_utc"):
            connection.exec_driver_sql(f"ALTER TABLE predictions DROP COLUMN {field}")
    init_db(engine)
    with session_scope(engine) as session:
        row = session.scalars(select(Prediction)).one()
        assert row.model_name == "old" and (row.batch_id, row.mode, row.as_of_utc) == (
            None,
            None,
            None,
        )


def test_t15_prior_readiness_and_frozen_render_keep_bet_disabled(setup_batch, tmp_path):
    from zucai.report.batch import predict_batch
    from zucai.report.render import _csv_bytes, _markdown, _validate_batch

    engine, path, model, _ = setup_batch
    model.unknown_team_prior = True
    policy = dict(history_scope="cumulative", strict_availability=False, replay_known_after=None)
    model.database_sha256 = "a" * 64
    model.training_policy = policy
    model.save(path)
    config = tmp_path / "params.json"
    entry = dict(
        half_life_days=model.half_life_days,
        selection_as_of_utc="2026-07-01T00:00Z",
        generated_utc="2026-07-01T00:00Z",
        sources=["jc"],
        unknown_team_prior="weakest_quartile_half_shrinkage",
        draw_calibration=False,
        p2_ready=True,
        database_sha256="a" * 64,
        training_policy=policy,
        p2_evidence={"as_of_utc": "2026-07-01T00:00Z", "seasons": ["2425", "2526"]},
    )
    config.write_text(json.dumps({"schema_version": 1, "leagues": {"L": entry}}))
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1", "jc:2"],
            params_file=config,
        )
    assert batch["predictions"][1]["metadata"]["unknown_team_prior"] is True
    assert all(row["p2_ready"] for row in batch["predictions"])
    _validate_batch(batch)
    assert "P2 可用" in _markdown(batch)
    assert "p2_ready" in _csv_bytes(batch).decode("utf-8-sig")
    assert batch["bet_allowed"] is False
    # Rendering freezes the decision; changing a config does not rewrite old reports.
    original = _markdown(batch)
    config.unlink()
    assert _markdown(batch) == original
