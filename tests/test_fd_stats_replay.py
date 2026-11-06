"""Bounded archived-file replay must validate all inputs before any ingestion."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore

FIX = Path(__file__).parents[1] / "data/fixtures/t31"


def setup_replay(tmp_path):
    database = tmp_path / "feeds.db"
    engine = get_engine(database)
    init_db(engine)
    seed = json.loads((FIX / "store.json").read_text())
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in seed["teams"])
        session.flush()
        session.add_all(Match(**m) for m in seed["matches"])
    engine.dispose()
    raw = RawStore(tmp_path / "raw").save(
        "football-data-co-uk",
        "E0-2425",
        (FIX / "fd.csv").read_bytes(),
        "csv",
        captured_at=instant("2026-11-04T12:00:00Z"),
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )
    sidecar = raw.with_suffix(".csv.meta.json")
    item = dict(
        path=str(raw),
        div="E0",
        season="2425",
        sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        sidecar_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        captured_utc="2026-11-04T12:00:00Z",
        raw_location="fixture://t31/fd.csv",
    )
    plan = tmp_path / "replay.json"
    plan.write_text(json.dumps({"files": [item]}))
    return database, plan, item


def run_replay(database, plan):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "zucai.cli",
            "stats",
            "replay-fd",
            "--db",
            str(database),
            "--plan",
            str(plan),
            "--leagues-file",
            str(FIX / "fd_leagues.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_replay_command_preserves_null_clocks_and_is_repeatable(tmp_path):
    database, plan, _ = setup_replay(tmp_path)
    result = run_replay(database, plan)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM match_stats").fetchone() == (16,)
        assert db.execute(
            "SELECT value FROM match_stats WHERE record_key='home:shots_on_target'"
        ).fetchone() == (4.0,)
        assert db.execute(
            "SELECT value FROM match_stats WHERE record_key='away:shots_on_target'"
        ).fetchone() == (None,)
        assert db.execute(
            "SELECT DISTINCT availability_basis,available_utc,captured_utc FROM match_stats"
        ).fetchall() == [("backfilled", None, "2026-11-04T12:00:00.000000Z")]
    before = database.read_bytes()
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    audit_before = audit.read_bytes()
    assert run_replay(database, plan).returncode == 0
    assert database.read_bytes() == before
    assert audit.read_bytes() == audit_before


@pytest.mark.parametrize("damage", ["hash", "sidecar", "capture", "duplicate", "missing_field"])
def test_replay_rejects_bad_later_entry_before_writing_earlier_file(tmp_path, damage):
    database, plan, item = setup_replay(tmp_path)
    bad = dict(item)
    if damage == "hash":
        bad["sha256"] = "0" * 64
    elif damage == "sidecar":
        bad["sidecar_sha256"] = "0" * 64
    elif damage == "capture":
        bad["captured_utc"] = "2026-11-05T12:00:00Z"
    elif damage == "missing_field":
        del bad["sha256"]
    plan.write_text(json.dumps({"files": [item, bad]}))
    before = database.read_bytes()
    assert run_replay(database, plan).returncode != 0
    assert database.read_bytes() == before
    assert not (database.parent / "state/audit/stats_ingest_audit.csv").exists()
