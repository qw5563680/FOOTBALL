"""The existing offline T31 observations acquire clocks only in the daily path."""

import json
import sqlite3
from datetime import timedelta

import pytest

from test_stats_storage import database, save, setup_link  # noqa: F401
from zucai.sources.apif_stats import instant
from zucai.stats_availability import annotate_coverage
from zucai.stats_storage import ingest_apif


@pytest.mark.parametrize(
    "daily,status,hours,expected",
    [
        (True, "finished", 3, "captured"),
        (True, "finished", 2, "backfilled"),
        (True, "scheduled", 3, "backfilled"),
        (False, "finished", 3, "backfilled"),
    ],
)
def test_daily_clocks(database, tmp_path, daily, status, hours, expected):  # noqa: F811
    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, "statistics")
    sidecar = raw.with_suffix(".json.meta.json")
    meta = json.loads(sidecar.read_bytes())
    with sqlite3.connect(database) as db:
        kickoff = db.execute("SELECT kickoff_utc FROM matches WHERE match_id='jc:1'").fetchone()[0]
        db.execute("UPDATE matches SET status=? WHERE match_id='jc:1'", (status,))
    stamp = (instant(kickoff) + timedelta(hours=hours)).isoformat()
    meta["fetched_at_utc"] = stamp
    sidecar.write_text(json.dumps(meta))
    ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics", daily=daily)
    with sqlite3.connect(database) as db:
        assert {
            (b, instant(a) if a else None)
            for b, a in db.execute("SELECT availability_basis, available_utc FROM match_stats")
        } == {(expected, instant(stamp) if expected == "captured" else None)}
    coverage = annotate_coverage({"rows": []}, database)
    assert coverage["availability_basis_counts"] == {expected: 36}
    before = database.read_bytes()
    ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics", daily=True)
    assert database.read_bytes() == before  # No relabeling old backfills on rerun.


def test_reuse_fd_csv_without_request_and_linked_jc_attachment(database, tmp_path):  # noqa: F811
    from pathlib import Path

    from test_stats_rows import LocalRelease
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.db import get_engine, session_scope
    from zucai.models import Result
    from zucai.sources.base import RawStore
    from zucai.stats_captured_fd import ingest_captured_files
    from zucai.stats_rows import publish_rows, restore_rows
    from zucai.stats_schedule import empty_cursor, plan

    now = instant("2026-11-04T12:00:00Z")
    raw_root = tmp_path / "raw"
    fixture = Path(__file__).parents[1] / "data/fixtures/t31/fd.csv"
    RawStore(raw_root).save(
        "football-data-co-uk",
        "E0-2425",
        fixture.read_bytes(),
        "csv",
        captured_at=now,
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )
    with sqlite3.connect(database) as db:
        original_id = db.execute("SELECT match_id FROM matches WHERE source='fd'").fetchone()[0]
        db.execute(
            "UPDATE matches SET match_id='jc:linked-fd', source='jc', counterpart_id=?, "
            "status='finished' WHERE source='fd'",
            (original_id,),
        )
    goals = json.loads(
        (Path(__file__).parents[1] / "data/fixtures/t34a/prospective.json").read_bytes()
    )["rows"][0]
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.add(
            Result(
                match_id="jc:linked-fd",
                home_goals=goals["home_goals"],
                away_goals=goals["away_goals"],
                available_utc=now,
                available_source="captured",
            )
        )
    engine.dispose()
    selection = plan("E0", "2024", "fd", now)
    cursor = empty_cursor()
    result = ingest_captured_files(database, raw_root, selection, now, cursor, str)
    assert result["inserted"] == 16 and result["reused_files"] == 1
    assert sum(r["attempts"] for r in cursor["records"].values()) == 0
    with sqlite3.connect(database) as db:
        assert set(
            db.execute("SELECT match_id,provider_fixture_id,availability_basis FROM match_stats")
        ) == {("jc:linked-fd", original_id, "captured")}
    coverage = annotate_coverage(
        {"rows": [dict(league="英超", season=2024, endpoint="fd")]}, database
    )
    assert coverage["rows"][0]["captured_rows"] == 16
    assert (
        ingest_captured_files(database, raw_root, selection, now, cursor, str)["reused_files"] == 0
    )
    release = LocalRelease(tmp_path / "release")
    publish_rows(
        database, tmp_path / "assets", release=release, tag="stats-raw-202611", run_id="captured"
    )
    archive = tmp_path / "archive"
    export_feeds(database, archive, now.date(), "captured")
    restored = tmp_path / "restored.db"
    restore_feeds(archive, restored)
    restore_rows(restored, archive, release=release)
    with sqlite3.connect(restored) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM match_stats WHERE availability_basis='captured'"
            ).fetchone()[0]
            == 16
        )
