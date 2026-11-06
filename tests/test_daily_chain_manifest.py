"""Run provenance adds metadata without altering archived rows or legacy readers."""

import json
from datetime import date

import pytest

from test_cloud_feeds import seed_cloud_archive
from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds


@pytest.mark.parametrize(
    "event,operation", [("schedule", ""), ("workflow_dispatch", "daily"), ("", "")]
)
def test_feed_manifest_retains_dispatch_origin_through_restore(
    tmp_path, monkeypatch, event, operation
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("ZUCAI_OPERATION", operation)
    archive = seed_cloud_archive(tmp_path)
    meta = archive_info(archive)
    assert meta["run"] == "seed"
    assert meta["event_name"] == (event or "local")
    assert meta["operation"] == (operation or None)
    target = tmp_path / "restored/data/zucai.db"
    restored = restore_feeds(archive, target)
    assert restored["event_name"] == meta["event_name"]
    assert restored["operation"] == meta["operation"]
    new = export_feeds(target, tmp_path / "second", date(2026, 11, 1), "second")
    assert new["counts"] == meta["counts"]
    assert new["sources"] == meta["sources"]
    assert new["files"] == meta["files"]


def test_legacy_manifest_without_provenance_still_restores(tmp_path):
    archive = seed_cloud_archive(tmp_path)
    path = archive / "feed_manifest.json"
    meta = json.loads(path.read_bytes())
    for key in ("event_name", "operation"):
        meta.pop(key, None)
    path.write_text(json.dumps(meta))
    assert restore_feeds(archive, tmp_path / "restored/data/zucai.db")["run"] == "seed"
