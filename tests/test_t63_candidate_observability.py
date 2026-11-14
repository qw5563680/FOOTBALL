"""Offline fault injection using the existing t16/t17 training and target fixtures."""

import json
from datetime import timedelta

import pytest

from test_cloud_predict import NOW, scheduled_archive  # noqa: F401
from zucai import cloud_predict, source_version_archive, source_version_training
from zucai.cloud_feeds import restore_feeds
from zucai.daily_digest import generate
from zucai.forecast_archive import verify_forecasts

candidate_archive = scheduled_archive


@pytest.mark.parametrize("stage", ["training", "prediction", "freeze"])
def test_outer_candidate_failure_survives_restore_summary_and_digest(
    candidate_archive, tmp_path, monkeypatch, stage
):
    archive, params = candidate_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    primary = {}

    def fail(*args, **kwargs):
        primary.update({path: path.read_bytes() for path in archive.rglob("*") if path.is_file()})
        raise ValueError("synthetic private detail https://example.invalid/?api_key=DO_NOT_PUBLISH")

    if stage == "training":
        monkeypatch.setattr(source_version_training, "train_candidates", fail)
    elif stage == "prediction":
        monkeypatch.setattr(source_version_training, "predict_candidates", fail)
    else:
        # Let real attachment create the candidate files, then force verification
        # failure so its rollback and the outer failure recorder both run.
        monkeypatch.setattr(source_version_archive, "verify_source_versions", fail)
    result = cloud_predict.run(archive, params, "failed-candidate")
    status = result["source_version_status"]
    assert status["status"] == "failed"
    assert status["stage"] == stage
    assert status["reason"] == f"candidate_{stage}_failed"
    assert status["artifacts_frozen"] is False
    assert "source_versions" not in result
    assert verify_forecasts(archive)["runs"][result["run_id"]] == result
    assert primary
    assert all(
        path.read_bytes() == data
        for path, data in primary.items()
        if path.name != "forecast_manifest.json"
        and "source-expanded" not in path.name
        and path.name != "source_versions.json"
    )
    assert not list(archive.rglob("*.source-expanded.*"))
    assert not list(archive.rglob("source-expanded.params.json"))
    assert not list(archive.rglob("source_versions.json"))
    assert "DO_NOT_PUBLISH" not in json.dumps(result)
    assert status["reason"] in summary.read_text()
    assert "DO_NOT_PUBLISH" not in summary.read_text()
    restored = tmp_path / "restored" / "zucai.db"
    restore_feeds(archive, restored)
    assert restored.is_file()
    assert verify_forecasts(archive)["runs"][result["run_id"]]["source_version_status"] == status
    daily = generate(archive, tmp_path / "daily", now=NOW + timedelta(minutes=1))
    assert status["reason"] in daily.read_text()
    assert "新来源候选：失败" in daily.read_text()
    assert "DO_NOT_PUBLISH" not in daily.read_text()


def test_partial_candidate_training_is_visible_without_changing_primary(
    candidate_archive, tmp_path, monkeypatch
):
    archive, params = candidate_archive
    config = json.loads(params.read_bytes())
    config["leagues"]["西甲"] = dict(config["leagues"]["英超"])
    params.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    frozen = {}
    original = source_version_training.train_candidates

    def capture_primary(*args, **kwargs):
        frozen.update({path: path.read_bytes() for path in archive.rglob("*") if path.is_file()})
        return original(*args, **kwargs)

    monkeypatch.setattr(source_version_training, "train_candidates", capture_primary)
    result = cloud_predict.run(archive, params, "partial-candidate")
    status = result["source_version_status"]
    assert status["status"] == "partial"
    assert status["stage"] == "training"
    assert status["reason"] == "candidate_training_failed"
    assert status["failed_leagues"] == ["西甲"]
    assert status["artifacts_frozen"] is True
    assert result["source_versions"]["leagues"]["英超"]["status"] == "trained"
    assert result["source_versions"]["leagues"]["西甲"]["status"] == "failed"
    assert verify_forecasts(archive)["runs"][result["run_id"]] == result
    assert frozen
    assert all(
        path.read_bytes() == data
        for path, data in frozen.items()
        if path.name != "forecast_manifest.json"
    )
    assert "新来源候选：部分失败" in summary.read_text()
    daily = generate(archive, tmp_path / "daily", now=NOW + timedelta(minutes=1))
    assert "新来源候选：部分失败" in daily.read_text()
    assert "西甲" in daily.read_text()
