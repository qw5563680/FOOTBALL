"""Versioned, additive manifest extension for technical observations (not features)."""

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import JSON

from zucai.sources.base import _atomic_write
from zucai.stats_release import validate_name, validate_tag
from zucai.stats_schema import STATS_COLUMNS, STATS_MODELS

LEGACY_ROW_TABLES = ("match_stats", "match_events", "match_lineups")
ROW_TABLES = (*LEGACY_ROW_TABLES, "match_injuries")
INDEX_FILE = "state/apif_fixture_index.jsonl"
ROWS_MANIFEST = "state/stats_rows_manifest.json"
STATS_FILES = {f"state/{name}.jsonl" for name in STATS_COLUMNS}


def manifest_columns(block):
    """T31/T32 receipts stay valid byte-for-byte; T38 adds a versioned table."""
    return {
        k: v for k, v in STATS_COLUMNS.items() if block.get("version") == 3 or k != "match_injuries"
    }


def row_snapshot(db, tables=ROW_TABLES):
    result = {"counts": {}, "max_ids": {}, "sha256": {}}
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        digest = hashlib.sha256()
        count, last = 0, None
        values = (
            db.execute(f"SELECT {','.join(STATS_COLUMNS[table])} FROM {table} ORDER BY id")
            if table in present
            else ()
        )
        for row in values:
            digest.update(
                (json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            )
            count, last = count + 1, row[STATS_COLUMNS[table].index("id")]
        result["counts"][table], result["max_ids"][table] = count, last
        result["sha256"][table] = digest.hexdigest()
    return result


def write_rows_manifest(database, block):
    path = Path(database).parent / ROWS_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(block, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if not path.exists() or path.read_bytes() != content:
        _atomic_write(path, content)


def read_rows_manifest(database):
    path = Path(database).parent / ROWS_MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def check_stats(db, *, rows=True):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = tables & STATS_COLUMNS.keys()
    if not present:
        return False  # Legacy DB, read without migrating or changing it.
    if present not in (STATS_COLUMNS.keys(), STATS_COLUMNS.keys() - {"match_injuries"}):
        raise ValueError("技术数据表不完整")
    for model in STATS_MODELS if rows else STATS_MODELS[:1]:
        table = model.__tablename__
        if table not in present:
            continue
        columns = STATS_COLUMNS[table]
        for values in db.execute(f"SELECT {','.join(columns)} FROM {table}"):
            row = dict(zip(columns, values, strict=True))
            for key in ("captured_utc", "available_utc", "kickoff_utc", "fixture_kickoff_utc"):
                value = row.get(key)
                if value is not None:
                    try:
                        stamp = datetime.fromisoformat(value)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("技术数据时间无效") from exc
                    if stamp.utcoffset() != timedelta(0):
                        raise ValueError("技术数据时间必须为明确的 UTC")
            if not re.fullmatch(r"[0-9a-f]{64}", row["raw_sha256"]):
                raise ValueError("技术数据原件哈希无效")
            if not row["raw_location"] or not row["parser_version"]:
                raise ValueError("技术数据原件位置或解析版本缺失")
            for column in model.__table__.columns:
                if isinstance(column.type, JSON):
                    try:
                        # SQLite gives JSON columns NUMERIC affinity: unquoted JSON
                        # numbers can come back as int/float, unlike objects/strings.
                        raw = row[column.name]
                        value = raw if type(raw) in (int, float) else json.loads(raw)
                        if isinstance(value, float) and not math.isfinite(value):
                            raise ValueError("JSON 数值非有限值")
                    except (ValueError, TypeError) as exc:
                        raise ValueError("技术数据 JSON 字段无效") from exc
            if (
                table == "match_stats"
                and row["value"] is not None
                and not math.isfinite(row["value"])
            ):
                raise ValueError("技术数据数值非有限值")
    for table in LEGACY_ROW_TABLES if rows else ():
        if db.execute(f"""SELECT 1 FROM {table} t JOIN matches m USING(match_id)
            WHERE t.team_id <> CASE t.side WHEN 'home' THEN m.home_team_id ELSE m.away_team_id END
            OR (t.provider='fd' AND NOT (
                (m.source='fd' AND t.provider_fixture_id=t.match_id)
                OR (m.source='jc' AND COALESCE(m.counterpart_id,'')=t.provider_fixture_id)))
            OR (t.provider='apif' AND NOT EXISTS (
                SELECT 1 FROM apif_fixture_index i WHERE i.provider_fixture_id=t.provider_fixture_id
                AND i.match_id=t.match_id AND t.provider_team_id=CASE t.side
                    WHEN 'home' THEN i.home_provider_id ELSE i.away_provider_id END))
            LIMIT 1""").fetchone():
            raise ValueError(f"技术数据 {table} 身份或主客校验失败")
    if rows:
        for table in LEGACY_ROW_TABLES:
            if db.execute(
                f"SELECT 1 FROM {table} t LEFT JOIN matches m USING(match_id) "
                "LEFT JOIN teams k ON k.id=t.team_id WHERE m.match_id IS NULL OR k.id IS NULL "
                "OR t.version<1 LIMIT 1"
            ).fetchone():
                raise ValueError(f"技术数据 {table} 身份、版本或外键校验失败")
        if "match_injuries" in present:
            check_injuries(db)
    if db.execute("""SELECT 1 FROM apif_fixture_index i JOIN matches m USING(match_id)
        WHERE i.home_team_id<>m.home_team_id OR i.away_team_id<>m.away_team_id
        OR i.league<>m.league OR abs(julianday(i.kickoff_utc)-julianday(m.kickoff_utc))>0.125
        LIMIT 1""").fetchone():
        raise ValueError("技术数据索引身份或开球时间校验失败")
    return True


def check_injuries(db):
    """Unlinked injuries are retained; partial or inconsistent links are rejected."""
    if db.execute("""SELECT 1 FROM match_injuries r
        LEFT JOIN apif_fixture_index i ON i.provider_fixture_id=r.provider_fixture_id
        LEFT JOIN matches m ON m.match_id=r.match_id
        WHERE r.version < 1 OR r.provider <> 'apif'
        OR r.captured_day <> substr(r.captured_utc,1,10)
        OR (r.match_id IS NULL) <> (r.team_id IS NULL)
        OR (r.match_id IS NOT NULL AND (i.provider_fixture_id IS NULL OR m.match_id IS NULL
            OR r.provider_team_id IS NULL OR r.side IS NULL
            OR r.match_id<>i.match_id OR r.league<>i.league OR r.provider_season<>i.season
            OR abs(julianday(r.fixture_kickoff_utc)-julianday(i.kickoff_utc))>0.125
            OR NOT ((r.side='home' AND r.provider_team_id=i.home_provider_id
                    AND r.team_id=i.home_team_id)
                OR (r.side='away' AND r.provider_team_id=i.away_provider_id
                    AND r.team_id=i.away_team_id))))
        OR r.captured_before_kickoff <> (julianday(r.captured_utc)<julianday(r.fixture_kickoff_utc))
        OR r.availability_basis NOT IN ('captured','backfilled')
        OR (r.availability_basis='backfilled' AND r.available_utc IS NOT NULL)
        OR (r.availability_basis='captured' AND (r.available_utc IS NULL
            OR r.available_utc<>r.captured_utc
            OR julianday(r.captured_utc)>=julianday(r.fixture_kickoff_utc)))
        LIMIT 1""").fetchone():
        raise ValueError("伤停身份、版本或可知时间校验失败")


def export_stats(db, archive, source=None):
    present = check_stats(db, rows=False)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    block = read_rows_manifest(source) if source is not None else None
    counts = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if present and table in tables
        else 0
        for table in STATS_COLUMNS
    }
    if any(counts[t] for t in ROW_TABLES):
        check_stats(db)
        snapshot = block.get("snapshot") if block else None
        if (
            snapshot is None
            or snapshot != row_snapshot(db, snapshot["counts"])
            or (counts["match_injuries"] and "match_injuries" not in snapshot["counts"])
        ):
            raise ValueError(
                "技术数据行尚未发布；先 restore_rows 恢复全部历史行，"
                "再 publish_rows 发布 Release 分片"
            )
    if block is None:
        block = {
            "version": 3,
            "columns": {k: list(v) for k, v in STATS_COLUMNS.items()},
            "counts": counts,
            "row_shards": [],
            "raw_archives": [],
        }
    else:
        block = copy.deepcopy(block)
        validate_stats_manifest({"technical_data": block, "files": {INDEX_FILE: ""}})
        block["counts"]["apif_fixture_index"] = counts["apif_fixture_index"]
    target = archive / INDEX_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        if present:
            columns = STATS_COLUMNS["apif_fixture_index"]
            for row in db.execute(f"SELECT {','.join(columns)} FROM apif_fixture_index ORDER BY 1"):
                stream.write(
                    json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
                )
    # Keep a hash-listed descriptor copy alongside the feed, even for an empty DB.
    write_rows_manifest(archive / "placeholder.db", block)
    return block


def validate_asset(asset):
    if not isinstance(asset, dict):
        raise ValueError("技术数据 Release 描述无效")
    validate_tag(asset.get("tag"))
    validate_name(asset.get("name"))
    if (
        not isinstance(asset.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
        or type(asset.get("size")) is not int
        or asset["size"] < 0
    ):
        raise ValueError("技术数据 Release 哈希或大小无效")


def validate_stats_manifest(meta):
    block = meta.get("technical_data")
    present = STATS_FILES.intersection(meta["files"])
    if block is None and not present:
        return
    if (
        not isinstance(block, dict)
        or block.get("version") not in (1, 2, 3)
        or block.get("columns") != {k: list(v) for k, v in manifest_columns(block).items()}
        or set(block.get("counts", {})) != manifest_columns(block).keys()
        or any(type(v) is not int or v < 0 for v in block["counts"].values())
    ):
        raise ValueError("技术数据 manifest 字段、文件或行数清单不完整")
    if block["version"] == 1:
        if present != {f"state/{t}.jsonl" for t in manifest_columns(block)}:
            raise ValueError("技术数据 manifest 文件不完整")
        return
    if (
        present != {INDEX_FILE}
        or not isinstance(block.get("row_shards"), list)
        or not isinstance(block.get("raw_archives"), list)
    ):
        raise ValueError("技术数据外部分片文件清单不完整")
    row_tables = tuple(t for t in manifest_columns(block) if t != "apif_fixture_index")
    totals = dict.fromkeys(row_tables, 0)
    seen = set()
    for shard in block["row_shards"]:
        validate_asset(shard)
        key = (shard["tag"], shard["name"])
        if key in seen:
            raise ValueError("技术数据分片重复")
        seen.add(key)
        if (
            not isinstance(shard.get("league"), str)
            or not shard["league"]
            or not isinstance(shard.get("provider_season"), str)
            or not re.fullmatch(r"[0-9]{4}", shard["provider_season"])
            or set(shard.get("counts", {})) != set(row_tables)
            or any(type(n) is not int or n < 0 for n in shard["counts"].values())
            or type(shard.get("rows")) is not int
            or shard["rows"] != sum(shard["counts"].values())
        ):
            raise ValueError("技术数据分片范围或行数无效")
        for table in row_tables:
            totals[table] += shard["counts"][table]
    if any(totals[t] != block["counts"][t] for t in row_tables):
        raise ValueError("技术数据分片行数清单不符")
    for asset in block["raw_archives"]:
        validate_asset(asset)
        if "checksum_asset" in asset:
            validate_asset(asset["checksum_asset"])
    if "checksum_asset" in block and (block["checksum_asset"] is not None or block["row_shards"]):
        validate_asset(block["checksum_asset"])


def restore_stats(db, archive, meta):
    validate_stats_manifest(meta)
    if "technical_data" not in meta:
        return
    block = meta["technical_data"]
    if block["version"] in (2, 3):
        if (
            ROWS_MANIFEST not in meta["files"]
            or json.loads((archive / ROWS_MANIFEST).read_text(encoding="utf-8")) != block
        ):
            raise ValueError("技术数据外部分片持久清单与 manifest 不符")
    tables = manifest_columns(block) if block["version"] == 1 else ("apif_fixture_index",)
    for table in tables:
        columns = STATS_COLUMNS[table]
        with (archive / f"state/{table}.jsonl").open(encoding="utf-8") as stream:
            db.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                (json.loads(line) for line in stream),
            )
        if db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != block["counts"][table]:
            raise ValueError(f"技术数据 {table} 行数校验失败")
    check_stats(db, rows=block["version"] == 1)
