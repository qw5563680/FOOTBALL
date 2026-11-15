"""Daily application/invalidation through the real sync and export/restore entry."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest
import responses

from test_cloud_feeds import mock_calendar, mock_result_day, run_cloud_sync
from test_t68_cross_day import API, FD, approval, current_ids
from test_t68_cross_day import db as db_fixture
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, session_scope
from zucai.models import Result

db = db_fixture


@pytest.mark.parametrize("action", ["same_match", "different_match"])
def test_normal_sync_applies_approved_decision_and_roundtrips(db, tmp_path, monkeypatch, action):
    module, _, _ = approval(db, tmp_path, monkeypatch, action=action)
    archive = tmp_path / "daily-archive"
    export_feeds(Path(db.url.database), archive, date(2026, 11, 14), "seed")
    with responses.RequestsMock() as http:
        mock_calendar(http)
        for day in ["2026-11-12", "2026-11-13", "2026-11-14"]:
            mock_result_day(http, day, [])
        run_cloud_sync(monkeypatch, tmp_path / "daily-work", archive, "2026-11-14")
    receipt_path = archive / module.RECEIPT_FILE
    assert receipt_path.exists()
    records = list(csv.DictReader((archive / "state/audit/apif_cross_day_decisions.csv").open()))
    assert records[0]["status"] == "active"
    target = tmp_path / "daily-restored/zucai.db"
    restore_feeds(archive, target)
    engine = get_engine(target)
    try:
        assert (API in current_ids(engine)) == (action == "different_match")
        assert FD in current_ids(engine)
    finally:
        engine.dispose()
    assert (target.parent / module.RECEIPT_FILE).read_bytes() == receipt_path.read_bytes()


@pytest.mark.parametrize("drift", ["snapshot", "removed"])
def test_stale_decision_is_audited_without_destroying_receipt(db, tmp_path, monkeypatch, drift):
    from zucai.source_results_audit import audit_source_results

    module, registry, document = approval(db, tmp_path, monkeypatch)
    module.apply_decisions(db.url.database, registry)
    receipt = Path(db.url.database).parent / module.RECEIPT_FILE
    before = receipt.read_bytes()
    if drift == "snapshot":
        with session_scope(db) as session:
            session.get(Result, FD).home_goals = 8
    else:
        document["decisions"] = []
        registry.write_text(json.dumps(document))
    module.apply_decisions(db.url.database, registry)
    audit = audit_source_results(db.url.database, tmp_path / "audit/apif_results_crosscheck.csv")
    assert audit["cross_day_decisions"]["inactive"] == 1
    assert receipt.read_bytes() == before
    assert API not in current_ids(db)
    rows = list(csv.DictReader((tmp_path / "audit/apif_cross_day_decisions.csv").open()))
    assert rows[0]["reason"] == (
        "snapshot_changed" if drift == "snapshot" else "registry_removed_or_changed"
    )


@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink_parent"])
def test_application_never_embeds_files_outside_registry_root(db, tmp_path, monkeypatch, escape):
    module, registry, document = approval(db, tmp_path, monkeypatch)
    evidence = tmp_path / "review-original.txt"
    item = document["decisions"][0]["evidence"][0]
    if escape == "absolute":
        item["path"] = str(evidence)
    elif escape == "traversal":
        item["path"] = "../" + tmp_path.name + "/review-original.txt"
    else:
        (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
        item["path"] = "linked/review-original.txt"
    registry.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        module.apply_decisions(db.url.database, registry)
    assert not (Path(db.url.database).parent / module.RECEIPT_FILE).exists()


def test_stale_entry_does_not_stop_an_unrelated_valid_decision(db, tmp_path, monkeypatch):
    import copy

    module, registry, document = approval(db, tmp_path, monkeypatch)
    module.apply_decisions(db.url.database, registry)
    # The fixture's independently retained rows identify the second 12-pair example.
    with session_scope(db) as session:
        from sqlalchemy import select

        from zucai.models import Match

        source = session.scalar(select(Match).where(Match.source == "apif", Match.match_id != API))
        from datetime import datetime

        partner = next(
            r
            for r in session.scalars(
                select(Match).where(
                    Match.source == "fd",
                    Match.home_team_id == source.home_team_id,
                    Match.away_team_id == source.away_team_id,
                )
            )
            if 3 * 3600
            < abs(
                (
                    datetime.fromisoformat(r.kickoff_utc.replace("Z", "+00:00"))
                    - datetime.fromisoformat(source.kickoff_utc.replace("Z", "+00:00"))
                ).total_seconds()
            )
            <= 36 * 3600
        )
        second = copy.deepcopy(document["decisions"][0])
        second.update(
            decision_id="test-only-2", match_ids=sorted([source.match_id, partner.match_id])
        )
        second["snapshot"] = module.decision_snapshot(session, second["match_ids"])
        session.get(Result, FD).home_goals = 8
    document["decisions"].append(second)
    registry.write_text(json.dumps(document))
    receipts = module.apply_decisions(db.url.database, registry)["receipts"]
    assert len(receipts) == 2
    with session_scope(db) as session:
        rows = module.decision_audit(session)
    assert {(r["decision_id"], r["status"]) for r in rows} == {
        ("test-only-1", "inactive"),
        ("test-only-2", "active"),
    }
