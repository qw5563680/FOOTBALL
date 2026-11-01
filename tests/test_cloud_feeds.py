import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest


def test_private_archive_roundtrip_and_corruption_does_not_replace_existing_db(
    tmp_path, monkeypatch
):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data/zucai.db"
    archive = tmp_path / "archive"
    e = get_engine(source)
    init_db(e)
    f = Path(__file__).parents[1] / "data/fixtures/sporttery_calculator_20261101.json"
    snap = datetime(2026, 11, 1, 12, 42, tzinfo=UTC)
    with session_scope(e) as s:
        save_schedule(s, SportteryCalculator().parse(f.read_bytes()), snap)
    e.dispose()
    export_feeds(source, archive, date(2026, 10, 31), "verification")
    import csv

    with (archive / "matches/2026-11.csv").open(encoding="utf-8-sig") as view:
        first = next(row for row in csv.DictReader(view) if row["match_id"] == "jc:2042420")
    assert (
        first["had_home"] == "3.46" and first["had_snapshot_utc"] == "2026-11-01T12:42:00.000000Z"
    )
    restore = tmp_path / "new/data/zucai.db"
    restore_feeds(archive, restore)

    def counts(p):
        with sqlite3.connect(p) as c:
            return [
                c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("teams", "matches", "odds", "results")
            ]

    assert counts(restore) == counts(source)
    damaged = next((archive / "state/odds").glob("*.jsonl"))
    damaged.write_text("[]\n", encoding="utf8")
    with pytest.raises(ValueError, match="校验"):
        restore_feeds(archive, restore)
    assert counts(restore) == counts(source)


def test_collection_window_recovers_missing_days_and_includes_unsettled_old_fixtures(tmp_path):
    from zucai.cloud_feeds import date_windows
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import JcMatch, save_schedule

    path = tmp_path / "data/zucai.db"
    e = get_engine(path)
    init_db(e)
    kickoff = datetime(2026, 9, 1, 12, tzinfo=UTC)
    with session_scope(e) as s:
        g = JcMatch("jc:12", kickoff.date(), "周二001", "联赛", "主队", "客队", kickoff, [])
        save_schedule(s, [g], kickoff - timedelta(hours=1))
    assert date_windows(path, date(2026, 10, 28), date(2026, 11, 1), 3) == [
        (date(2026, 9, 1), date(2026, 9, 1)),
        (date(2026, 10, 27), date(2026, 11, 1)),
    ]
    e.dispose()


def test_sync_refuses_to_replace_a_preexisting_working_database(tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import main

    monkeypatch.chdir(tmp_path)
    p = Path("data/zucai.db")
    p.parent.mkdir()
    p.write_bytes(b"existing")
    monkeypatch.setattr(sys, "argv", ["feeds", "sync", "--archive", "archive", "--run-id", "trial"])
    with pytest.raises(ValueError, match="已有数据库"):
        main()
    assert p.read_bytes() == b"existing"


def test_cloud_verify_only_restores_the_archive_and_does_not_call_the_source(tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import export_feeds, main
    from zucai.db import get_engine, init_db

    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base/feeds.db"
    e = get_engine(base)
    init_db(e)
    e.dispose()
    arc = tmp_path / "archive"
    export_feeds(base, arc, date.today(), "sample")
    before = (arc / "feed_manifest.json").read_bytes()
    monkeypatch.setattr(sys, "argv", ["feeds", "verify", "--archive", str(arc), "--run-id", "test"])
    main()
    assert (tmp_path / "data/zucai.db").is_file() and (
        arc / "feed_manifest.json"
    ).read_bytes() == before


def test_site_policy_denial_is_not_retried_as_a_transient_server_error():
    from zucai.sources.base import make_session

    with make_session() as client:
        retry = client.get_adapter("https://webapi.sporttery.cn/").max_retries
        assert not retry.is_retry("GET", 567) and retry.is_retry("GET", 503)


def test_export_removes_only_its_stale_month_and_reads_older_match_schema(tmp_path, monkeypatch):
    import json

    from zucai.cloud_feeds import digest, export_feeds, restore_feeds
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import JcMatch, save_schedule

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data/zucai.db"
    archive = tmp_path / "archive"
    e = get_engine(db)
    init_db(e)
    clock = datetime(2026, 10, 31, 12, tzinfo=UTC)
    g = JcMatch("jc:12", clock.date(), "周六001", "联赛", "主", "客", clock, [])
    with session_scope(e) as s:
        save_schedule(s, [g], clock - timedelta(days=1))
    e.dispose()
    export_feeds(db, archive, date(2026, 10, 31), "first")
    keep = archive / "matches/field-notes.txt"
    keep.write_text("user")
    with sqlite3.connect(db) as s:
        s.execute("UPDATE matches SET kickoff_utc='2026-11-01T14:00:00.000000Z'")
    export_feeds(db, archive, date(2026, 11, 1), "second")
    assert not (archive / "matches/2026-10.csv").exists() and keep.read_text() == "user"
    # The first published seed predates the optional cross-source alias.
    m = archive / "feed_manifest.json"
    meta = json.loads(m.read_text(encoding="utf8"))
    key = "state/matches.jsonl"
    payload = archive / key
    index = meta["columns"]["matches"].index("counterpart_id")
    meta["columns"]["matches"].pop(index)
    row = json.loads(payload.read_text(encoding="utf8"))
    row.pop(index)
    payload.write_text(json.dumps(row) + "\n", encoding="utf8")
    meta["files"][key] = digest(payload)
    m.write_text(json.dumps(meta), encoding="utf8")
    target = tmp_path / "new/zucai.db"
    restore_feeds(archive, target)
    with sqlite3.connect(target) as s:
        assert s.execute("select counterpart_id from matches").fetchone() == (None,)
