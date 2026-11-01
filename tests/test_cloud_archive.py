"""Archive contracts: four-source data, legacy upgrades and durable collector state."""

import json
import sqlite3
from datetime import date

import pytest

from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.db import get_engine, init_db


def seed_database(path):
    engine = get_engine(path)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
        for source in ("jc", "fd", "fdorg", "apif"):
            db.execute(
                "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES (?,?,?,1,2,?,'finished')",
                (source + ":1", source, "League", "2026-10-31T23:00:00.000000Z"),
            )
            db.execute(
                "INSERT INTO results "
                "(match_id,home_goals,away_goals,available_utc,available_source) "
                "VALUES (?,2,1,'2026-11-01T02:00:00.000000Z','captured')",
                (source + ":1",),
            )
        db.execute(
            "INSERT INTO closing_odds VALUES ('fd:1','B365','had',2.0,3.0,4.0,'publisher_closing')"
        )
        db.execute(
            "INSERT INTO closing_odds VALUES ('jc:1','PS','had',2.1,3.1,4.1,'publisher_closing')"
        )


def table_rows(path):
    with sqlite3.connect(path) as db:
        return {
            t: db.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()
            for t in ("teams", "matches", "results", "odds", "closing_odds")
        }


def test_four_sources_closing_prices_and_state_roundtrip(tmp_path):
    db = tmp_path / "data/zucai.db"
    seed_database(db)
    state = db.parent / "state"
    (state / "audit").mkdir(parents=True)
    (state / "alias_candidates").mkdir()
    (state / "api_football_quota.json").write_text(
        '{"date_utc":"2026-11-01","used":7,"remaining":93}'
    )
    for source in ("jc", "fd", "fdorg", "apif"):
        (state / f"audit/{source}_rejected_rows.csv").write_text("reason\ninvalid\n")
    (state / "alias_candidates/fdorg.json").write_text("{}")
    archive = tmp_path / "archive"
    meta = export_feeds(db, archive, date(2026, 11, 1), "test")
    assert meta["counts"]["closing_odds"] == 2
    assert set(meta["sources"]) == {"jc", "fd", "fdorg", "apif"}
    assert meta["sources"]["fd"]["counts"]["closing_odds"] == 1
    assert len(meta["sources"]["apif"]["sha256"]["matches"]) == 64
    assert (archive / "matches/2026-10.csv").exists()  # UTC kickoff, not Beijing display month.
    dest = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, dest)
    assert table_rows(dest) == table_rows(db)
    for path in state.rglob("*"):
        if path.is_file():
            assert (
                dest.parent / "state" / path.relative_to(state)
            ).read_bytes() == path.read_bytes()
    export_feeds(dest, archive, date(2026, 11, 1), "second")
    assert archive_info(archive)["sources"] == meta["sources"]
    assert archive_info(archive)["files"] == meta["files"]


def test_legacy_archive_without_closing_prices_restores_and_upgrades(tmp_path):
    db = tmp_path / "data/zucai.db"
    seed_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM closing_odds")
    archive = tmp_path / "archive"
    meta = export_feeds(db, archive, date(2026, 11, 1), "legacy")
    meta["version"] = 1
    meta["counts"].pop("closing_odds", None)
    meta["columns"].pop("closing_odds", None)
    meta["files"].pop("state/closing_odds.jsonl", None)
    meta.pop("sources", None)
    (archive / "state/closing_odds.jsonl").unlink(missing_ok=True)
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    dest = tmp_path / "restored/zucai.db"
    restore_feeds(archive, dest)
    assert table_rows(dest) == table_rows(db)
    assert export_feeds(dest, archive, date(2026, 11, 1), "upgrade")["version"] == 2


@pytest.mark.parametrize("damage", ["checksum", "missing_table", "source_counts", "unsafe_path"])
def test_corrupt_archive_never_replaces_destination(tmp_path, damage):
    db = tmp_path / "data/zucai.db"
    seed_database(db)
    archive = tmp_path / "archive"
    meta = export_feeds(db, archive, date(2026, 11, 1), "test")
    if damage == "checksum":
        (archive / "state/closing_odds.jsonl").write_text("[]\n")
    elif damage == "missing_table":
        meta["files"].pop("state/closing_odds.jsonl")
    elif damage == "source_counts":
        meta["sources"]["fd"]["counts"]["matches"] += 1
    else:
        meta["files"]["state/../outside.csv"] = "0" * 64
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    dest = tmp_path / "existing.db"
    dest.write_bytes(b"keep")
    with pytest.raises(ValueError):
        restore_feeds(archive, dest)
    assert dest.read_bytes() == b"keep"


@pytest.mark.parametrize("provider", ["fd", "fdorg", "apif"])
def test_official_reconciliation_ignores_stale_api_schedules(tmp_path, provider):
    from zucai.cloud_feeds import date_windows

    database = tmp_path / "data/zucai.db"
    seed_database(database)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM results WHERE match_id IN (?, 'jc:1')", (provider + ":1",))
        db.execute(
            "UPDATE matches SET status='scheduled',kickoff_utc='2026-09-01T12:00:00Z' "
            "WHERE source=?",
            (provider,),
        )
        db.execute(
            "UPDATE matches SET status='scheduled',kickoff_utc='2026-09-02T12:00:00Z' "
            "WHERE source='jc'"
        )
    assert date_windows(database, date(2026, 11, 1), date(2026, 11, 2), 3) == [
        (date(2026, 9, 2), date(2026, 9, 2)),
        (date(2026, 10, 31), date(2026, 11, 2)),
    ]
