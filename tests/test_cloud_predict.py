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


@pytest.mark.parametrize("event,operation", [("schedule", ""), ("workflow_dispatch", "daily")])
def test_run_restores_trains_and_freezes_two_days_without_mutating_inputs(
    scheduled_archive, tmp_path, monkeypatch, event, operation
):
    from zucai import cloud_predict
    from zucai.forecast_archive import verify_forecasts

    archive, params = scheduled_archive
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    params_before = params.read_bytes()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("ZUCAI_OPERATION", operation)
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "offline-1", sporttery_status="skipped")
    assert result["event_name"] == event
    assert result["operation"] == (operation or None)
    assert result["status"] == "partial"
    assert result["leagues"]["芬超"]["status"] == "failed"
    good = result["leagues"]["英超"]
    assert good["training_rows"] == 84
    assert good["parameter_source"] == "params.json"
    assert good["p2_label"] == "可用（旧规，考核中）"
    model = DixonColes.load(archive / good["model_path"])
    assert model.sources == ["fd", "jc"]
    candidate = result["source_versions"]["leagues"]["英超"]
    assert result["source_version_status"]["status"] == "complete"
    assert result["source_version_status"]["artifacts_frozen"] is True
    assert candidate["status"] == "trained"
    assert candidate["p2_label"] == "实验" and candidate["paper_eligible"] is False
    assert candidate["sources"] == ["apif", "fd", "fdorg", "jc"]
    assert candidate["model_definition_id"] != good["model_definition_id"]
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
        parallel = json.loads((archive / forecast["source_versions_path"]).read_bytes())
        assert parallel["batch_id"] == batch["batch_id"]
        assert parallel["as_of_utc"] == batch["as_of_utc"]
        assert {row["match_id"] for row in parallel["predictions"]} == {
            row["match_id"] for row in batch["predictions"] if row.get("league") == "英超"
        }
        assert all(row["paper_eligible"] is False for row in parallel["predictions"])
    assert verify_forecasts(archive)["runs"]["offline-1"] == result
    assert all(p.read_bytes() == data for p, data in before.items())
    assert params.read_bytes() == params_before
    text = summary.read_text()
    assert "新来源候选：成功" in text
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


def test_failed_source_candidate_freezes_exclusions_and_preserves_primary(
    scheduled_archive, monkeypatch
):
    from zucai import cloud_predict
    from zucai.forecast_archive import verify_forecasts

    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    original = cloud_predict.train
    frozen = {}

    def candidate_failure(**kwargs):
        if kwargs["output"].name.endswith(".source-expanded.pkl"):
            frozen.update(
                {
                    path: path.read_bytes()
                    for path in archive.rglob("*")
                    if path.is_file() and path.name in {"forecast.json", "report.md", "英超.pkl"}
                }
            )
            assert frozen
            raise ValueError("synthetic candidate-only training failure")
        return original(**kwargs)

    monkeypatch.setattr(cloud_predict, "train", candidate_failure)
    result = cloud_predict.run(archive, params, "candidate-failed")
    assert result["leagues"]["英超"]["status"] == "trained"
    assert result["leagues"]["英超"]["p2_label"] == "可用（旧规，考核中）"
    assert result["source_versions"]["leagues"]["英超"]["status"] == "failed"
    assert result["source_version_status"]["status"] == "failed"
    assert result["source_version_status"]["stage"] == "training"
    assert result["source_version_status"]["failed_leagues"] == ["英超"]
    assert result["source_version_status"]["artifacts_frozen"] is True
    for forecast in result["forecasts"].values():
        primary = load_batch(archive / forecast["path"] / "forecast.json")
        candidate = json.loads((archive / forecast["source_versions_path"]).read_bytes())
        assert candidate["as_of_utc"] == primary["as_of_utc"]
        assert not candidate["models"]
        assert {row["match_id"] for row in candidate["predictions"]} == {
            row["match_id"] for row in primary["predictions"] if row["league"] == "英超"
        }
        assert all(row["status"] == "excluded" for row in candidate["predictions"])
        assert all(row["model_version"] is None for row in candidate["predictions"])
        assert all("model_probabilities" not in row for row in candidate["predictions"])
    assert all(path.read_bytes() == value for path, value in frozen.items())
    assert verify_forecasts(archive)["runs"]["candidate-failed"] == result


def test_original_experimental_league_uses_new_sources_in_primary(
    scheduled_archive, tmp_path, monkeypatch
):
    from sqlalchemy import update

    from zucai import cloud_predict

    _, params = scheduled_archive
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.execute(update(Match).where(Match.league == "英超").values(league="芬超"))
    engine.dispose()
    archive = tmp_path / "experimental-archive"
    export_feeds(database, archive, date(2026, 11, 2), "expanded-fixture")
    config = json.loads(params.read_bytes())
    config["leagues"] = {"芬超": config["leagues"]["芬超"]}
    params.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "expanded-primary")
    outcome = result["leagues"]["芬超"]
    assert outcome["status"] == "trained"
    assert outcome["sources"] == ["apif", "fd", "jc"]
    assert outcome["p2_label"] == "实验" and outcome["paper_eligible"] is False
    assert "source_versions" not in result
    for forecast in result["forecasts"].values():
        batch = load_batch(archive / forecast["path"] / "forecast.json")
        for row in batch["predictions"]:
            if row["league"] == "芬超":
                assert row["source_variant"] == "expanded"
                assert row["paper_eligible"] is False
                assert row["model_definition_id"] == outcome["model_definition_id"]


def test_partial_parameters_do_not_require_candidates_for_unconfigured_leagues(
    scheduled_archive, tmp_path, monkeypatch
):
    from sqlalchemy import update

    from zucai import cloud_predict
    from zucai.forecast_archive import verify_forecasts

    _, params = scheduled_archive
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.execute(update(Match).where(Match.match_id == "jc:t17-today").values(league="西甲"))
    engine.dispose()
    archive = tmp_path / "partial-archive"
    export_feeds(database, archive, date(2026, 11, 2), "partial-fixture")
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "partial-parameters")
    assert set(result["source_versions"]["leagues"]) == {"英超"}
    for forecast in result["forecasts"].values():
        primary = load_batch(archive / forecast["path"] / "forecast.json")
        candidate = json.loads((archive / forecast["source_versions_path"]).read_bytes())
        assert {row["match_id"] for row in candidate["predictions"]} == {
            row["match_id"] for row in primary["predictions"] if row["league"] == "英超"
        }
        assert all(
            row["status"] == "excluded" for row in primary["predictions"] if row["league"] == "西甲"
        )
    assert verify_forecasts(archive)["runs"]["partial-parameters"] == result


@pytest.mark.parametrize("schedule_state", ["failed", "complete", "legacy"])
def test_schedule_stage_controls_stale_even_when_sync_succeeds_today(
    scheduled_archive, tmp_path, monkeypatch, schedule_state
):
    from requests import ConnectionError

    from zucai import cloud_feeds, cloud_predict
    from zucai.forecast_archive import verify_forecasts
    from zucai.sources.sporttery import BEIJING

    archive, params = scheduled_archive
    database = tmp_path / "seed.db"
    today = NOW.astimezone(BEIJING).date()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    def schedule(*, metadata):
        if schedule_state == "failed":
            raise ConnectionError("offline schedule failure")
        metadata["captured_utc"] = NOW.isoformat()
        return 0, 0

    monkeypatch.setattr(cloud_feeds, "datetime", Clock)
    monkeypatch.setattr(cloud_feeds, "fetch_schedule", schedule)
    monkeypatch.setattr(cloud_feeds, "fetch_results", lambda *_: {})
    monkeypatch.setattr(cloud_feeds.time, "sleep", lambda _: None)
    jc, windows_complete = cloud_feeds.collect_jc(
        database, cloud_feeds.archive_info(archive), today, 3
    )
    assert windows_complete and jc["results_status"] == "complete"
    assert jc["schedule_status"] == ("failed" if schedule_state == "failed" else "complete")
    if schedule_state == "legacy":
        jc.pop("schedule_status")  # Older archives remain compatible.
    feed = export_feeds(database, archive, today, "sync-finished", collection={"jc": jc})
    assert feed["through_beijing"] == today.isoformat()
    assert feed["collection"]["jc"]["status"] == (
        "partial" if schedule_state == "failed" else "complete"
    )
    before = (archive / "feed_manifest.json").read_bytes()
    summary = tmp_path / "predict-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    result = cloud_predict.run(archive, params, "review-stale", sporttery_status="success")
    expected = schedule_state == "failed"
    assert result["sporttery_stale"] is expected
    assert verify_forecasts(archive)["runs"]["review-stale"]["sporttery_stale"] is expected
    assert ("竞彩数据非今日" in summary.read_text()) is expected
    if expected:
        assert "赛程阶段未完整成功" in summary.read_text()
    assert (archive / "feed_manifest.json").read_bytes() == before


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
