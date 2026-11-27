"""Offline checks for publishing production forecasts before independent experiments."""
# ruff: noqa: F811

import json
import shutil
from datetime import timedelta

import pytest

from forecast_publication_support import confirm_fixture
from test_cloud_predict import NOW, scheduled_archive  # noqa: F401
from test_stats_rows import LocalRelease
from zucai import cloud_predict, experimental_archive
from zucai.cloud_feeds import digest
from zucai.daily_digest import generate
from zucai.forecast_archive import REPORT_FILES, verify_forecasts
from zucai.predict.experiments.global_rating_contract import RATING_NAME
from zucai.scoreboard.command import run as settle

RUN_ID = "t88-offline"
EXPERIMENT_AT = NOW + timedelta(minutes=30)


def _official_bytes(archive, run):
    names = {"models/latest.json"}
    for league in run["leagues"].values():
        names.add(league["training_json_path"])
        if league["status"] == "trained":
            names.add(league["model_path"])
    for forecast in run["forecasts"].values():
        names.update(f"{forecast['path']}/{filename}" for filename in REPORT_FILES)
    return {name: (archive / name).read_bytes() for name in names}


def _freeze_production(archive, params, directory, monkeypatch):
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    run = cloud_predict.run(archive, params, RUN_ID, production_state=directory)
    assert digest(directory / "zucai.db") == run["database_sha256"]
    confirm_fixture(archive)
    assert run["forecasts"]["2026-11-03"]["predicted"] == 1
    assert run["forecasts"]["2026-11-04"]["predicted"] == 1
    for name in ("experimental.json", "source_versions.json", "parlay_sd.json"):
        assert not list(archive.rglob(name))
    monkeypatch.setattr(cloud_predict, "_now", lambda: EXPERIMENT_AT)
    return verify_forecasts(archive)["runs"][RUN_ID]


def test_split_and_legacy_runs_freeze_identical_official_files(
    scheduled_archive, tmp_path, monkeypatch
):
    archive, params = scheduled_archive
    legacy, state = tmp_path / "legacy-archive", tmp_path / "production-state"
    shutil.copytree(archive, legacy)
    release = LocalRelease(tmp_path / "release")
    worker = experimental_archive.freeze_parallel
    began = []

    def offline_experiment(archive, *args, **kwargs):
        began.append(archive)
        return worker(archive, *args, **kwargs, release_factory=lambda: release)

    monkeypatch.setattr(experimental_archive, "freeze_parallel", offline_experiment)
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    existing = cloud_predict.run(legacy, params, RUN_ID)
    expected = _official_bytes(legacy, existing)

    primary = _freeze_production(archive, params, state, monkeypatch)
    assert began == [legacy]
    assert _official_bytes(archive, primary) == expected

    finished = cloud_predict.run_experiments(archive, params, RUN_ID, state)
    assert began == [legacy, archive]
    assert finished["source_versions"]["leagues"]["英超"]["status"] == "trained"
    assert finished["experimental_release"]["asset"]["tag"] == "experiments-202611"
    assert all(
        {"experimental_path", "parlay_path", "source_versions_path"} <= entry.keys()
        for entry in finished["forecasts"].values()
    )
    assert all(
        forecast["publication_evidence"] == primary["forecasts"][day]["publication_evidence"]
        for day, forecast in finished["forecasts"].items()
    )
    assert _official_bytes(archive, finished) == expected


@pytest.mark.parametrize(
    ("failure", "want_reason", "want_text"),
    [
        ("compute_error", "experiment_failed:OSError", "实验数据或计算失败"),
        ("experiment_timeout", "experiment_timeout", "实验超过时间预算"),
        ("experiment_cancelled", "experiment_cancelled", "实验运行被取消"),
        ("experiment_publication_failed", "experiment_publication_failed", "实验发布失败"),
    ],
)
def test_unavailable_experiments_keep_published_forecasts_and_allow_settlement_and_digest(
    scheduled_archive, tmp_path, monkeypatch, failure, want_reason, want_text
):
    archive, params = scheduled_archive
    state = tmp_path / "production-state"
    primary = _freeze_production(archive, params, state, monkeypatch)
    frozen = _official_bytes(archive, primary)

    if failure == "compute_error":
        worker = experimental_archive.freeze_parallel
        release = LocalRelease(tmp_path / "release")

        def fail(*args, **kwargs):
            raise OSError("synthetic experimental computation failure")

        monkeypatch.setattr(experimental_archive, "compute", fail)
        monkeypatch.setattr(
            experimental_archive,
            "freeze_parallel",
            lambda *args, **kwargs: worker(*args, **kwargs, release_factory=lambda: release),
        )
        cloud_predict.run_experiments(archive, params, RUN_ID, state)
        assert not list(release.root.rglob("*.zip"))
    else:
        cloud_predict.record_experiment_unavailable(archive, RUN_ID, state, reason=failure)

    manifest = verify_forecasts(archive)
    finished = manifest["runs"][RUN_ID]
    assert "experimental_release" not in finished
    assert _official_bytes(archive, finished) == frozen
    for day, entry in finished["forecasts"].items():
        assert entry["publication_evidence"] == primary["forecasts"][day]["publication_evidence"]
        document = json.loads((archive / entry["experimental_path"]).read_bytes())
        main = json.loads((archive / entry["path"] / "forecast.json").read_bytes())
        expected = {row["match_id"] for row in main["predictions"]}
        expected_technical = {(key, name) for key in expected for name in ("dc-sot-v1", "dc-xg-v1")}
        expected_rating = {
            (row["match_id"], RATING_NAME)
            for row in main["predictions"]
            if row["status"] == "predicted" and row["model_name"] == "dixon-coles"
        }
        frozen_attempts = [(row["match_id"], row["model_name"]) for row in document["predictions"]]
        assert document["generated_utc"] == EXPERIMENT_AT.isoformat()
        assert len(frozen_attempts) == len(expected_technical) + len(expected_rating)
        assert set(frozen_attempts) == expected_technical | expected_rating
        assert all(
            row["status"] == "missing" and row["model_probabilities"] is None
            for row in document["predictions"]
        )
        assert {
            row["fallback_reason"]
            for row in document["predictions"]
            if row["match_id"] in {"jc:t17-today", "jc:t17-tomorrow"}
        } == {want_reason}
        parlay = json.loads((archive / entry["parlay_path"]).read_bytes())
        assert parlay["bet_allowed"] is False

    confirm_fixture(archive, published=EXPERIMENT_AT.isoformat(), only_unpublished=True)
    summary = settle(archive, as_of=EXPERIMENT_AT)
    english_sot = next(
        g
        for g in summary["experiments"]["groups"]
        if g["league"] == "英超" and g["model_name"] == "dc-sot-v1"
    )
    assert english_sot["today"]["predicted"] == 0
    assert english_sot["today"]["reasons"] == {want_reason: 1}
    global_rating = next(
        g for g in summary["experiments"]["groups"] if g["model_name"] == RATING_NAME
    )
    assert global_rating["today"]["predicted"] == 0
    assert global_rating["today"]["reasons"] == {want_reason: 1}
    report = generate(archive, tmp_path / "digest", now=EXPERIMENT_AT).read_text()
    assert "## 实验模型" in report and "jc:t17-today" in report
    experimental_section = report.split("## 实验模型\n", 1)[1].split("\n## ", 1)[0]
    assert want_text in experimental_section
    assert _official_bytes(archive, finished) == frozen


@pytest.mark.parametrize(
    ("changed", "error_text"),
    [("database", "训练库"), ("parameters", "参数")],
)
def test_experiments_reject_changed_production_inputs(
    scheduled_archive, tmp_path, monkeypatch, changed, error_text
):
    archive, params = scheduled_archive
    state = tmp_path / "production-state"
    primary = _freeze_production(archive, params, state, monkeypatch)
    frozen = _official_bytes(archive, primary)
    manifest_bytes = (archive / "forecast_manifest.json").read_bytes()
    changed_file = state / "zucai.db" if changed == "database" else params
    changed_file.write_bytes(changed_file.read_bytes() + b"\n")

    with pytest.raises(ValueError, match=error_text):
        cloud_predict.run_experiments(archive, params, RUN_ID, state)

    assert not list(archive.rglob("experimental.json"))
    assert (archive / "forecast_manifest.json").read_bytes() == manifest_bytes
    assert _official_bytes(archive, primary) == frozen
