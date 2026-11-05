"""The fourth observation table must survive Release and old archive boundaries."""

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from test_injuries_storage import ingest
from test_injuries_storage import injury_database as injury_database
from test_stats_rows import LocalRelease, seed
from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, MatchInjury, Result, Team
from zucai.stats_archive import check_stats, write_rows_manifest
from zucai.stats_availability import annotate_coverage
from zucai.stats_rows import publish_rows, restore_rows

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t38/archive.json"


def injury_db(path):
    data = json.loads(FIXTURE.read_bytes())
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in data["teams"])
        session.flush()
        session.add(Match(**data["match"]))
        session.flush()
        session.add(ApifFixtureIndex(**data["index"]))
        session.add_all(MatchInjury(**row) for row in data["injuries"])
    engine.dispose()


@pytest.fixture
def archived_injury_database(injury_database):
    engine = get_engine(injury_database)
    from sqlalchemy import select

    with session_scope(engine) as session:
        template = json.loads(FIXTURE.read_bytes())["prior_result"]
        for game in session.scalars(select(Match).where(Match.status == "finished")):
            session.add(Result(match_id=game.match_id, **template))
    engine.dispose()
    return injury_database


def publish(database, out, release, run="test"):
    return publish_rows(database, out, release=release, tag="stats-raw-202611", run_id=run)


def test_injury_linked_and_unlinked_rows_restore_exactly_from_release(tmp_path):
    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    injury_db(source)
    release = LocalRelease(tmp_path / "release")
    block = publish(source, tmp_path / "assets", release)
    assert block["version"] == 3 and block["counts"]["match_injuries"] == 2
    assert len(block["row_shards"]) == 1
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 4), "test")
    assert not (archive / "state/match_injuries.jsonl").exists()
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT count(*) FROM match_injuries").fetchone()[0] == 0
    assert restore_rows(restored, archive, release=release)["match_injuries"] == 2
    with sqlite3.connect(source) as a, sqlite3.connect(restored) as b:
        assert (
            a.execute("SELECT * FROM match_injuries").fetchall()
            == b.execute("SELECT * FROM match_injuries").fetchall()
        )
        assert check_stats(b)
    before = restored.read_bytes()
    assert restore_rows(restored, release=release)["match_injuries"] == 0
    assert restored.read_bytes() == before
    again = export_feeds(restored, tmp_path / "again", date(2026, 11, 4), "again")
    assert again["technical_data"] == block


def test_legacy_three_row_table_release_restores_then_upgrades(tmp_path):
    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source)
    release = LocalRelease(tmp_path / "release")
    block = publish(source, tmp_path / "assets", release)
    # Reproduce the exact T32 descriptor shape; shard bytes contain only old tables.
    block["version"] = 2
    for key in ("columns", "counts"):
        block[key].pop("match_injuries")
    for mapping in block["snapshot"].values():
        mapping.pop("match_injuries")
    for shard in block["row_shards"]:
        shard["counts"].pop("match_injuries")
    write_rows_manifest(source, block)
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 4), "old")
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert restore_rows(restored, archive, release=release)["match_stats"] == 1
    again = export_feeds(restored, tmp_path / "again", date(2026, 11, 4), "unchanged")
    assert again["technical_data"] == block
    upgraded = publish(restored, tmp_path / "new-assets", release, "upgraded")
    assert upgraded["version"] == 3 and upgraded["counts"]["match_injuries"] == 0
    assert (
        upgraded["snapshot"]["sha256"]["match_stats"] == block["snapshot"]["sha256"]["match_stats"]
    )


@pytest.mark.parametrize("damage", ["identity", "clock"])
def test_invalid_injury_identity_or_clock_blocks_publication(tmp_path, damage):
    database = tmp_path / "feeds.db"
    injury_db(database)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        if damage == "identity":
            db.execute("UPDATE match_injuries SET provider_team_id=999 WHERE match_id IS NOT NULL")
        else:
            db.execute(
                "UPDATE match_injuries SET available_utc=NULL WHERE availability_basis='captured'"
            )
    release = LocalRelease(tmp_path / "release")
    with pytest.raises(ValueError, match="伤停"):
        publish(database, tmp_path / "out", release)
    assert not release.root.exists()


def test_injury_coverage_includes_unlinked_nulls_and_digest_only_quotes_it(
    digest_archive, tmp_path
):
    database = tmp_path / "feeds.db"
    injury_db(database)
    coverage = annotate_coverage(json.loads(FIXTURE.read_bytes())["coverage"], database)
    assert coverage["injuries"]["total_rows"] == 2
    assert coverage["injuries"]["linked_rows"] == 1
    assert coverage["injuries"]["linkage_rate"] == 0.5
    assert coverage["injuries"]["captured_rows"] == coverage["injuries"]["backfilled_rows"] == 1
    assert coverage["rows"][0]["captured_rows"] == 1
    name = "state/stats_coverage.json"
    target = digest_archive / name
    target.write_text(json.dumps(coverage, ensure_ascii=False))
    manifest = digest_archive / "feed_manifest.json"
    meta = json.loads(manifest.read_bytes())
    meta["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(meta))
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "| 1 | 1 | 1 / 2 | 0.5 |" in text
    assert text.index("伤停入库") > text.index("## 异常与采集记录")
    assert snapshot(digest_archive) == before
    assert archive_info(digest_archive)["files"][name] == meta["files"][name]


def test_no_injury_observations_has_unknown_linkage_rate(tmp_path):
    database = tmp_path / "feeds.db"
    injury_db(database)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM match_injuries")
    coverage = annotate_coverage(json.loads(FIXTURE.read_bytes())["coverage"], database)
    assert coverage["injuries"]["total_rows"] == 0
    assert coverage["injuries"]["linkage_rate"] is None
    assert coverage["injuries"]["rows"] == []


def test_injury_cli_ingests_only_verified_local_raw(tmp_path):
    from zucai.cli import build_parser
    from zucai.sources.apif_stats import instant
    from zucai.sources.base import RawStore
    from zucai.stats_command import run_stats

    database = tmp_path / "feeds.db"
    injury_db(database)
    raw = RawStore(tmp_path / "raw").save(
        "api-football",
        "injuries",
        (FIXTURE.parent / "injuries.json").read_bytes(),
        "json",
        captured_at=instant("2026-11-04T11:00:00Z"),
        url="https://v3.football.api-sports.io/injuries?league=39&season=2024",
        status_code=200,
    )
    args = build_parser().parse_args(
        [
            "stats",
            "ingest-injuries",
            "--db",
            str(database),
            "--raw-file",
            str(raw),
            "--league",
            "39",
            "--season",
            "2024",
        ]
    )
    assert not args.daily
    assert run_stats(args)["inserted"] == 4
    assert run_stats(args)["inserted"] == 0


def test_capture_receipts_live_in_release_and_restore_with_rows(archived_injury_database, tmp_path):
    from zucai.features.injuries import InjuryFeatures
    from zucai.injuries_archive import references

    ingest(archived_injury_database)
    release = LocalRelease(tmp_path / "release")
    block = publish(archived_injury_database, tmp_path / "assets", release)
    assert len(block["injury_receipts"]) == 1
    archive = tmp_path / "archive"
    export_feeds(archived_injury_database, archive, date(2024, 9, 9), "receipt")
    assert not (archive / "state/injuries_captures").exists()
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    restore_rows(restored, archive, release=release)
    assert references(restored.parent) == references(archived_injury_database.parent)
    for path in references(restored.parent):
        assert (restored.parent / path).read_bytes() == (
            archived_injury_database.parent / path
        ).read_bytes()
    engine = get_engine(restored)
    with session_scope(engine) as session:
        builder = InjuryFeatures(session)
        features = builder.build(builder.matches["fd:target"], as_of="2024-09-10T11:00:00Z")
        assert features["home"]["missing_n"]["value"] == 2
    engine.dispose()


def test_unchanged_capture_interval_can_publish_without_changing_history(
    archived_injury_database, tmp_path
):
    ingest(archived_injury_database, captured="2024-09-08T10:00:00Z")
    release = LocalRelease(tmp_path / "release")
    first = publish(archived_injury_database, tmp_path / "one", release, "one")
    ingest(archived_injury_database, captured="2024-09-09T10:00:00Z")
    second = publish(archived_injury_database, tmp_path / "two", release, "two")
    assert first["counts"] == second["counts"]
    assert len(second["injury_receipts"]) == 2
    with sqlite3.connect(archived_injury_database) as db:
        db.execute("UPDATE match_injuries SET reason='tampered'")
    with pytest.raises(ValueError, match="ID 冲突"):
        publish(archived_injury_database, tmp_path / "three", release, "three")


def test_capture_asset_corruption_aborts_restore_before_database_change(
    archived_injury_database, tmp_path
):
    ingest(archived_injury_database)
    release = LocalRelease(tmp_path / "release")
    block = publish(archived_injury_database, tmp_path / "assets", release)
    archive = tmp_path / "archive"
    export_feeds(archived_injury_database, archive, date(2024, 9, 9), "receipt")
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    before = restored.read_bytes()
    asset = block["injury_receipts"][0]
    (release.root / asset["tag"] / asset["name"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="伤停压缩回执"):
        restore_rows(restored, archive, release=release)
    assert restored.read_bytes() == before
    assert not (restored.parent / "state/injuries_captures").exists()


def test_new_empty_capture_requires_published_receipt(archived_injury_database, tmp_path):
    from test_injuries_storage import document

    release = LocalRelease(tmp_path / "release")
    publish(archived_injury_database, tmp_path / "one", release)
    data = document()
    data.update(response=[], results=0)
    ingest(archived_injury_database, data=data)
    with pytest.raises(ValueError, match="回执尚未发布"):
        export_feeds(archived_injury_database, tmp_path / "archive", date(2024, 9, 9), "empty")


def test_capture_csv_clock_must_agree_with_immutable_payload(archived_injury_database, tmp_path):
    import csv

    ingest(archived_injury_database)
    path = archived_injury_database.parent / "state/audit/injuries_ingest_audit.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]["captured_utc"] = "2024-09-01T10:00:00Z"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    release = LocalRelease(tmp_path / "release")
    with pytest.raises(ValueError, match="伤停"):
        publish(archived_injury_database, tmp_path / "out", release)
