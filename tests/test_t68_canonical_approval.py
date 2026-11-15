"""Creation requires reviewed numeric originals, never a bulk approval of proposals."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import pin_categories, seal, table
from zucai.db import get_engine, init_db


@pytest.fixture
def creation(frozen_archive, tmp_path, monkeypatch):  # noqa: F811
    pin_categories(tmp_path, monkeypatch, approved={"48"})
    table(frozen_archive, "matches", [])
    table(frozen_archive, "results", [])
    table(frozen_archive, "teams", [])
    db = tmp_path / "copy/data/feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    shutil.copytree(frozen_archive / "state", db.parent / "state")
    return frozen_archive, db


def approved(archive):
    from zucai.identity_canonical import propose

    proposal = propose(archive, codes=["48"])
    item = proposal["decisions"][0]
    item.update(
        status="approved",
        approved_by="qw5563680",
        approved_utc=datetime.now(UTC).isoformat(),
        approval_pr="https://github.com/qw5563680/FOOTBALL/pull/143",
        review_url="https://github.com/qw5563680/FOOTBALL/pull/143#pullrequestreview-1",
    )
    proposal["decisions"] = [item]
    path = archive.parent / "approvals.json"
    path.write_text(json.dumps(proposal))
    return path, item


def test_creation_repeat_and_exported_reader_recognizes_identity(creation):
    from zucai.identity_binding_archive import load_archive
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    path, item = approved(archive)
    before = sqlite3.connect(db).execute("SELECT * FROM matches").fetchall()
    first = apply_approvals(db, archive, path)
    assert first["created"] == 1
    assert apply_approvals(db, archive, path)["created"] == 0
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        teams = [dict(r) for r in con.execute("SELECT * FROM teams")]
        assert con.execute("SELECT * FROM matches").fetchall() == before
    # Same archive-reader entry used after export/restore, including the authenticated audit.
    table(archive, "teams", teams)
    audit = db.parent / "state/audit/jc_canonical_identities.csv"
    dest = archive / "state/audit/jc_canonical_identities.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(audit.read_bytes())
    seal(archive)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["created_utc"] = datetime.now(UTC).isoformat()
    (archive / "feed_manifest.json").write_text(json.dumps(manifest))
    result = load_archive(archive, leagues=["48"])["teams"]["jc-team:" + item["official_team_id"]]
    assert result["identity_status"] == "existing_numbered"
    assert result["project_team_id"] == str(teams[0]["id"])


@pytest.mark.parametrize(
    "attack",
    [
        "proposed",
        "wrong_owner",
        "missing_review",
        "future",
        "wrong_evidence",
        "wrong_category",
        "stale_db",
        "alias_collision",
    ],
)
def test_creation_rejects_invalid_approval_without_writes(creation, attack):
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    path, item = approved(archive)
    doc = json.loads(path.read_bytes())
    row = doc["decisions"][0]
    if attack == "proposed":
        row["status"] = "proposed"
    if attack == "wrong_owner":
        row["approved_by"] = "someone"
    if attack == "missing_review":
        row.pop("review_url")
    if attack == "future":
        row["approved_utc"] = "2099-01-01T00:00:00Z"
    if attack == "wrong_evidence":
        row["evidence_sha256"] = "0" * 64
    if attack == "wrong_category":
        row["category"] = "national/women/U23/olympic"
    if attack in {"stale_db", "alias_collision"}:
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO teams VALUES(1,?,?)",
                (
                    "既有球队",
                    json.dumps([item["canonical"]] if attack == "alias_collision" else []),
                ),
            )
    path.write_text(json.dumps(doc))
    before = db.read_bytes()
    with pytest.raises(ValueError):
        apply_approvals(db, archive, path)
    assert db.read_bytes() == before
    assert not (db.parent / "state/audit/jc_canonical_identities.csv").exists()


def test_creation_receipt_allows_additional_current_observations(creation):
    """An append-only catalogue growing must not invalidate the original decision."""
    from copy import deepcopy

    from zucai.identity_binding_archive import _tables, load_archive
    from zucai.identity_canonical import apply_approvals, apply_created_identities

    archive, db = creation
    path, item = approved(archive)
    apply_approvals(db, archive, path)
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        table(archive, "teams", [dict(r) for r in con.execute("SELECT * FROM teams")])
    receipt = archive / "state/audit/jc_canonical_identities.csv"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes((db.parent / "state/audit/jc_canonical_identities.csv").read_bytes())
    seal(archive)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["created_utc"] = datetime.now(UTC).isoformat()
    (archive / "feed_manifest.json").write_text(json.dumps(manifest))
    facts = load_archive(archive, leagues=["48"])
    team = facts["teams"]["jc-team:" + item["official_team_id"]]
    added = deepcopy(team["official_name_evidence"][0])
    added["match_id"] = "jc:later-authenticated-observation"
    team["official_name_evidence"].append(added)
    # Contract: the archive reader has already authenticated the larger current inventory.
    tables, ref = _tables(archive)
    apply_created_identities(archive, {f"jc-team:{item['official_team_id']}": team}, tables, ref)
    assert team["project_team_id"] is not None


def test_creation_audit_survives_actual_export_restore(creation):
    from datetime import date

    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    path, item = approved(archive)
    apply_approvals(db, archive, path)
    exported = archive.parent / "export"
    manifest = export_feeds(db, exported, date(2026, 11, 15), "t68-creation-test")
    restored = archive.parent / "restored/data/feeds.db"
    restore_feeds(exported, restored)
    key = "state/audit/jc_canonical_identities.csv"
    assert key in manifest["files"]
    for row in item["evidence"]:
        for original in row["originals"]:
            assert manifest["files"][original["path"]] == original["sha256"]
            assert (restored.parent / original["path"]).is_file()
            assert (restored.parent / (original["path"] + ".meta.json")).is_file()
    assert (restored.parent / key).read_bytes() == (db.parent / key).read_bytes()
    with sqlite3.connect(db) as a, sqlite3.connect(restored) as b:
        assert (
            a.execute("SELECT * FROM teams").fetchall()
            == b.execute("SELECT * FROM teams").fetchall()
        )


@pytest.mark.parametrize("collision", ["alias", "name", "target_other_id"])
def test_receipt_rejects_duplicate_numeric_marker_owner(creation, collision):
    from zucai.identity_binding_archive import load_archive
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    path, item = approved(archive)
    apply_approvals(db, archive, path)
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        teams = [dict(row) for row in con.execute("SELECT * FROM teams")]
    marker = "jc:team:" + item["official_team_id"]
    if collision == "target_other_id":
        teams[0]["aliases"] = json.loads(teams[0]["aliases"]) + ["jc:team:999999"]
    else:
        teams.append(
            dict(
                id=99,
                name=marker if collision == "name" else "Conflicting owner",
                aliases=[marker.upper()] if collision == "alias" else [],
            )
        )
    table(archive, "teams", teams)
    receipt = archive / "state/audit/jc_canonical_identities.csv"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes((db.parent / "state/audit/jc_canonical_identities.csv").read_bytes())
    seal(archive)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["created_utc"] = datetime.now(UTC).isoformat()
    (archive / "feed_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_archive(archive, leagues=["48"])


def test_collision_in_both_frozen_and_live_inventory_reaches_collision_guard(creation):
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    _, item = approved(archive)
    existing = dict(id=99, name="Existing exact alias owner", aliases=[item["canonical"]])
    table(archive, "teams", [existing])
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO teams VALUES(?,?,?)",
            (existing["id"], existing["name"], json.dumps(existing["aliases"])),
        )
    path, _ = approved(archive)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="already has an owner"):
        apply_approvals(db, archive, path)
    assert db.read_bytes() == before


def test_creation_rejects_missing_portable_original(creation):
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    path, item = approved(archive)
    original = item["evidence"][0]["originals"][0]["path"]
    (db.parent / original).unlink()
    before = db.read_bytes()
    with pytest.raises(ValueError, match="originals must be restored"):
        apply_approvals(db, archive, path)
    assert db.read_bytes() == before
    assert not (db.parent / "state/audit/jc_canonical_identities.csv").exists()


def _cloud_env(monkeypatch, work):
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))


def _strip_unknown_catalogue_files(root):
    """Fixture archives carry nested catalogue shapes that the strict manifest
    reader rejects; drop only those keys so the online export re-reads cleanly."""
    manifest_path = root / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = {
        key: sha
        for key, sha in manifest["files"].items()
        if not key.startswith("state/catalogue/backfill/")
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _ensure_closing_odds(root):
    path = root / "state/closing_odds.jsonl"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def test_cloud_canonical_apply_empty_approvals_changes_nothing(creation, tmp_path, monkeypatch):
    """Online entry with the checked-in empty approvals list is a no-op export."""
    import sys
    from datetime import date

    from test_apif_reconcile import frozen
    from zucai.cloud_feeds import export_feeds, main, restore_feeds

    archive, db = creation
    _ensure_closing_odds(db.parent)
    exported = tmp_path / "exported"
    seed = export_feeds(
        db,
        exported,
        date(2026, 11, 15),
        "seed",
        collection={"apif": {"status": "complete", "run_id": "seed"}},
    )
    _strip_unknown_catalogue_files(exported)
    empty = tmp_path / "empty-approvals.json"
    empty.write_text('{"version": 1, "archive_sha256": "", "codes": [], "decisions": []}')
    monkeypatch.setattr("zucai.cloud_feeds.CANONICAL_APPROVALS_PATH", empty)
    work = tmp_path / "work"
    work.mkdir()
    _cloud_env(monkeypatch, work)
    monkeypatch.setattr(
        sys,
        "argv",
        ["feeds", "canonical-apply", "--archive", str(exported), "--run-id", "c1"],
    )
    main()
    meta = json.loads((exported / "feed_manifest.json").read_bytes())
    record = meta["collection"]["canonical_apply"]
    assert record["status"] == "skipped" and record["created"] == 0
    assert set(record) == {
        "operation",
        "status",
        "run_id",
        "created",
        "already_applied",
        "decisions",
    }
    assert meta["collection"]["apif"] == {"status": "complete", "run_id": "seed"}
    from zucai.cloud_feeds import daily_collection

    assert "canonical_apply" not in daily_collection(meta["collection"])
    # Nothing changed: a restore equals the pre-run database byte for byte.
    restored = tmp_path / "restored/data/feeds.db"
    restore_feeds(exported, restored)
    assert frozen(restored) == frozen(db)
    assert meta["counts"] == seed["counts"]


def test_cloud_canonical_apply_export_then_restore_stays_consistent(
    creation, tmp_path, monkeypatch
):
    """Online entry: restore, apply approvals, export; restores stay consistent."""
    import sys

    from test_apif_reconcile import frozen
    from zucai.cloud_feeds import main, restore_feeds

    archive, db = creation
    _ensure_closing_odds(archive)
    _ensure_closing_odds(db.parent)
    seal(archive)
    _strip_unknown_catalogue_files(archive)
    path, _ = approved(archive)
    monkeypatch.setattr("zucai.cloud_feeds.CANONICAL_APPROVALS_PATH", path)
    work = tmp_path / "work"
    work.mkdir()
    _cloud_env(monkeypatch, work)
    monkeypatch.setattr(
        sys,
        "argv",
        ["feeds", "canonical-apply", "--archive", str(archive), "--run-id", "c2"],
    )
    main()
    merged = work / "data/zucai.db"
    meta = json.loads((archive / "feed_manifest.json").read_bytes())
    record = meta["collection"]["canonical_apply"]
    assert record["status"] == "complete" and record["created"] == 1
    assert len(json.dumps(record)) < 2000
    from zucai.cloud_feeds import daily_collection

    assert "canonical_apply" not in daily_collection(meta["collection"])
    for index in range(2):
        restored = tmp_path / f"restored-{index}/data/feeds.db"
        restore_feeds(archive, restored)
        assert frozen(restored) == frozen(merged)
    before = {
        p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    }
    restore_feeds(archive, tmp_path / "restored-again/data/feeds.db")
    after = {
        p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    }
    assert after == before
