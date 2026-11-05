"""T31 archives remain additive and restore every technical-data row."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from test_stats_rows import LocalRelease
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, MatchStat, Team
from zucai.stats_rows import publish_rows, restore_rows
from zucai.stats_schema import STATS_COLUMNS, STATS_MODELS, migrate_stats

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


def test_migration_only_creates_attached_tables(tmp_path):
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


def published_export(source, archive, through, run_id):
    release = LocalRelease(archive.parent / "release")
    publish_rows(
        source, archive.parent / "assets", release=release, tag="stats-raw-202611", run_id=run_id
    )
    return export_feeds(source, archive, through, run_id)


def test_technical_archive_roundtrip_and_manifest(tmp_path):
    source, dest, archive = tmp_path / "feeds.db", tmp_path / "restored.db", tmp_path / "archive"
    seed(source)
    meta = published_export(source, archive, date(2026, 11, 4), "t31")
    assert meta["technical_data"]["counts"]["match_stats"] == 1
    assert "state/apif_fixture_index.jsonl" in meta["files"]
    assert "state/match_stats.jsonl" not in meta["files"]
    restore_feeds(archive, dest)
    restore_rows(dest, archive, release=LocalRelease(tmp_path / "release"))
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
    published_export(source, archive, date(2026, 11, 4), "t31")
    manifest = archive / "feed_manifest.json"
    meta = json.loads(manifest.read_text())
    del meta["files"]["state/apif_fixture_index.jsonl"]
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
    meta = published_export(source, archive, date(2026, 11, 4), "t31")
    del meta["technical_data"]
    for model in STATS_MODELS:
        meta["files"].pop(f"state/{model.__tablename__}.jsonl", None)
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


def test_invalid_json_text_still_rejected(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)
    raw = json.loads((FIXTURE.parent / "statistics_archive.json").read_text())
    percentage = raw["response"][0]["statistics"][1]["value"]
    with sqlite3.connect(source) as db:
        # This intentionally omits JSON string quoting: 56% is not valid JSON text.
        db.execute("UPDATE match_stats SET raw_value=?", (percentage,))
    with pytest.raises(ValueError, match="JSON 字段无效"):
        export_feeds(source, tmp_path / "archive", date(2026, 11, 4), "t31")


def test_legacy_v1_technical_rows_remain_readable(tmp_path):
    import hashlib

    source, archive = tmp_path / "feeds.db", tmp_path / "archive"
    seed(source)
    meta = published_export(source, archive, date(2026, 11, 4), "legacy")
    counts = {}
    legacy_columns = {k: v for k, v in STATS_COLUMNS.items() if k != "match_injuries"}
    with sqlite3.connect(source) as db:
        for table, columns in legacy_columns.items():
            rows = db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1").fetchall()
            content = "".join(json.dumps(list(row)) + "\n" for row in rows).encode()
            key = f"state/{table}.jsonl"
            (archive / key).write_bytes(content)
            meta["files"][key] = hashlib.sha256(content).hexdigest()
            counts[table] = len(rows)
    meta["technical_data"] = {
        "version": 1,
        "columns": {k: list(v) for k, v in legacy_columns.items()},
        "counts": counts,
    }
    meta["files"].pop("state/stats_rows_manifest.json", None)
    (archive / "feed_manifest.json").write_text(json.dumps(meta))
    target = tmp_path / "legacy-restored.db"
    restore_feeds(archive, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT raw_value,version FROM match_stats").fetchone() == ('"0"', 1)


@pytest.mark.parametrize("archive_has_state", [False, True])
def test_restore_replaces_backfill_state_with_selected_snapshot(tmp_path, archive_has_state):
    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source)
    state_names = ("stats_backfill_cursor.json", "stats_coverage.json")
    archived = b'{"selected_snapshot":true}\n'
    if archive_has_state:
        state = source.parent / "state"
        state.mkdir()
        for name in state_names:
            (state / name).write_bytes(archived)
    archive = tmp_path / "archive"
    published_export(source, archive, date(2026, 11, 4), "selected")
    dest = tmp_path / "dest/feeds.db"
    state = dest.parent / "state"
    state.mkdir(parents=True)
    for name in state_names:
        (state / name).write_bytes(b'{"stale_later_snapshot":true}\n')
    quota = state / "api_football_quota.json"
    durable_quota = b'{"durable_quota":true}\n'
    quota.write_bytes(durable_quota)

    restore_feeds(archive, dest)

    for name in state_names:
        path = state / name
        if archive_has_state:
            assert path.read_bytes() == archived
        else:
            assert not path.exists()
    assert quota.read_bytes() == durable_quota
    exported = export_feeds(dest, tmp_path / "again", date(2026, 11, 4), "again")
    for name in state_names:
        assert (f"state/{name}" in exported["files"]) is archive_has_state
