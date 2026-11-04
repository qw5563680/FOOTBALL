"""T31 archives remain additive and restore every technical-data row."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, MatchStat, Team
from zucai.stats_schema import STATS_MODELS, migrate_stats

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t31/store.json"


def seed(path):
    data = json.loads(FIXTURE.read_text())
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in data["teams"])
        session.flush()
        session.add_all(Match(**m) for m in data["matches"])
        session.flush()
        session.add(MatchStat(**data["stat"]))
    engine.dispose()


def test_migration_only_creates_four_new_tables(tmp_path):
    path = tmp_path / "feeds.db"
    seed(path)
    with sqlite3.connect(path) as db:
        for model in reversed(STATS_MODELS):
            db.execute(f"DROP TABLE {model.__tablename__}")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    engine = get_engine(path)
    migrate_stats(engine)
    migrate_stats(engine)
    assert set(inspect(engine).get_table_names()) >= {m.__tablename__ for m in STATS_MODELS}
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    assert set(before) <= set(after)
    engine.dispose()


def test_technical_archive_roundtrip_and_manifest(tmp_path):
    source, dest, archive = tmp_path / "feeds.db", tmp_path / "restored.db", tmp_path / "archive"
    seed(source)
    meta = export_feeds(source, archive, date(2026, 11, 4), "t31")
    assert meta["technical_data"]["counts"]["match_stats"] == 1
    for model in STATS_MODELS:
        assert f"state/{model.__tablename__}.jsonl" in meta["files"]
    restore_feeds(archive, dest)
    engine = get_engine(dest)
    with session_scope(engine) as session:
        stat = session.scalars(select(MatchStat)).one()
        assert stat.value == 0 and stat.raw_value == "0" and stat.available_utc is None
    engine.dispose()
    second = export_feeds(dest, tmp_path / "again", date(2026, 11, 4), "again")
    assert second["technical_data"] == meta["technical_data"]
    assert second["sources"] == meta["sources"]


def test_partial_technical_manifest_is_rejected_without_replacing_db(tmp_path):
    source, archive = tmp_path / "feeds.db", tmp_path / "archive"
    seed(source)
    export_feeds(source, archive, date(2026, 11, 4), "t31")
    manifest = archive / "feed_manifest.json"
    meta = json.loads(manifest.read_text())
    del meta["files"]["state/match_events.jsonl"]
    manifest.write_text(json.dumps(meta))
    target = tmp_path / "keep.db"
    target.write_bytes(b"keep existing destination")
    with pytest.raises(ValueError, match="技术数据"):
        restore_feeds(archive, target)
    assert target.read_bytes() == b"keep existing destination"


def test_invalid_technical_timestamp_never_exports(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE match_stats SET captured_utc='2026-11-04T12:00:00'")
    with pytest.raises(ValueError, match="时间"):
        export_feeds(source, tmp_path / "archive", date(2026, 11, 4), "t31")


def test_pre_t31_archive_restores_with_empty_technical_tables(tmp_path):
    source, archive = tmp_path / "feeds.db", tmp_path / "archive"
    seed(source)
    meta = export_feeds(source, archive, date(2026, 11, 4), "t31")
    del meta["technical_data"]
    for model in STATS_MODELS:
        del meta["files"][f"state/{model.__tablename__}.jsonl"]
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    target = tmp_path / "old-restored.db"
    restore_feeds(archive, target)
    with sqlite3.connect(target) as db:
        for model in STATS_MODELS:
            assert db.execute(f"SELECT count(*) FROM {model.__tablename__}").fetchone()[0] == 0


def test_wrong_side_is_rejected_on_verify(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE match_stats SET team_id=2")
    with pytest.raises(ValueError, match="身份"):
        export_feeds(source, tmp_path / "archive", date(2026, 11, 4), "t31")
