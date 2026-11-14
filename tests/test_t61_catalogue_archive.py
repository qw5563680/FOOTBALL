"""Catalogue evidence belongs to one hashed feed snapshot, including JC page originals."""

import hashlib
import json
from datetime import date

import pytest

from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.db import get_engine, init_db

CATALOGUE_FILES = (
    "state/catalogue/latest.json",
    "state/catalogue/run-1/apif-raw.json",
    "state/catalogue/run-1/apif-meta.json",
    "state/catalogue/run-1/apif.json",
    "state/catalogue/run-1/fdorg-raw.json",
    "state/catalogue/run-1/fdorg-meta.json",
    "state/catalogue/run-1/fdorg.json",
    "state/catalogue/run-1/fd.json",
    "state/catalogue/run-1/summary.json",
    "state/catalogue/run-1/jc.json",
    "state/catalogue/run-1/jc/2026-10/2026-10-31-p1.json",
    "state/catalogue/run-1/jc/2026-10/2026-10-31-p1.json.meta.json",
)


def database_at(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(path)
    init_db(engine)
    engine.dispose()
    return path


def put(root, key, raw=b'{"offline":"evidence"}\n'):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def test_catalogue_files_are_hashed_exported_and_restored_without_changing_feed_rows(tmp_path):
    source = database_at(tmp_path / "work/data/zucai.db")
    archive = tmp_path / "archive"
    baseline = export_feeds(source, archive, date(2026, 11, 13), "before")
    rows_before = {key: (archive / key).read_bytes() for key in baseline["files"]}
    for key in CATALOGUE_FILES:
        put(source.parent, key)
    meta = export_feeds(source, archive, date(2026, 11, 13), "catalogue")
    assert set(CATALOGUE_FILES) <= set(meta["files"])
    assert meta["sources"] == baseline["sources"]
    assert all((archive / key).read_bytes() == raw for key, raw in rows_before.items())
    target = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, target)
    for key in CATALOGUE_FILES:
        assert (target.parent / key).read_bytes() == (source.parent / key).read_bytes()
        assert meta["files"][key] == hashlib.sha256((source.parent / key).read_bytes()).hexdigest()
    put(archive, CATALOGUE_FILES[1], b"tampered")
    with pytest.raises(ValueError, match="校验"):
        archive_info(archive)


def test_restore_older_snapshot_removes_all_later_catalogue_evidence(tmp_path):
    source = database_at(tmp_path / "seed/data/zucai.db")
    older, newer = tmp_path / "older", tmp_path / "newer"
    export_feeds(source, older, date(2026, 11, 12), "older")
    for key in CATALOGUE_FILES:
        put(source.parent, key)
    export_feeds(source, newer, date(2026, 11, 13), "newer")
    target = tmp_path / "restored/data/zucai.db"
    restore_feeds(newer, target)
    assert (target.parent / CATALOGUE_FILES[0]).is_file()
    restore_feeds(older, target)
    assert not any(path.is_file() for path in (target.parent / "state/catalogue").rglob("*"))
    again = export_feeds(target, tmp_path / "roundtrip", date(2026, 11, 12), "again")
    assert not any(key.startswith("state/catalogue/") for key in again["files"])


@pytest.mark.parametrize("where", ["source", "archive", "destination"])
@pytest.mark.parametrize("link", ["state/catalogue", "state/catalogue/run-1", CATALOGUE_FILES[1]])
def test_catalogue_symlinks_are_rejected_before_database_or_archive_changes(tmp_path, where, link):
    source = database_at(tmp_path / "work/data/zucai.db")
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 13), "seed")
    destination = database_at(tmp_path / "destination/data/zucai.db")
    before_db = destination.read_bytes()
    before_manifest = (archive / "feed_manifest.json").read_bytes()
    root = {"source": source.parent, "archive": archive, "destination": destination.parent}[where]
    outside = tmp_path / "outside"
    if link.endswith(".json"):
        outside.write_bytes(b"external")
    else:
        outside.mkdir()
    path = root / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(ValueError, match="符号链接"):
        if where == "source":
            export_feeds(source, archive, date(2026, 11, 13), "new")
        else:
            restore_feeds(archive, destination)
    assert destination.read_bytes() == before_db
    assert (archive / "feed_manifest.json").read_bytes() == before_manifest


@pytest.mark.parametrize(
    "bad",
    [
        "state/catalogue/../unexpected.json",
        "state/catalogue/run-1/unknown.json",
        "state/catalogue/run-1/jc/2026-13/2026-13-01-p1.json",
        "state/catalogue/run-1/jc/2026-10/2026-10-31-p0.json",
        "state/catalogue/run-1/jc/2026-10/../outside.json",
    ],
)
def test_catalogue_archive_rejects_unknown_paths(tmp_path, bad):
    source = database_at(tmp_path / "work/data/zucai.db")
    archive = tmp_path / "archive"
    meta = export_feeds(source, archive, date(2026, 11, 13), "seed")
    path = put(archive, bad)
    meta["files"][bad] = hashlib.sha256(path.read_bytes()).hexdigest()
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="未知归档"):
        archive_info(archive)


@pytest.mark.parametrize(
    "key",
    [
        "state/catalogue/backfill-checkpoint.json",
        "state/catalogue/history-1/backfill.json",
        "state/catalogue/history-1/backfill/api-football/2026-11-14/fixtures-39-2025_012345123456Z_0123456789abcdef0123456789abcdef.json",
        "state/catalogue/history-1/backfill/api-football/2026-11-14/fixtures-39-2025_012345123456Z_0123456789abcdef0123456789abcdef.json.meta.json",
        "state/catalogue/history-1/backfill/football-data-org/2026-11-14/matches_012345123456Z_0123456789abcdef0123456789abcdef.json",
        "state/catalogue/history-1/backfill/football-data-co-uk/2026-11-14/E0-2526_012345123456Z_0123456789abcdef0123456789abcdef.csv",
        "state/catalogue/history-1/backfill/football-data-co-uk/2026-11-14/E0-2526_012345123456Z_0123456789abcdef0123456789abcdef.csv.meta.json",
    ],
)
def test_catalogue_backfill_originals_and_checkpoint_roundtrip(tmp_path, key):
    source = database_at(tmp_path / "seed/data/zucai.db")
    evidence = put(source.parent, key)
    archive = tmp_path / "archive"
    report = export_feeds(source, archive, date(2026, 11, 14), "history")
    assert key in report["files"]
    target = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, target)
    assert (target.parent / key).read_bytes() == evidence.read_bytes()


def test_restoring_older_snapshot_removes_unmanifested_future_runtime_catalogue_files(tmp_path):
    source = database_at(tmp_path / "seed/data/zucai.db")
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 14), "old")
    target = database_at(tmp_path / "target/data/zucai.db")
    later = put(target.parent, "state/catalogue/later/backfill/apif/cup-aliases-later.json")
    restore_feeds(archive, target)
    assert not later.exists()
