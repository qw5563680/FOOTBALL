"""External snapshots retain exact SQL values, IDs and every historical version."""

import copy
import hashlib
import io
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, MatchStat, Team

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t32/archive_store.json"


class LocalRelease:
    def __init__(self, root):
        self.root = root
        self.downloads = []

    def upload(self, tag, path):
        data = path.read_bytes()
        target = self.root / tag / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("collision")
        target.write_bytes(data)
        return dict(
            tag=tag, name=path.name, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )

    def download(self, tag, name, dest):
        self.downloads.append((tag, name))
        shutil.copyfile(self.root / tag / name, dest)
        return dest


def seed(path, *, versions=1):
    data = json.loads(FIXTURE.read_text())
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in data["teams"])
        session.flush()
        session.add_all(Match(**m) for m in data["matches"])
        session.flush()
        for number in range(1, versions + 1):
            stat = copy.deepcopy(data["stat"])
            stat.update(
                version=number,
                raw_sha256=str(number) * 64,
                raw_value=[3, "56%", None][(number - 1) % 3],
            )
            session.add(MatchStat(**stat))
    engine.dispose()


def rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM match_stats ORDER BY id").fetchall()


def publish(path, out, release, **options):
    from zucai.stats_rows import publish_rows

    return publish_rows(
        path,
        out,
        release=release,
        tag="stats-raw-202611",
        run_id=options.pop("run_id", "one"),
        **options,
    )


def test_no_rows_silently_exported_without_release(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)
    with pytest.raises(ValueError, match="publish_rows|发布"):
        export_feeds(source, tmp_path / "archive", date(2026, 11, 4), "run")


def test_default_restore_empty_explicit_restore_exact_and_repeat_byte_stable(tmp_path):
    from zucai.stats_rows import restore_rows

    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source, versions=3)
    release = LocalRelease(tmp_path / "release")
    manifest = publish(source, tmp_path / "out", release)
    assert manifest["counts"]["match_stats"] == 3
    assert len(manifest["row_shards"]) == 1
    archive = tmp_path / "archive"
    meta = export_feeds(source, archive, date(2026, 11, 4), "run")
    assert meta["technical_data"]["version"] == 2
    assert "state/apif_fixture_index.jsonl" in meta["files"]
    assert not any(
        f"state/{t}.jsonl" in meta["files"]
        for t in ("match_stats", "match_events", "match_lineups")
    )
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert rows(restored) == [] and release.downloads == []
    again = export_feeds(restored, tmp_path / "again", date(2026, 11, 4), "again")
    assert again["technical_data"] == meta["technical_data"]
    result = restore_rows(restored, archive, release=release)
    assert result["match_stats"] == 3 and rows(restored) == rows(source)
    before = restored.read_bytes()
    assert restore_rows(restored, None, release=release)["match_stats"] == 0
    assert restored.read_bytes() == before


@pytest.mark.parametrize("damage", ["sha256", "count", "identity", "table", "version"])
def test_bad_shard_preserves_existing_database(tmp_path, damage):
    import zstandard

    from zucai.stats_rows import restore_rows

    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source, versions=3)
    release = LocalRelease(tmp_path / "release")
    manifest = publish(source, tmp_path / "out", release)
    shard = manifest["row_shards"][0]
    asset = release.root / shard["tag"] / shard["name"]
    if damage == "count":
        shard["rows"] += 1
    elif damage in {"identity", "table", "version"}:
        lines = [
            json.loads(line)
            for line in zstandard.ZstdDecompressor()
            .stream_reader(io.BytesIO(asset.read_bytes()))
            .read()
            .splitlines()
        ]
        if damage == "identity":
            lines[0]["row"][lines[0]["columns"].index("team_id")] = 2
        elif damage == "table":
            lines[0]["table"] = "matches"
        else:
            lines[0]["row"][lines[0]["columns"].index("version")] = 0
        content = b"".join((json.dumps(line) + "\n").encode() for line in lines)
        asset.write_bytes(zstandard.ZstdCompressor(write_content_size=True).compress(content))
        shard.update(
            sha256=hashlib.sha256(asset.read_bytes()).hexdigest(), size=asset.stat().st_size
        )
    else:
        asset.write_bytes(b"corrupt")
    before = source.read_bytes()
    state = source.parent / "state/stats_rows_manifest.json"
    state.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        restore_rows(source, None, release=release)
    assert source.read_bytes() == before


def test_incomplete_previous_snapshot_cannot_be_republished(tmp_path):
    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source, versions=3)
    release = LocalRelease(tmp_path / "release")
    original = publish(source, tmp_path / "out", release)
    with sqlite3.connect(source) as db:
        db.execute("DELETE FROM match_stats WHERE version=1")
    with pytest.raises(ValueError, match="历史|snapshot|恢复"):
        publish(source, tmp_path / "out2", release, previous=original, run_id="two")


@pytest.mark.parametrize("damage", [None, "changed_row", "missing_row", "cached_bytes"])
def test_restore_publish_reuses_downloads_without_weakening_history_checks(tmp_path, damage):
    from zucai.stats_rows import restore_rows

    source = tmp_path / "feeds.db"
    seed(source, versions=3)
    release = LocalRelease(tmp_path / "release")
    previous = publish(source, tmp_path / "out", release)
    cache = tmp_path / "batch-cache"
    restore_rows(source, release=release, shard_cache=cache)
    downloads = [(s["tag"], s["name"]) for s in previous["row_shards"]]
    assert release.downloads == downloads
    if damage == "cached_bytes":
        next(cache.glob("*.jsonl.zst")).write_bytes(b"corrupt")
    elif damage:
        with sqlite3.connect(source) as db:
            if damage == "changed_row":
                db.execute("UPDATE match_stats SET value=10 WHERE version=1")
            else:
                db.execute("DELETE FROM match_stats WHERE version=1")
    before = rows(source)
    if damage:
        with pytest.raises(ValueError, match="SHA256|snapshot|冲突"):
            publish(source, tmp_path / "out2", release, run_id="two", shard_cache=cache)
        assert len(list((release.root / "stats-raw-202611").iterdir())) == 2
    else:
        current = publish(source, tmp_path / "out2", release, run_id="two", shard_cache=cache)
        assert current["snapshot"] == previous["snapshot"]
    assert release.downloads == downloads
    assert rows(source) == before


def test_publish_failure_does_not_mark_rows_published(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)

    class Failure:
        def upload(self, tag, path):
            raise OSError("synthetic upload failure")

    with pytest.raises(OSError, match="synthetic"):
        publish(source, tmp_path / "out", Failure())
    assert not (source.parent / "state/stats_rows_manifest.json").exists()


def test_two_daily_cycles_keep_old_versions_raw_assets_and_ids(tmp_path):
    from zucai.stats_rows import restore_rows

    source = tmp_path / "source/feeds.db"
    source.parent.mkdir()
    seed(source, versions=3)
    release = LocalRelease(tmp_path / "release")
    previous = publish(source, tmp_path / "out", release)
    raw = dict(tag="stats-raw-202610", name="stats-raw-earlier.zip", sha256="a" * 64, size=100)
    previous["raw_archives"] = [raw]
    expected = rows(source)
    for day in (1, 2):
        archive = tmp_path / f"archive{day}"
        export_feeds(source, archive, date(2026, 11, 4), str(day))
        restored = tmp_path / f"daily{day}/feeds.db"
        restore_feeds(archive, restored)
        restore_rows(restored, None, release=release)
        assert rows(restored) == expected
        # New ingestion receives a fresh ID after the original maximum, preserving versions.
        with sqlite3.connect(restored) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(match_stats)")]
            old = list(db.execute("SELECT * FROM match_stats ORDER BY id DESC LIMIT 1").fetchone())
            old[columns.index("id")] += 1
            old[columns.index("version")] += 1
            old[columns.index("raw_sha256")] = str(day + 3) * 64
            db.execute(f"INSERT INTO match_stats VALUES ({','.join('?' for _ in columns)})", old)
        previous = publish(
            restored, tmp_path / f"out{day}", release, previous=previous, run_id=f"daily-{day}"
        )
        assert previous["raw_archives"] == [raw]
        assert rows(restored)[: len(expected)] == expected
        expected, source = rows(restored), restored
    assert len(expected) == 5


def test_export_rejects_modified_published_rows(tmp_path):
    source = tmp_path / "feeds.db"
    seed(source)
    release = LocalRelease(tmp_path / "release")
    publish(source, tmp_path / "out", release)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE match_stats SET value=10")
    with pytest.raises(ValueError, match="publish_rows|发布"):
        export_feeds(source, tmp_path / "archive", date(2026, 11, 4), "changed")


def test_changed_positive_version_is_rejected_by_snapshot_hash(tmp_path):
    import io

    import zstandard

    from zucai.stats_rows import restore_rows

    source = tmp_path / "feeds.db"
    seed(source)
    release = LocalRelease(tmp_path / "release")
    block = publish(source, tmp_path / "out", release)
    shard = block["row_shards"][0]
    asset = release.root / shard["tag"] / shard["name"]
    entry = json.loads(
        zstandard.ZstdDecompressor().stream_reader(io.BytesIO(asset.read_bytes())).read()
    )
    entry["row"][entry["columns"].index("version")] = 99
    asset.write_bytes(zstandard.ZstdCompressor().compress((json.dumps(entry) + "\n").encode()))
    shard.update(size=asset.stat().st_size, sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
    state = source.parent / "state/stats_rows_manifest.json"
    state.write_text(json.dumps(block))
    with sqlite3.connect(source) as db:
        db.execute("DELETE FROM match_stats")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="snapshot|哈希"):
        restore_rows(source, None, release=release)
    assert source.read_bytes() == before


@pytest.mark.parametrize("cached", [False, True])
def test_only_approved_den_haag_identity_redirect_preserves_history(tmp_path, cached):
    from zucai.stats_rows import restore_rows

    source = tmp_path / "feeds.db"
    seed(source, versions=3)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE teams SET name='Den Haag' WHERE id=1")
    release = LocalRelease(tmp_path / "release")
    previous = publish(source, tmp_path / "out", release)
    options = {"shard_cache": tmp_path / "batch-cache"} if cached else {}
    if cached:
        restore_rows(source, release=release, **options)
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO teams(id,name,aliases) VALUES(3,'海牙','[]')")
        db.execute("UPDATE matches SET home_team_id=3")
        db.execute("UPDATE match_stats SET team_id=3")
        db.execute("DELETE FROM teams WHERE id=1")
    previous["identity_redirects"] = [
        {
            "old_team_id": 1,
            "new_team_id": 3,
            "approval_url": "https://github.com/qw5563680/FOOTBALL/pull/49#issuecomment-6627072639",
        }
    ]
    with pytest.raises(ValueError, match="冲突|snapshot"):
        publish(source, tmp_path / "unapproved", release, run_id="unapproved", **options)
    current = publish(
        source, tmp_path / "approved", release, previous=previous, run_id="approved", **options
    )
    assert current["counts"]["match_stats"] == 3
    assert current["identity_redirects"] == previous["identity_redirects"]
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT id,version,team_id FROM match_stats ORDER BY id").fetchall() == [
            (1, 1, 3),
            (2, 2, 3),
            (3, 3, 3),
        ]
    previous["identity_redirects"][0]["approval_url"] = "https://github.com/other/approval"
    with pytest.raises(ValueError, match="批准|approval"):
        publish(
            source,
            tmp_path / "wrongapproval",
            release,
            previous=previous,
            run_id="wrongapproval",
            **options,
        )


def test_initial_publish_accepts_new_raw_archive_without_previous_manifest(tmp_path):
    from zucai.stats_release import publish_raw

    source = tmp_path / "feeds.db"
    seed(source)
    release = LocalRelease(tmp_path / "release")
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "original.json").write_bytes(b'{"raw":3}')
    raw = publish_raw(
        raw_root,
        tmp_path / "raw-assets",
        release=release,
        tag="stats-raw-202611",
        run_id="first",
    )
    assert not (source.parent / "state/stats_rows_manifest.json").exists()

    first = publish(source, tmp_path / "first", release, previous=None, raw_archives=[raw])

    assert first["raw_archives"] == [raw]
    persisted = json.loads((source.parent / "state/stats_rows_manifest.json").read_text())
    assert persisted["raw_archives"] == [raw]
    second = publish(source, tmp_path / "second", release, run_id="second")
    assert second["raw_archives"] == [raw]
