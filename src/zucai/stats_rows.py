"""Stream immutable compressed row snapshots, then restore atomically with original IDs."""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import zstandard

from zucai.injuries_archive import install_receipts, prepare_receipts, publish_receipts
from zucai.stats_archive import (
    INDEX_FILE,
    ROW_TABLES,
    check_stats,
    read_rows_manifest,
    row_snapshot,
    validate_stats_manifest,
    write_rows_manifest,
)
from zucai.stats_identity import approved_fd_merges
from zucai.stats_release import (
    sha256,
    upload_checksums,
    upload_verified,
    validate_name,
    validate_tag,
)
from zucai.stats_schema import STATS_COLUMNS


def _manifest(database, archive):
    if archive is None:
        block = read_rows_manifest(database)
    else:
        path = Path(archive)
        if path.is_dir():
            path = path / "feed_manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        block = document.get("technical_data", document)
    if block is None:
        raise ValueError("缺少技术数据分片清单；请先恢复 feeds 归档")
    if block.get("version") not in (2, 3):
        raise ValueError("restore-rows 需要 technical_data version 2/3 外部分片清单")
    validate_stats_manifest({"technical_data": block, "files": {INDEX_FILE: ""}})
    return block


def _fd_seasons(database):
    result = {}
    path = Path(database).parent / "state/audit/stats_ingest_audit.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("endpoint") == "csv" and re.fullmatch(
                    r"[0-9]{4}", row.get("season", "")
                ):
                    result[row["raw_sha256"]] = row["season"]
    return result


def _scope(db, table, row, fd_seasons, cache):
    values = dict(zip(STATS_COLUMNS[table], row, strict=True))
    if table == "match_injuries":
        # Unlinked injury rows still belong to their explicit provider scope.
        return values["league"], str(values["provider_season"])
    key = (
        values["match_id"],
        values["provider"],
        values["provider_fixture_id"],
        values["raw_sha256"],
    )
    if key not in cache:
        match = db.execute(
            "SELECT league,kickoff_utc FROM matches WHERE match_id=?", (values["match_id"],)
        ).fetchone()
        if match is None:
            raise ValueError("技术数据分片比赛身份不存在")
        if values["provider"] == "apif":
            index = db.execute(
                "SELECT season FROM apif_fixture_index WHERE provider_fixture_id=? AND match_id=?",
                (values["provider_fixture_id"], values["match_id"]),
            ).fetchone()
            if index is None:
                raise ValueError("技术数据分片索引身份不存在")
            season = str(index[0])
        elif values["provider"] == "fd":
            season = fd_seasons.get(values["raw_sha256"])
            if season is None:
                # Legacy T31 rows predate receipts. The nine fd main leagues use July-year seasons.
                year = int(match[1][:4]) - (int(match[1][5:7]) < 7)
                season = f"{year % 100:02d}{(year + 1) % 100:02d}"
        else:
            raise ValueError("技术数据分片供应商身份无效")
        cache[key] = (match[0], season)
    return cache[key]


def _download_shards(block, directory, release, tag=None, *, shard_cache=None):
    prepared = []
    cache = Path(shard_cache) if shard_cache is not None else None
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
    for number, shard in enumerate(block["row_shards"]):
        if tag is not None and shard["tag"] != tag:
            continue
        compressed = (
            cache / f"{shard['sha256']}.jsonl.zst"
            if cache is not None
            else directory / f"{number}.jsonl.zst"
        )
        if cache is None or not compressed.exists():
            release.download(shard["tag"], shard["name"], compressed)
        # Batch-local cached bytes save the second download, not integrity checks.
        if compressed.stat().st_size != shard["size"] or sha256(compressed) != shard["sha256"]:
            raise ValueError("技术数据分片 SHA256 或大小校验失败")
        plain = directory / f"{number}.jsonl"
        counts = dict.fromkeys(shard["counts"], 0)
        try:
            with (
                compressed.open("rb") as source,
                zstandard.ZstdDecompressor().stream_reader(source) as reader,
                io.TextIOWrapper(reader, encoding="utf-8") as stream,
                plain.open("w", encoding="utf-8") as target,
            ):
                for line in stream:
                    entry = json.loads(line)
                    table = entry.get("table")
                    if (
                        table not in counts
                        or entry.get("columns") != list(STATS_COLUMNS[table])
                        or not isinstance(entry.get("row"), list)
                        or len(entry["row"]) != len(STATS_COLUMNS[table])
                    ):
                        raise ValueError("技术数据分片表、字段或行格式无效")
                    values = dict(zip(STATS_COLUMNS[table], entry["row"], strict=True))
                    if (
                        type(values["id"]) is not int
                        or values["id"] < 1
                        or type(values["version"]) is not int
                        or values["version"] < 1
                    ):
                        raise ValueError("技术数据分片行 ID 或版本无效")
                    if any(isinstance(value, (dict, list)) for value in entry["row"]):
                        raise ValueError("技术数据分片必须保留 SQL 原值")
                    counts[table] += 1
                    target.write(line)
        except (
            zstandard.ZstdError,
            UnicodeError,
            json.JSONDecodeError,
            AttributeError,
            TypeError,
        ) as exc:
            raise ValueError("技术数据分片解压或 JSON 校验失败") from exc
        if counts != shard["counts"] or sum(counts.values()) != shard["rows"]:
            raise ValueError("技术数据分片行数校验失败")
        prepared.append((shard, plain))
    if tag is not None and not prepared:
        raise ValueError(f"技术数据清单没有 Release {tag}")
    return prepared


def _identity_redirects(db, previous):
    """Validate every redirect against the formal owner-approved fd merge list."""
    redirects = previous.get("identity_redirects", [])
    if not isinstance(redirects, list):
        raise ValueError("球队身份 redirect 批准清单无效")
    result = {}
    approved = approved_fd_merges() if redirects else {}
    for item in redirects:
        if (
            not isinstance(item, dict)
            or type(item.get("old_team_id")) is not int
            or type(item.get("new_team_id")) is not int
            or item["old_team_id"] < 1
            or item["new_team_id"] < 1
            or item["old_team_id"] == item["new_team_id"]
            or item["old_team_id"] in result
        ):
            raise ValueError("球队身份 redirect 缺少有效的批准记录")
        # Existing T32 snapshots stored this one approved pair without name/scope fields.
        legacy = set(item) == {"old_team_id", "new_team_id", "approval_url"}
        league = "荷甲" if legacy else item.get("league")
        source = "Den Haag" if legacy else item.get("source_name")
        canonical = "海牙" if legacy else item.get("canonical")
        entry = approved.get((league, source))
        if (
            entry is None
            or canonical != entry["canonical"]
            or item.get("approval_url") != entry["approval_pr"]
        ):
            raise ValueError("球队身份 redirect 不在正式 fd 名称批准清单")
        old = db.execute("SELECT name FROM teams WHERE id=?", (item["old_team_id"],)).fetchone()
        new = db.execute("SELECT name FROM teams WHERE id=?", (item["new_team_id"],)).fetchone()
        if new != (canonical,) or (old is not None and old != (source,)):
            raise ValueError("球队身份 redirect 与已批准的 fd 名称对不一致")
        result[item["old_team_id"]] = item["new_team_id"]
    return result


def _apply(db, prepared, database, *, verify_only=False, redirects=None):
    inserted = dict.fromkeys(ROW_TABLES, 0)
    fd_seasons, cache, seen = _fd_seasons(database), {}, set()
    redirects = redirects or {}
    for shard, plain in prepared:
        with plain.open(encoding="utf-8") as stream:
            for line in stream:
                entry = json.loads(line)
                table, row = entry["table"], entry["row"]
                columns = STATS_COLUMNS[table]
                row_id = row[columns.index("id")]
                if (table, row_id) in seen:
                    raise ValueError("技术数据分片重复行 ID")
                seen.add((table, row_id))
                if _scope(db, table, row, fd_seasons, cache) != (
                    shard["league"],
                    shard["provider_season"],
                ):
                    raise ValueError("技术数据分片联赛、供应商赛季或身份校验失败")
                existing = db.execute(
                    f"SELECT {','.join(columns)} FROM {table} WHERE id=?", (row_id,)
                ).fetchone()
                if existing is not None:
                    expected = row.copy()
                    if verify_only:
                        team_column = columns.index("team_id")
                        expected[team_column] = redirects.get(
                            expected[team_column], expected[team_column]
                        )
                    if verify_only and table == "match_injuries":
                        last = columns.index("last_captured_utc")
                        if existing[last] >= expected[last]:
                            expected[last] = existing[last]
                    if list(existing) != expected:
                        raise ValueError(
                            "技术数据行 ID 冲突；先在仅恢复索引的数据库恢复全部历史 snapshot"
                        )
                    continue
                if verify_only:
                    raise ValueError("技术数据历史 snapshot 不完整；必须先恢复全部历史分片后再发布")
                db.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    row,
                )
                inserted[table] += 1
    return inserted


def restore_rows(database, archive=None, *, release, tag=None, shard_cache=None):
    """Hash/decompress every requested asset before staging a fully validated DB replacement."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("restore-rows 需要已有 feeds 数据库")
    if tag is not None:
        validate_tag(tag)
    block = _manifest(database, archive)
    with TemporaryDirectory(prefix="stats-restore-", dir=database.parent) as temporary:
        directory = Path(temporary)
        prepared = _download_shards(block, directory, release, tag, shard_cache=shard_cache)
        receipts = prepare_receipts(database.parent, block, directory, release)
        staged = directory / "rows.db"
        with sqlite3.connect(database) as source, sqlite3.connect(staged) as db:
            source.backup(db)
        from zucai.db import get_engine
        from zucai.stats_schema import migrate_stats

        engine = get_engine(staged)
        try:
            migrate_stats(engine)
        finally:
            engine.dispose()
        with sqlite3.connect(staged) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            result = _apply(db, prepared, database)
            check_stats(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("技术数据分片外键校验失败")
            if tag is None and any(
                db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] != block["counts"].get(t, 0)
                for t in ROW_TABLES
            ):
                raise ValueError("技术数据恢复后总行数不符；请使用只含索引的 feeds 数据库")
            if (
                tag is None
                and "snapshot" in block
                and row_snapshot(db, block["snapshot"]["counts"]) != block["snapshot"]
            ):
                raise ValueError("技术数据恢复 snapshot 哈希或版本校验失败")
            db.commit()
        install_receipts(receipts)
        write_rows_manifest(database, block)
        with sqlite3.connect(database) as source:
            migrated = not source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_injuries'"
            ).fetchone()
        if any(result.values()) or migrated:
            os.replace(staged, database)
    return result


def publish_rows(
    database, out, *, release, tag, run_id, previous=None, raw_archives=(), shard_cache=None
):
    """Publish a full-history snapshot; no prior row may disappear or change its ID."""
    database, out = Path(database), Path(out)
    if not database.is_file():
        raise ValueError("publish_rows 需要已有 feeds 数据库")
    validate_tag(tag)
    validate_name(run_id)
    previous = previous if previous is not None else read_rows_manifest(database)
    if previous is not None and "technical_data" in previous:
        previous = previous["technical_data"]
    if previous is not None:
        validate_stats_manifest({"technical_data": previous, "files": {INDEX_FILE: ""}})
    out.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(database) as db,
        TemporaryDirectory(prefix="stats-publish-", dir=out) as temporary,
    ):
        db.execute("BEGIN")
        check_stats(db)
        redirects = _identity_redirects(db, previous or {})
        if previous and previous.get("row_shards"):
            prepared = _download_shards(previous, Path(temporary), release, shard_cache=shard_cache)
            _apply(db, prepared, database, verify_only=True, redirects=redirects)
        fd_seasons, cache, groups = _fd_seasons(database), {}, {}
        # One ordered pass through each table, writing directly into each scope's compressor.
        with ExitStack() as stack:
            for table in ROW_TABLES:
                columns = STATS_COLUMNS[table]
                for row in db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY id"):
                    scope = _scope(db, table, row, fd_seasons, cache)
                    if scope not in groups:
                        compressed = Path(temporary) / f"shard-{len(groups)}.zst"
                        target = stack.enter_context(compressed.open("wb"))
                        stream = stack.enter_context(
                            zstandard.ZstdCompressor(level=3).stream_writer(target)
                        )
                        groups[scope] = (compressed, stream, dict.fromkeys(ROW_TABLES, 0))
                    compressed, stream, counts = groups[scope]
                    counts[table] += 1
                    entry = {"table": table, "columns": list(columns), "row": list(row)}
                    stream.write(
                        (
                            json.dumps(
                                entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                            )
                            + "\n"
                        ).encode()
                    )
        shards = []
        for (league, season), (compressed, _stream, counts) in sorted(groups.items()):
            slug = re.sub(r"[^a-zA-Z0-9-]", "", league) or "league"
            slug += "-" + hashlib.sha256(league.encode()).hexdigest()[:8]
            target = out / f"stats-rows-{slug}-{season}-{sha256(compressed)[:16]}.jsonl.zst"
            os.replace(compressed, target)
            asset = upload_verified(release, tag, target)
            asset.update(
                league=league, provider_season=season, rows=sum(counts.values()), counts=counts
            )
            shards.append(asset)
        receipts = publish_receipts(database.parent, previous, release, tag)
        snapshot = row_snapshot(db)
        block = {
            "version": 3,
            "columns": {k: list(v) for k, v in STATS_COLUMNS.items()},
            "counts": {
                "apif_fixture_index": db.execute(
                    "SELECT count(*) FROM apif_fixture_index"
                ).fetchone()[0],
                **snapshot["counts"],
            },
            "snapshot": snapshot,
            "row_shards": shards,
            "injury_receipts": receipts,
            "raw_archives": [*(previous or {}).get("raw_archives", []), *raw_archives],
        }
        if previous and previous.get("identity_redirects"):
            block["identity_redirects"] = previous["identity_redirects"]
        checksum = upload_checksums(release, tag, out, run_id, [*shards, *receipts])
        if checksum is not None:
            block["checksum_asset"] = checksum
        validate_stats_manifest({"technical_data": block, "files": {INDEX_FILE: ""}})
    write_rows_manifest(database, block)
    return block
