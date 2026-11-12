"""JC pending-name evidence must survive the same archive as its new team."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from zucai.cloud_feeds import TABLES, archive_info, export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team


def test_jc_alias_candidates_roundtrip_and_hash_guard(tmp_path):
    database = tmp_path / "source/football.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    name = "state/alias_candidates/jc.json"
    pending = database.parent / name
    pending.parent.mkdir(parents=True)
    original = (
        '{"source":"jc","candidates":[{"raw_name":"未知缩写","status":"pending"}]}\n'.encode()
    )
    pending.write_bytes(original)
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 12), "t54-test")
    assert name in archive_info(archive)["files"]
    assert (archive / name).read_bytes() == original
    restored = tmp_path / "restored/football.db"
    restore_feeds(archive, restored)
    assert (restored.parent / name).read_bytes() == original
    before = restored.read_bytes()
    (archive / name).write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="校验"):
        restore_feeds(archive, restored)
    assert restored.read_bytes() == before
    assert (restored.parent / name).read_bytes() == original


@pytest.mark.parametrize(
    "name", ["state/alias_candidates/jc.json", "state/audit/jc_identity_merges.csv"]
)
def test_restore_older_snapshot_removes_later_jc_identity_state(tmp_path, name):
    database = tmp_path / "source/football.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "old-archive"
    export_feeds(database, archive, date(2026, 11, 12), "before-t54")
    restored = tmp_path / "restored/football.db"
    restore_feeds(archive, restored)
    later = restored.parent / name
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_bytes(b"later evidence for another database state\n")
    restore_feeds(archive, restored)
    assert not later.exists()


def test_manual_reconcile_publishes_verified_jc_binding_and_repeat_is_noop(tmp_path, monkeypatch):
    from zucai.cloud_feeds import main

    fixture = Path(__file__).parents[1] / "data/fixtures/t54"
    database = tmp_path / "seed/football.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        for table, cls, wanted in (
            ("teams", Team, {1148, 1149, 1156}),
            ("matches", Match, {"jc:2042694", "apif:1528963"}),
        ):
            for line in (fixture / f"{table}.jsonl").read_text().splitlines():
                values = json.loads(line)
                if values[0] not in wanted:
                    continue
                row = dict(zip(TABLES[table], values, strict=True))
                if table == "teams":
                    row["aliases"] = json.loads(row["aliases"])
                session.add(cls(**row))
            session.flush()
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 12), "seed")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    for index in range(2):
        work = tmp_path / f"run-{index}"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
        monkeypatch.setattr(
            sys, "argv", ["feeds", "reconcile", "--archive", str(archive), "--run-id", str(index)]
        )
        main()
        manifest = archive_info(archive)
        assert manifest["collection"]["jc_identity"]["rekeyed_matches"] == (1 if index == 0 else 0)
        assert manifest["through_beijing"] == "2026-11-12"
        restored = tmp_path / f"read-{index}/football.db"
        restore_feeds(archive, restored)
        engine = get_engine(restored)
        with session_scope(engine) as session:
            match = session.get(Match, "jc:2042694")
            assert (match.home_team_id, match.away_team_id) == (1149, 1148)
            assert match.counterpart_id == "apif:1528963"
            assert session.get(Team, 1156).name == "阿尔巴尼"
        engine.dispose()
        for name in ("jc_identity_merges.csv", "jc_relink.csv"):
            assert (restored.parent / "state/audit" / name).read_bytes() == (
                archive / "state/audit" / name
            ).read_bytes()
        assert "竞彩" in (work / "summary.md").read_text()
