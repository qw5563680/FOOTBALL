"""Candidate-review evidence is portable, verified and bound to its snapshot."""

import hashlib
import json
import shutil
from datetime import date

import pytest

from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.db import get_engine, init_db

AUDIT = "state/audit/jc_candidate_reviews"
REVIEW_FILES = ("plan", "identity_inventory", "review", "operation")
QUEUE = "state/alias_candidates/jc.json"


def write_bytes(root, key, raw):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def seed_archive(tmp_path, run_id="review-20261113_1"):
    database = tmp_path / "data/zucai.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    evidence = {
        f"{AUDIT}/{run_id}/{name}.json": (f'{{"evidence": "{name}"}}\n').encode()
        for name in REVIEW_FILES
    }
    for key, raw in evidence.items():
        write_bytes(database.parent, key, raw)
    write_bytes(database.parent, QUEUE, b'{"snapshot":"original"}\n')
    archive = tmp_path / "archive"
    manifest = export_feeds(database, archive, date(2026, 11, 13), "export")
    return database, archive, evidence, manifest


@pytest.mark.parametrize("run_id", ["0", "review-20261113_1", "a" * 100])
def test_review_evidence_roundtrips_with_exact_bytes_and_hashes(tmp_path, run_id):
    database, archive, evidence, manifest = seed_archive(tmp_path, run_id)
    for key, raw in evidence.items():
        assert manifest["files"].get(key) == hashlib.sha256(raw).hexdigest()
    assert archive_info(archive) == manifest
    restored = tmp_path / "restored/zucai.db"
    restore_feeds(archive, restored)
    for key, raw in evidence.items():
        assert (restored.parent / key).read_bytes() == raw
    second = tmp_path / "second-archive"
    exported = export_feeds(restored, second, date(2026, 11, 13), "second")
    for key, raw in evidence.items():
        assert exported["files"][key] == hashlib.sha256(raw).hexdigest()
        assert (second / key).read_bytes() == (database.parent / key).read_bytes()


def test_restoring_older_snapshot_removes_later_review_evidence(tmp_path):
    database, older, evidence, _ = seed_archive(tmp_path)
    later_key = f"{AUDIT}/later-2/review.json"
    write_bytes(database.parent, later_key, b'{"reviewed_utc":"later"}\n')
    write_bytes(database.parent, QUEUE, b'{"snapshot":"later"}\n')
    newer = tmp_path / "newer"
    export_feeds(database, newer, date(2026, 11, 13), "later")
    restored = tmp_path / "restored/zucai.db"
    restore_feeds(newer, restored)
    assert (restored.parent / later_key).is_file()
    unrelated = write_bytes(restored.parent, f"{AUDIT}/later-2/notes.json", b"keep")
    restore_feeds(older, restored)
    assert not (restored.parent / later_key).exists()
    assert unrelated.read_bytes() == b"keep"
    assert (restored.parent / QUEUE).read_bytes() == b'{"snapshot":"original"}\n'
    for key, raw in evidence.items():
        assert (restored.parent / key).read_bytes() == raw
    exported = export_feeds(restored, newer, date(2026, 11, 13), "restored-old")
    assert later_key not in exported["files"]
    assert not (newer / later_key).exists()


def test_restoring_pre_review_snapshot_removes_all_known_review_evidence(tmp_path):
    database, newer, evidence, _ = seed_archive(tmp_path)
    restored = tmp_path / "restored/zucai.db"
    restore_feeds(newer, restored)
    assert all((restored.parent / key).is_file() for key in evidence)
    for key in evidence:
        (database.parent / key).unlink()
    older = tmp_path / "older"
    export_feeds(database, older, date(2026, 11, 12), "older")
    restore_feeds(older, restored)
    assert not any((restored.parent / key).exists() for key in evidence)


@pytest.mark.parametrize("filename", REVIEW_FILES)
def test_tampered_review_is_rejected_before_database_or_queue_changes(tmp_path, filename):
    _, archive, _, manifest = seed_archive(tmp_path)
    key = f"{AUDIT}/review-20261113_1/{filename}.json"
    assert key in manifest["files"]
    (archive / key).write_bytes(b'{"tampered":true}\n')
    destination = write_bytes(tmp_path, "destination/zucai.db", b"keep database")
    queue = write_bytes(destination.parent, QUEUE, b"keep queue")
    audit = write_bytes(destination.parent, key, b"keep audit")
    with pytest.raises(ValueError, match="校验失败"):
        restore_feeds(archive, destination)
    assert destination.read_bytes() == b"keep database"
    assert queue.read_bytes() == b"keep queue"
    assert audit.read_bytes() == b"keep audit"


@pytest.mark.parametrize(
    "suffix",
    [
        "../review.json",
        "-run/review.json",
        "_run/review.json",
        ".run/review.json",
        "a" * 101 + "/review.json",
        "run/subdir/review.json",
        "run/unknown.json",
    ],
)
def test_manifest_rejects_unrecognized_review_paths(tmp_path, suffix):
    _, archive, _, manifest = seed_archive(tmp_path)
    manifest["files"][f"{AUDIT}/{suffix}"] = "0" * 64
    (archive / "feed_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="未知归档文件"):
        archive_info(archive)


@pytest.mark.parametrize("link_kind", ["file", "run", "directory"])
@pytest.mark.parametrize("stage", ["export", "archive", "restore"])
def test_review_symlinks_are_rejected_before_restored_state_changes(tmp_path, link_kind, stage):
    database, archive, _, manifest = seed_archive(tmp_path)
    destination = write_bytes(tmp_path, "destination/zucai.db", b"keep database")
    queue = write_bytes(destination.parent, QUEUE, b"keep queue")
    key = f"{AUDIT}/review-20261113_1/review.json"
    if stage == "restore":
        root = destination.parent
        write_bytes(root, key, b"keep audit")
    else:
        root = database.parent if stage == "export" else archive
        if stage == "archive":
            assert key in manifest["files"]
    relative = {
        "file": key,
        "run": f"{AUDIT}/review-20261113_1",
        "directory": AUDIT,
    }[link_kind]
    linked = root / relative
    target = root / "unrelated-link-target"
    shutil.move(linked, target)
    linked.symlink_to(target, target_is_directory=target.is_dir())
    if stage == "export":
        with pytest.raises(ValueError, match="符号链接"):
            export_feeds(database, archive, date(2026, 11, 13), "bad")
        assert json.loads((archive / "feed_manifest.json").read_bytes()) == manifest
    else:
        with pytest.raises(ValueError, match="符号链接|校验失败"):
            restore_feeds(archive, destination)
    assert destination.read_bytes() == b"keep database"
    assert queue.read_bytes() == b"keep queue"


def test_unlisted_archive_review_is_never_imported(tmp_path):
    _, archive, _, _ = seed_archive(tmp_path)
    key = f"{AUDIT}/unlisted/review.json"
    write_bytes(archive, key, b'{"unlisted":true}\n')
    destination = tmp_path / "restored/zucai.db"
    restore_feeds(archive, destination)
    assert not (destination.parent / key).exists()
