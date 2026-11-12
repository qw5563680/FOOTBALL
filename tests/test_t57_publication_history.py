"""Publication history failures and saved proof, using real offline Git repositories."""

import json
import shutil
from datetime import date, datetime

import pytest

from sfc_publication_support import REF, git, prepare_remote
from test_forecast_publication import _legacy
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import NOW
from zucai.daily_digest import generate
from zucai.forecast_publication import publication_times
from zucai.scoreboard.command import run

HISTORY = "state/publication/forecast_history.json"


@pytest.fixture(autouse=True)
def fixed_scoreboard_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 13, tzinfo=tz)

    monkeypatch.setattr("zucai.scoreboard.command.datetime", Clock)


def files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


def break_git(archive, failure):
    if failure == "非仓库":
        shutil.rmtree(archive / ".git")
    elif failure == "浅克隆":
        (archive / ".git/shallow").write_text(git(archive, "rev-parse", "HEAD") + "\n")
    elif failure == "published_ref":
        git(archive, "update-ref", "-d", REF)
    else:
        git(archive, "config", "--unset", "remote.origin.url")


def command(archive, name):
    if name == "scoreboard":
        return run(archive, as_of=NOW)
    return generate(archive, archive / "daily", now=NOW, run_id="t57")


@pytest.mark.parametrize("failure", ["非仓库", "浅克隆", "published_ref", "远端配置"])
@pytest.mark.parametrize("name", ["scoreboard", "digest"])
def test_missing_history_fails_without_writing(scoreboard_archive, failure, name):
    archive = scoreboard_archive
    _legacy(archive)
    prepare_remote(archive, "2026-11-01T16:00:00Z")
    break_git(archive, failure)
    before = files(archive)
    with pytest.raises(ValueError, match=failure):
        command(archive, name)
    assert files(archive) == before


@pytest.mark.parametrize("failure", ["非仓库", "浅克隆", "published_ref"])
@pytest.mark.parametrize("name", ["scoreboard", "digest"])
def test_saved_history_survives_git_failure_and_tampering_fails(scoreboard_archive, failure, name):
    archive = scoreboard_archive
    manifest = _legacy(archive)
    # All fixture batches were generated before this commit.
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, name)
    before = files(archive)
    assert HISTORY in before
    break_git(archive, failure)
    command(archive, name)
    assert files(archive) == before
    history = json.loads((archive / HISTORY).read_bytes())
    key = next(iter(history["entries"]))
    history["entries"][key]["candidate_sha256"] = "0" * 64
    (archive / HISTORY).write_text(json.dumps(history))
    changed = files(archive)
    with pytest.raises(ValueError, match=failure):
        command(archive, name)
    assert files(archive) == changed
    assert manifest == json.loads((archive / "forecast_manifest.json").read_bytes())


def test_receipts_need_no_git_and_do_not_generate_history(scoreboard_archive):
    for name in ("scoreboard", "digest"):
        command(scoreboard_archive, name)
    assert not (scoreboard_archive / HISTORY).exists()


def test_export_restore_includes_history_and_removes_stale_history(scoreboard_archive, tmp_path):
    from zucai.cloud_feeds import export_feeds, restore_feeds

    archive = scoreboard_archive
    manifest = _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, "scoreboard")
    raw = (archive / HISTORY).read_bytes()
    db = tmp_path / "working/feeds.db"
    restore_feeds(archive, db)
    exported = export_feeds(db, archive, date(2026, 11, 2), "t57")
    assert HISTORY in exported["files"]
    restore_feeds(archive, db)
    assert (db.parent / HISTORY).read_bytes() == raw
    # Forecast files stay in archive; restored state alone supplies the proof.
    shutil.copytree(archive / "forecasts", db.parent / "forecasts")
    assert publication_times(db.parent, manifest)
    (archive / HISTORY).write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="校验"):
        restore_feeds(archive, db)


def test_invalid_saved_slot_is_unknown_with_healthy_git_and_never_rewritten(scoreboard_archive):
    archive = scoreboard_archive
    manifest = _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, "scoreboard")
    saved = json.loads((archive / HISTORY).read_bytes())
    path = next(iter(saved["entries"]))
    saved["entries"][path]["candidate_sha256"] = "0" * 64
    raw = json.dumps(saved).encode()
    (archive / HISTORY).write_bytes(raw)
    assert path not in publication_times(archive, manifest)
    command(archive, "scoreboard")
    assert (archive / HISTORY).read_bytes() == raw


@pytest.mark.parametrize("name", ["scoreboard", "digest"])
def test_state_write_failure_rolls_back_reports(scoreboard_archive, monkeypatch, name):
    archive = scoreboard_archive
    _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    before = files(archive)
    if name == "scoreboard":
        from pathlib import Path

        original = Path.replace

        def fail(self, target):
            if str(target).endswith(HISTORY):
                raise OSError("injected state write failure")
            return original(self, target)

        monkeypatch.setattr(Path, "replace", fail)
    else:
        import zucai.daily_digest as digest

        original = digest._atomic_write

        def fail(target, content):
            if str(target).endswith(HISTORY):
                raise OSError("injected state write failure")
            return original(target, content)

        monkeypatch.setattr(digest, "_atomic_write", fail)
    with pytest.raises(OSError, match="injected"):
        command(archive, name)
    assert files(archive) == before


def test_receipt_wins_over_conflicting_saved_history(scoreboard_archive):
    archive = scoreboard_archive
    manifest = json.loads((archive / "forecast_manifest.json").read_bytes())
    expected = publication_times(archive, manifest)
    _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, "scoreboard")
    (archive / "forecast_manifest.json").write_text(json.dumps(manifest))
    break_git(archive, "非仓库")
    assert publication_times(archive, manifest) == expected


def test_export_preserves_newer_archive_proof_and_restore_clears_absent_state(
    scoreboard_archive, tmp_path
):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.publication_history import encode_history

    archive = scoreboard_archive
    _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, "scoreboard")
    raw = (archive / HISTORY).read_bytes()
    db = tmp_path / "working/feeds.db"
    restore_feeds(archive, db)
    assert not (db.parent / HISTORY).exists()  # Not enrolled until regular export.
    (db.parent / HISTORY).parent.mkdir(parents=True)
    saved = json.loads(raw)["entries"]
    key = next(iter(saved))
    (db.parent / HISTORY).write_bytes(encode_history({key: dict(saved[key], commit="0" * 40)}))
    export_feeds(db, archive, date(2026, 11, 2), "t57")
    assert (archive / HISTORY).read_bytes() == raw
    restore_feeds(archive, db)
    assert (db.parent / HISTORY).read_bytes() == raw
    meta = json.loads((archive / "feed_manifest.json").read_bytes())
    meta["files"].pop(HISTORY)
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    (archive / HISTORY).unlink()
    restore_feeds(archive, db)
    assert not (db.parent / HISTORY).exists()


@pytest.mark.parametrize("name", ["scoreboard", "digest"])
def test_enrolled_history_append_uses_final_manifest_hash_and_is_idempotent(
    scoreboard_archive, name
):
    import hashlib

    from zucai.publication_history import encode_history

    archive = scoreboard_archive
    _legacy(archive)
    prepare_remote(archive, "2026-11-02T18:00:00Z")
    command(archive, name)
    entries = json.loads((archive / HISTORY).read_bytes())["entries"]
    entries.pop(next(iter(entries)))  # Simulate an older enrolled proof snapshot.
    (archive / HISTORY).write_bytes(encode_history(entries))
    meta = json.loads((archive / "feed_manifest.json").read_bytes())
    meta["files"][HISTORY] = hashlib.sha256((archive / HISTORY).read_bytes()).hexdigest()
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    command(archive, name)
    sha = hashlib.sha256((archive / "feed_manifest.json").read_bytes()).hexdigest()
    if name == "scoreboard":
        result = json.loads((archive / "scoreboard/summary.json").read_bytes())
        assert result["input_manifests"]["feed_manifest.json"] == sha
    else:
        result = max(
            (json.loads(p.read_bytes()) for p in (archive / "daily").rglob("*.json")),
            key=lambda r: r["update_number"],
        )
        assert result["inputs"]["feed_manifest.json"] == sha
    before = files(archive)
    command(archive, name)
    assert files(archive) == before
