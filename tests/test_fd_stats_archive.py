"""Additional FD linkage survives real feeds and external row-shard restoration."""

import hashlib
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_archive import row_snapshot
from zucai.stats_rows import publish_rows, restore_rows
from zucai.stats_storage import ingest_fd

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t31"
LINK_FILE = "state/fd_stats_links.jsonl"


class LocalRelease:
    """Only the remote asset boundary is replaced with a local directory."""

    def __init__(self, root):
        self.root = root

    def upload(self, tag, path):
        target = self.root / tag / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise ValueError("asset collision")
        shutil.copyfile(path, target)
        return dict(
            tag=tag,
            name=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=path.stat().st_size,
        )

    def download(self, tag, name, dest):
        shutil.copyfile(self.root / tag / name, dest)
        return dest


def seed(tmp_path, *, counterpart="fdorg:77", legacy=False):
    database = tmp_path / "source/feeds.db"
    database.parent.mkdir(parents=True)
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(id=1, name="主队", aliases=[]), Team(id=2, name="客队", aliases=[])])
        session.flush()
        session.add(
            Match(
                match_id="jc:1",
                source="jc",
                league="英超",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2024-10-01T19:00:00Z",
                status="scheduled",
                counterpart_id=counterpart,
            )
        )
    engine.dispose()
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps(
            {
                "英超": {
                    "Home": {
                        "canonical": "主队",
                        "status": "approved",
                        "approved_by": "owner",
                        "approved_utc": "2026-11-04T00:00:00Z",
                        "approval_pr": "https://example.org/approval/1",
                    },
                    "Away": {
                        "canonical": "客队",
                        "status": "approved",
                        "approved_by": "owner",
                        "approved_utc": "2026-11-04T00:00:00Z",
                        "approval_pr": "https://example.org/approval/1",
                    },
                }
            },
            ensure_ascii=False,
        )
    )
    raw = RawStore(tmp_path / "raw").save(
        "football-data",
        "E0",
        (FIXTURE / "fd.csv").read_bytes(),
        "csv",
        captured_at=instant("2026-11-04T12:00:00Z"),
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )
    options = {} if legacy else {"aliases_path": aliases}
    assert (
        ingest_fd(
            database,
            raw,
            div="E0",
            season="2425",
            leagues_path=FIXTURE / "fd_leagues.json",
            **options,
        )["inserted"]
        > 0
    )
    return database


def publish(tmp_path, database):
    release = LocalRelease(tmp_path / "release")
    publish_rows(
        database, tmp_path / "assets", release=release, tag="stats-raw-202611", run_id="archive"
    )
    archive = tmp_path / "archive"
    meta = export_feeds(database, archive, date(2026, 11, 4), "archive")
    return archive, meta, release


def snapshot(database):
    with sqlite3.connect(database) as db:
        return (
            db.execute("SELECT * FROM fd_stats_links ORDER BY provider_fixture_id").fetchall(),
            row_snapshot(db),
            db.execute("SELECT match_id,counterpart_id FROM matches ORDER BY match_id").fetchall(),
        )


def save_manifest(archive, meta):
    (archive / "feed_manifest.json").write_text(json.dumps(meta))


def test_additional_link_survives_feeds_then_external_rows(tmp_path):
    database = seed(tmp_path)
    before = snapshot(database)
    archive, meta, release = publish(tmp_path, database)
    assert LINK_FILE in meta["files"]
    assert "fd_stats_links" not in meta["technical_data"]["columns"]
    dest = tmp_path / "dest/feeds.db"
    restore_feeds(archive, dest)
    with sqlite3.connect(dest) as db:
        assert db.execute("SELECT count(*) FROM match_stats").fetchone() == (0,)
        assert db.execute("SELECT match_id FROM fd_stats_links").fetchone() == ("jc:1",)
    assert restore_rows(dest, archive, release=release)["match_stats"] > 0
    assert snapshot(dest) == before
    assert restore_rows(dest, archive, release=release)["match_stats"] == 0
    again = export_feeds(dest, tmp_path / "again", date(2026, 11, 4), "again")
    assert again["files"][LINK_FILE] == meta["files"][LINK_FILE]
    assert again["technical_data"] == meta["technical_data"]
    assert snapshot(dest)[2] == [("jc:1", "fdorg:77")]


def test_legacy_counterpart_archive_needs_no_additional_index(tmp_path):
    database = seed(tmp_path, counterpart="fd:E0:20241001:home:away", legacy=True)
    archive, meta, release = publish(tmp_path, database)
    assert LINK_FILE not in meta["files"] and "fd_stats_links" not in meta
    dest = tmp_path / "dest/feeds.db"
    restore_feeds(archive, dest)
    assert restore_rows(dest, archive, release=release)["match_stats"] > 0
    assert snapshot(dest) == snapshot(database)


@pytest.mark.parametrize(
    "damage", ["missing", "hash", "shape", "columns", "count", "team", "evidence"]
)
def test_invalid_link_archive_never_replaces_destination(tmp_path, damage):
    database = seed(tmp_path)
    archive, meta, _ = publish(tmp_path, database)
    link = archive / LINK_FILE
    if damage == "missing":
        link.unlink()
    elif damage == "hash":
        link.write_bytes(link.read_bytes() + b"\n")
    else:
        if damage == "columns":
            meta["fd_stats_links"]["columns"] = []
        elif damage == "count":
            meta["fd_stats_links"]["count"] += 1
        else:
            rows = [json.loads(line) for line in link.read_text().splitlines()]
            columns = meta["fd_stats_links"]["columns"]
            if damage == "shape":
                rows[0].pop()
            elif damage == "team":
                rows[0][columns.index("home_team_id")] = 2
            else:
                rows[0][columns.index("evidence")] = "{}"
            link.write_text("".join(json.dumps(row) + "\n" for row in rows))
            meta["files"][LINK_FILE] = hashlib.sha256(link.read_bytes()).hexdigest()
        save_manifest(archive, meta)
    target = tmp_path / "keep.db"
    target.write_bytes(b"existing destination")
    with pytest.raises(ValueError):
        restore_feeds(archive, target)
    assert target.read_bytes() == b"existing destination"


def test_dropped_optional_link_is_rejected_when_dependent_rows_restore(tmp_path):
    database = seed(tmp_path)
    archive, meta, release = publish(tmp_path, database)
    del meta["files"][LINK_FILE]
    del meta["fd_stats_links"]
    save_manifest(archive, meta)
    target = tmp_path / "dest/feeds.db"
    restore_feeds(archive, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="身份|fd|FD"):
        restore_rows(target, archive, release=release)
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("raw_sha256", "b" * 64),
        ("captured_utc", "2026-11-05T12:00:00Z"),
        ("raw_location", "forged://raw.csv"),
    ],
)
def test_link_cannot_authorize_unobserved_stat_provenance(tmp_path, column, value):
    database = seed(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute(f"UPDATE match_stats SET {column}=?", (value,))
    with pytest.raises(ValueError, match="身份|证据|原件|fd|FD"):
        publish(tmp_path, database)
