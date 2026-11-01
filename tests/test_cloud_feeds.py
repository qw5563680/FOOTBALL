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
