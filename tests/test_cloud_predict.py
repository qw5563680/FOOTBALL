"""Offline restore -> real fitting -> two Beijing-day frozen report archives."""

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from zucai.cloud_feeds import digest, export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import model_readiness
from zucai.report.render import load_batch

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


@pytest.fixture
def scheduled_archive(tmp_path):
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in "ABCD"}
        session.add_all(teams.values())
        session.flush()
        with (ROOT / "data/fixtures/t16/train_history.csv").open() as source:
            history = list(csv.DictReader(source))
        upcoming = json.loads((ROOT / "data/fixtures/t17/upcoming.json").read_bytes())
        for row in history + upcoming:
            session.add(
                Match(
                    match_id=row["match_id"],
                    source=row["source"],
                    league=row["league"],
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished" if "home_goals" in row else "scheduled",
                )
            )
            session.flush()
            if "home_goals" in row:
                session.add(
                    Result(
                        match_id=row["match_id"],
                        home_goals=int(row["home_goals"]),
                        away_goals=int(row["away_goals"]),
                        available_utc=row["available_utc"],
                        available_source=row["available_source"],
                    )
                )
            for selection, price in zip("hda", row.get("had", []), strict=False):
                session.add(
                    Odds(
                        match_id=row["match_id"],
                        pool="had",
                        line=0,
                        selection=selection,
                        price=price,
                        snapshot_utc=NOW.isoformat(),
                    )
                )
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 2), "seed")
    config = json.loads((ROOT / "data/model/params.json").read_bytes())
    config["leagues"] = {k: v for k, v in config["leagues"].items() if k in {"英超", "芬超"}}
    params = tmp_path / "params.json"
    params.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return archive, params


def test_run_restores_trains_and_freezes_two_days_without_mutating_inputs(
    scheduled_archive, tmp_path, monkeypatch
):
    from zucai import cloud_predict
    from zucai.forecast_archive import verify_forecasts

    archive, params = scheduled_archive
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    params_before = params.read_bytes()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "offline-1", sporttery_status="skipped")
    assert result["status"] == "partial"
    assert result["leagues"]["芬超"]["status"] == "failed"
    good = result["leagues"]["英超"]
    assert good["training_rows"] == 84
    assert good["parameter_source"] == "params.json"
    assert good["p2_label"] == "P2 可用"
    model = DixonColes.load(archive / good["model_path"])
    assert model.sources == ["fd", "jc"]
    assert good["p2_ready"] == model_readiness(model, as_of=NOW, params_file=params)["p2_ready"]
    assert good["sha256"] == digest(archive / good["model_path"])
    assert model.training_database_sha256 == result["database_sha256"]
    train_result = json.loads((archive / good["training_json_path"]).read_bytes())
    assert "predictions" not in train_result["backtest"]
    assert train_result["strict_availability"] is False
    assert train_result["replay_delay"] is None
    assert set(result["forecasts"]) == {"2026-11-03", "2026-11-04"}
    today, tomorrow = result["forecasts"].values()
    assert (today["predicted"], today["excluded"]) == (1, 2)
    assert today["reasons"] == {"预测截点时已开赛": 1, "缺少同来源同联赛的模型": 1}
    assert (tomorrow["predicted"], tomorrow["excluded"]) == (1, 0)
    for forecast in result["forecasts"].values():
        batch = load_batch(archive / forecast["path"] / "forecast.json")
        assert batch["input"]["market_weight"] == 0
        assert all(row["source"] == "jc" for row in batch["predictions"])
        for row in batch["predictions"]:
            if row["status"] == "predicted":
                assert sum(row["model_probabilities"].values()) == pytest.approx(1)
                assert row["market"]["probabilities"]
                assert row["prospective_verified"] is False
                assert row["prospective_n"] == 0
        assert batch["models"][0]["p2_label"] == good["p2_label"]
    assert verify_forecasts(archive)["runs"]["offline-1"] == result
    assert all(p.read_bytes() == data for p, data in before.items())
    assert params.read_bytes() == params_before
    text = summary.read_text()
    assert text.startswith("非购买建议 · BET 禁用 · P2 标签含义见 README\n")
    assert "竞彩数据非今日" in text
    assert "英超" in text and "芬超" in text and "缺少同来源同联赛的模型" in text
    assert "jc:t17-today" in text and "jc:t17-tomorrow" in text
    previous = {
        p: p.read_bytes()
        for p in archive.rglob("*")
        if p.is_file() and p.name not in {"latest.json", "forecast_manifest.json"}
    }
    with pytest.raises(ValueError, match="已存在"):
        cloud_predict.run(archive, params, "offline-1")
    cloud_predict.run(archive, params, "offline-2")
    assert all(p.read_bytes() == data for p, data in previous.items())


def test_all_models_fail_and_next_day_empty_still_archives_exclusions(
    scheduled_archive, monkeypatch
):
    from zucai import cloud_predict

    archive, params = scheduled_archive
    config = json.loads(params.read_bytes())
    config["leagues"] = {"芬超": config["leagues"]["芬超"]}
    params.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(cloud_predict, "_now", lambda: datetime(2026, 11, 4, 17, tzinfo=UTC))
    result = cloud_predict.run(archive, params, "empty")
    assert result["status"] == "partial"
    assert all(f["predicted"] == 0 for f in result["forecasts"].values())
    assert result["forecasts"]["2026-11-06"]["excluded"] == 0
    assert json.loads((archive / "models/latest.json").read_bytes())["models"] == {}


def test_kickoff_during_export_rolls_back_run(scheduled_archive, monkeypatch):
    from zucai import cloud_predict

    archive, params = scheduled_archive
    clock = [NOW]
    monkeypatch.setattr(cloud_predict, "_now", lambda: clock[0])
    original = cloud_predict.write_report

    def slow_export(batch, path):
        original(batch, path)
        clock[0] = datetime(2026, 11, 3, 15, tzinfo=UTC)

    monkeypatch.setattr(cloud_predict, "write_report", slow_export)
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="已开赛"):
        cloud_predict.run(archive, params, "late")
    assert {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("bad", ["missing", "empty", "source", "run_id"])
def test_invalid_run_configuration_fails_before_training(scheduled_archive, monkeypatch, bad):
    from zucai import cloud_predict

    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    config = json.loads(params.read_bytes())
    if bad == "missing":
        params.unlink()
    elif bad == "empty":
        config["leagues"] = {}
    elif bad == "source":
        config["leagues"]["英超"]["sources"].append("apif")
    if bad != "missing":
        params.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        cloud_predict.run(archive, params, "../bad" if bad == "run_id" else "bad")
    assert not (archive / "models").exists()


@pytest.mark.parametrize("target", ["database", "params"])
def test_changed_training_input_blocks_publication(scheduled_archive, monkeypatch, target):
    from zucai import cloud_predict

    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    original = cloud_predict.train

    def changing_input(**kwargs):
        trained = original(**kwargs)
        if target == "params":
            params.write_bytes(params.read_bytes() + b"\n")
        else:
            # Appending a byte changes the corpus fingerprint without invalidating SQLite reads.
            database = Path(kwargs["db"])
            database.write_bytes(database.read_bytes() + b"\x00")
        return trained

    monkeypatch.setattr(cloud_predict, "train", changing_input)
    with pytest.raises(ValueError, match="发生变化"):
        cloud_predict.run(archive, params, "changed-input")
    assert not (archive / "forecast_manifest.json").exists()


def test_failed_retrain_does_not_predict_using_previous_latest_model(
    scheduled_archive, monkeypatch
):
    from zucai import cloud_predict

    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    cloud_predict.run(archive, params, "first")

    def failed_fit(**kwargs):
        raise ValueError("offline fit failed")

    monkeypatch.setattr(cloud_predict, "train", failed_fit)
    result = cloud_predict.run(archive, params, "second")
    assert all(f["predicted"] == 0 for f in result["forecasts"].values())
    assert result["forecasts"]["2026-11-04"]["reasons"] == {"缺少同来源同联赛的模型": 1}
    latest = json.loads((archive / "models/latest.json").read_bytes())
    assert latest["models"]["英超"]["model_path"] == "models/first/英超.pkl"
