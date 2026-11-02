"""Offline contracts for append-only forecast files and their independent manifest."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from zucai.report.render import write_report


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staged_run(root, run_id, *, failed=False):
    """Opaque model bytes deliberately cannot be unpickled by the archive verifier."""
    stage = root / run_id
    model_dir = stage / "models" / run_id
    model_dir.mkdir(parents=True)
    stamp = "2026-11-02T18:00:00+00:00"
    model_path = f"models/{run_id}/英超.pkl"
    train_path = f"models/{run_id}/英超.train.json"
    training = {"training_database_sha256": "b" * 64, "model_path": model_path}
    (stage / train_path).write_text(json.dumps(training), encoding="utf-8")
    league = {"status": "failed", "reason": "离线样本不足", "training_json_path": train_path}
    models = []
    if not failed:
        (stage / model_path).write_bytes(b"opaque model archive fixture\x00")
        league = {
            "status": "trained",
            "model_path": model_path,
            "training_json_path": train_path,
            "sha256": _sha(stage / model_path),
            "training_cutoff_utc": stamp,
            "training_database_sha256": "b" * 64,
            "p2_ready": True,
            "p2_label": "P2 可用",
        }
        models = [
            {
                "model_name": "dixon-coles",
                "model_version": league["sha256"],
                "sources": ["fd", "jc"],
                "league": "英超",
                "model_as_of_utc": stamp,
                "last_available_utc": "2026-11-01T15:00:00+00:00",
                "training_games": 84,
                "availability_basis": "mixed",
                "assumed_training_share": 0.25,
                "p2_ready": True,
                "p2_label": "P2 可用",
            }
        ]
    forecasts = {}
    for day in ("2026-11-03", "2026-11-04"):
        directory = f"forecasts/{day}/{run_id}"
        batch = {
            "schema_version": 2,
            "batch_id": f"{run_id}-{day}",
            "generated_utc": stamp,
            "as_of_utc": stamp,
            "mode": "prematch",
            "probability_type": "P2",
            "validation_status": "league_gated",
            "bet_allowed": False,
            "input": {"match_ids": [], "kickoff_date_beijing": day, "market_weight": 0},
            "models": models,
            "predictions": [],
        }
        write_report(batch, stage / directory)
        forecasts[day] = {
            "path": directory,
            "batch_id": batch["batch_id"],
            "generated_utc": stamp,
            "predicted": 0,
            "excluded": 0,
            "reasons": {},
        }
    return stage, {
        "run_id": run_id,
        "generated_utc": stamp,
        "training_cutoff_utc": stamp,
        "database_sha256": "b" * 64,
        "params_sha256": "c" * 64,
        "status": "partial" if failed else "complete",
        "leagues": {"英超": league},
        "forecasts": forecasts,
    }


def test_legacy_archive_without_forecasts_needs_no_new_manifest(tmp_path):
    from zucai.forecast_archive import verify_forecasts

    assert verify_forecasts(tmp_path) is None


def test_publish_hashes_all_files_and_keeps_latest_success_after_failed_retrain(tmp_path):
    from zucai.forecast_archive import publish_forecasts, verify_forecasts

    archive = tmp_path / "archive"
    stage, run = staged_run(tmp_path / "staging", "run-1")
    manifest = publish_forecasts(archive, stage, run)
    assert manifest["runs"]["run-1"] == run
    assert len(manifest["files"]) == 11  # Model, train JSON, two quartets, latest index.
    assert all(_sha(archive / name) == sha for name, sha in manifest["files"].items())
    assert verify_forecasts(archive) == manifest
    original = {
        name: (archive / name).read_bytes()
        for name in manifest["files"]
        if name != "models/latest.json"
    }

    failed_stage, failed_run = staged_run(tmp_path / "staging", "run-2", failed=True)
    second = publish_forecasts(archive, failed_stage, failed_run)
    assert second["runs"]["run-2"]["status"] == "partial"
    assert all((archive / name).read_bytes() == data for name, data in original.items())
    latest = json.loads((archive / "models/latest.json").read_text())
    assert latest["models"]["英超"]["model_path"] == "models/run-1/英超.pkl"
    assert verify_forecasts(archive) == second


def test_duplicate_run_is_rejected_without_touching_existing_files(tmp_path):
    from zucai.forecast_archive import publish_forecasts

    archive = tmp_path / "archive"
    stage, run = staged_run(tmp_path / "staging", "run-1")
    publish_forecasts(archive, stage, run)
    before = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="已存在"):
        publish_forecasts(archive, stage, run)
    assert {
        p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize(
    "target",
    [
        "models/run-1/英超.pkl",
        "models/run-1/英超.train.json",
        "models/latest.json",
        "forecasts/2026-11-03/run-1/report.csv",
        "forecasts/2026-11-04/run-1/manifest.json",
    ],
)
def test_verify_detects_corrupted_model_report_or_index(tmp_path, target):
    from zucai.forecast_archive import publish_forecasts, verify_forecasts

    archive = tmp_path / "archive"
    stage, run = staged_run(tmp_path / "staging", "run-1")
    publish_forecasts(archive, stage, run)
    (archive / target).write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        verify_forecasts(archive)


@pytest.mark.parametrize("attack", ["outside", "missing", "symlink", "manifest_missing"])
def test_verify_rejects_unsafe_or_incomplete_forecast_archives(tmp_path, attack):
    from zucai.forecast_archive import publish_forecasts, verify_forecasts

    archive = tmp_path / "archive"
    stage, run = staged_run(tmp_path / "staging", "run-1")
    manifest = publish_forecasts(archive, stage, run)
    model = archive / "models/run-1/英超.pkl"
    if attack == "outside":
        manifest["files"]["../outside.pkl"] = manifest["files"]["models/run-1/英超.pkl"]
        (archive / "forecast_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif attack == "missing":
        model.unlink()
    elif attack == "symlink":
        outside = tmp_path / "outside.pkl"
        outside.write_bytes(model.read_bytes())
        model.unlink()
        model.symlink_to(outside)
    else:
        (archive / "forecast_manifest.json").unlink()
    with pytest.raises(ValueError):
        verify_forecasts(archive)


def test_publish_rolls_back_new_run_if_index_write_fails(tmp_path, monkeypatch):
    from zucai import forecast_archive

    archive = tmp_path / "archive"
    stage, run = staged_run(tmp_path / "staging", "run-1")
    forecast_archive.publish_forecasts(archive, stage, run)
    before = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    stage2, run2 = staged_run(tmp_path / "staging", "run-2")
    original = Path.replace

    def fail_manifest(path, destination):
        if Path(destination) == archive / "forecast_manifest.json":
            raise OSError("fixture index failure")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_manifest)
    with pytest.raises(OSError, match="fixture index failure"):
        forecast_archive.publish_forecasts(archive, stage2, run2)
    assert {
        p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    } == before


def test_publish_rejects_paths_from_a_different_run(tmp_path):
    from zucai.forecast_archive import publish_forecasts

    stage, run = staged_run(tmp_path / "staging", "run-1")
    bad = copy.deepcopy(run)
    bad["leagues"]["英超"]["model_path"] = "models/old/英超.pkl"
    with pytest.raises(ValueError):
        publish_forecasts(tmp_path / "archive", stage, bad)


@pytest.mark.parametrize("corrupt", [False, True])
def test_hosted_verify_checks_forecast_manifest_as_well_as_feed_archive(
    tmp_path, monkeypatch, corrupt
):
    import sys
    from datetime import date

    from zucai.cloud_feeds import export_feeds, main
    from zucai.db import get_engine, init_db
    from zucai.forecast_archive import publish_forecasts

    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 2), "seed")
    stage, run = staged_run(tmp_path / "staging", "run-1")
    publish_forecasts(archive, stage, run)
    if corrupt:
        (archive / "models/run-1/英超.pkl").write_bytes(b"bad")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    summary = work / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        sys, "argv", ["feeds", "verify", "--archive", str(archive), "--run-id", "verify"]
    )
    if corrupt:
        with pytest.raises(ValueError, match="SHA256"):
            main()
        assert not summary.exists()
    else:
        main()
        assert "预测归档已校验：1 次运行" in summary.read_text()
