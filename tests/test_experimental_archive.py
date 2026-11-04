"""End-to-end freeze and Release isolation with only checked-in feed fixtures."""
# ruff: noqa: F811

import hashlib
import json
import zipfile

import pytest

from test_cloud_predict import NOW, scheduled_archive  # noqa: F401
from test_stats_rows import LocalRelease
from zucai import cloud_predict, experimental_archive
from zucai.forecast_archive import verify_forecasts


@pytest.mark.parametrize("failure", [None, "fit", "upload", "restore"])
def test_production_freezes_even_when_experiment_fails(
    scheduled_archive, tmp_path, monkeypatch, failure
):
    archive, params = scheduled_archive
    release = LocalRelease(tmp_path / "release")
    original = experimental_archive.freeze_parallel
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    if failure == "fit":
        monkeypatch.setattr(experimental_archive, "compute", fail)
    elif failure == "upload":
        monkeypatch.setattr(release, "upload", fail)
    elif failure == "restore":
        monkeypatch.setattr(experimental_archive, "restore_rows", fail)
    monkeypatch.setattr(
        experimental_archive,
        "freeze_parallel",
        lambda *a, **k: original(*a, **k, release_factory=lambda: release),
    )
    result = cloud_predict.run(archive, params, "parallel-offline")
    manifest = verify_forecasts(archive)
    assert result["forecasts"]["2026-11-04"]["predicted"] == 1
    frozen = {}
    for day, entry in result["forecasts"].items():
        path = archive / entry["experimental_path"]
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest["files"][entry["experimental_path"]]
        doc = json.loads(raw)
        assert all(
            r["p2_ready"] is False and r["model_probabilities"] is None for r in doc["predictions"]
        )
        assert all(r["fallback_reason"] for r in doc["predictions"])
        if failure:
            assert all(
                r["fallback_reason"] == "already_started" or "failed" in r["fallback_reason"]
                for r in doc["predictions"]
            )
        frozen[day] = raw
    if failure is None:
        asset = result["experimental_release"]["asset"]
        assert asset["tag"] == "experiments-202611"
        with zipfile.ZipFile(release.root / asset["tag"] / asset["name"]) as bundle:
            assert "training.json" in bundle.namelist()
            for day, raw in frozen.items():
                assert bundle.read(day + "/experimental.json") == raw
    else:
        assert "experimental_release" not in result
    before = (archive / "forecast_manifest.json").read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        cloud_predict.run(archive, params, "parallel-offline")
    assert (archive / "forecast_manifest.json").read_bytes() == before
    any_path = archive / next(iter(result["forecasts"].values()))["experimental_path"]
    any_path.write_bytes(any_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        verify_forecasts(archive)
