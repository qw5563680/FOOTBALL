"""Candidate production operation uses the original review rules on an isolated restore."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from test_t58_jc_candidate_review import case as case
from zucai.cloud_feeds import MANIFEST, archive_info, digest, export_feeds, restore_feeds
from zucai.digest_aliases import pending_name_lines
from zucai.jc_candidate_review import QUEUE


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def review_archive(case, tmp_path):
    db, _, plan, _ = case
    archive = tmp_path / "archive"
    export_feeds(db, archive, date(2026, 11, 12), "old-run")
    for key in ("forecasts/old/forecast.json", "scoreboard/ledger.jsonl", "daily/old.md"):
        target = archive / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"immutable old bytes\r\n")
    return archive, plan


def test_review_changes_only_queue_manifest_and_new_audit_and_restores(review_archive, tmp_path):
    from zucai.cloud_candidate_review import apply_to_archive

    archive, plan = review_archive
    before = inventory(archive)
    old_meta = archive_info(archive)
    result = apply_to_archive(archive, plan, "t59-offline-1")
    assert (result["pending_before"], result["pending_after"]) == (2, 1)
    assert result["requests"] == 0
    after = inventory(archive)
    assert {key for key in before if before[key] != after[key]} == {QUEUE, MANIFEST}
    added = set(after) - set(before)
    prefix = "state/audit/jc_candidate_reviews/t59-offline-1/"
    assert added == {
        prefix + name + ".json" for name in ("plan", "review", "identity_inventory", "operation")
    }
    meta = archive_info(archive)
    assert added <= meta["files"].keys()
    for field in (
        "through_beijing",
        "sources",
        "counts",
        "pending_result_dates",
        "collection_status",
    ):
        assert meta[field] == old_meta[field]
    restored = tmp_path / "restored/football.db"
    restore_feeds(archive, restored)
    for key in {*added, QUEUE}:
        assert (restored.parent / key).read_bytes() == after[key]
    assert "2 个" in pending_name_lines(archive, meta, "2026-11-12T00:00:00Z")[0]
    assert "1 个" in pending_name_lines(archive, meta, datetime.now(UTC))[0]
    again = tmp_path / "export-again"
    export_feeds(restored, again, date(2026, 11, 12), "roundtrip")
    assert all((again / key).read_bytes() == after[key] for key in {*added, QUEUE})


@pytest.mark.parametrize(
    "failure", ["queue_hash", "inventory_hash", "new_candidate", "bad_archive"]
)
def test_stale_or_tampered_operation_does_not_change_archive(review_archive, failure):
    from zucai.cloud_candidate_review import apply_to_archive

    archive, plan = review_archive
    if failure.endswith("hash"):
        value = json.loads(plan.read_bytes())
        value["queue_sha256" if failure == "queue_hash" else "inventory_sha256"] = "0" * 64
        plan.write_text(json.dumps(value))
    elif failure == "new_candidate":
        value = json.loads((archive / QUEUE).read_bytes())
        value["测试联赛"]["新候选"] = dict(source="jc", team_id=99, status="pending", evidence=[])
        (archive / QUEUE).write_text(json.dumps(value))
        meta = json.loads((archive / MANIFEST).read_bytes())
        meta["files"][QUEUE] = digest(archive / QUEUE)
        (archive / MANIFEST).write_text(json.dumps(meta))
    else:
        (archive / QUEUE).write_bytes(b"tampered")
    before = inventory(archive)
    with pytest.raises(ValueError):
        apply_to_archive(archive, plan, "must-fail")
    assert inventory(archive) == before


def test_repeating_consumed_plan_fails_without_rewriting_previous_review(review_archive):
    from zucai.cloud_candidate_review import apply_to_archive

    archive, plan = review_archive
    apply_to_archive(archive, plan, "first")
    before = inventory(archive)
    with pytest.raises(ValueError, match="变化"):
        apply_to_archive(archive, plan, "second")
    assert inventory(archive) == before


@pytest.mark.parametrize("run", ["../outside", "a/b", "x" * 101, "", "a\n", ".git"])
def test_invalid_run_directory_is_rejected_without_changes(review_archive, run):
    from zucai.cloud_candidate_review import apply_to_archive

    archive, plan = review_archive
    before = inventory(archive)
    with pytest.raises(ValueError):
        apply_to_archive(archive, plan, run)
    assert inventory(archive) == before


def test_partial_archive_write_is_rolled_back(review_archive, monkeypatch):
    from zucai import cloud_candidate_review as operation

    archive, plan = review_archive
    before = inventory(archive)
    original = operation._atomic_write
    failed = False

    def fail_manifest(path, raw):
        nonlocal failed
        if Path(path) == archive / MANIFEST and not failed:
            failed = True
            raise OSError("simulated manifest replacement failure")
        return original(path, raw)

    monkeypatch.setattr(operation, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="simulated"):
        operation.apply_to_archive(archive, plan, "write-failure")
    assert failed
    assert inventory(archive) == before


def test_existing_run_audit_cannot_be_overwritten(review_archive):
    from zucai.cloud_candidate_review import apply_to_archive

    archive, plan = review_archive
    prior = archive / "state/audit/jc_candidate_reviews/already-used"
    prior.mkdir(parents=True)
    (prior / "review.json").write_bytes(b"old evidence")
    before = inventory(archive)
    with pytest.raises(FileExistsError):
        apply_to_archive(archive, plan, "already-used")
    assert inventory(archive) == before


def test_archive_change_during_review_is_not_overwritten(review_archive, monkeypatch):
    from zucai import cloud_candidate_review as operation

    archive, plan = review_archive
    original = operation.review_candidates
    concurrent = None

    def change_after_review(*args):
        nonlocal concurrent
        result = original(*args)
        changed = json.loads((archive / MANIFEST).read_bytes())
        changed["run"] = "other-writer"
        (archive / MANIFEST).write_text(json.dumps(changed))
        concurrent = inventory(archive)
        return result

    monkeypatch.setattr(operation, "review_candidates", change_after_review)
    with pytest.raises(ValueError, match="归档已变化"):
        operation.apply_to_archive(archive, plan, "concurrent")
    assert inventory(archive) == concurrent


@pytest.mark.parametrize("changed", ["uncommitted", "modified", "symlink"])
def test_dispatch_plan_requires_exact_committed_plan_bytes(tmp_path, changed):
    from test_publish_script import git
    from zucai.cloud_candidate_review import dispatch_plan

    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Offline")
    git(tmp_path, "config", "user.email", "offline@example.invalid")
    name = "data/jc_candidate_reviews/2026-11-13-first.json"
    plan = tmp_path / name
    plan.parent.mkdir(parents=True)
    plan.write_bytes(b"{}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "plan")
    options = json.dumps({"plan": name})
    assert dispatch_plan(options, tmp_path) == plan
    if changed == "uncommitted":
        name = "data/jc_candidate_reviews/another.json"
        (tmp_path / name).write_bytes(b"{}\n")
    elif changed == "modified":
        plan.write_bytes(b'{"changed": true}\n')
    else:
        copy = tmp_path / "same-bytes.json"
        copy.write_bytes(plan.read_bytes())
        plan.unlink()
        plan.symlink_to(copy)
    with pytest.raises(ValueError):
        dispatch_plan(json.dumps({"plan": name}), tmp_path)


@pytest.mark.parametrize(
    "options",
    [
        "{}",
        "[]",
        '{"plan":"../../secret.json"}',
        '{"plan":"/tmp/file.json"}',
        '{"plan":"data/jc_candidate_reviews/x.json","unknown":true}',
        '{"plan":"data/jc_candidate_reviews/$(command).json"}',
    ],
)
def test_dispatch_plan_rejects_unreviewed_paths_or_options(options, tmp_path):
    from zucai.cloud_candidate_review import dispatch_plan

    with pytest.raises(ValueError):
        dispatch_plan(options, tmp_path)


def test_cli_does_not_write_diagnostics_inside_archive(review_archive, monkeypatch):
    import sys

    from zucai.cloud_candidate_review import main

    archive, plan = review_archive
    before = inventory(archive)
    monkeypatch.chdir(archive)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "candidate-review",
            "--archive",
            str(archive),
            "--plan",
            str(plan),
            "--run-id",
            "bad-workdir",
        ],
    )
    with pytest.raises(ValueError, match="工作目录"):
        main()
    assert inventory(archive) == before
