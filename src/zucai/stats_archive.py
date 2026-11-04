"""Versioned, additive manifest extension for technical observations (not features)."""

import json
import math
import re
from datetime import datetime, timedelta

from sqlalchemy import JSON

from zucai.stats_schema import STATS_COLUMNS, STATS_MODELS

STATS_FILES = {f"state/{name}.jsonl" for name in STATS_COLUMNS}


def check_stats(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = tables & STATS_COLUMNS.keys()
    if not present:
        return False  # Legacy DB, read without migrating or changing it.
    if present != STATS_COLUMNS.keys():
        raise ValueError("技术数据表不完整")
    for model in STATS_MODELS:
        table = model.__tablename__
        columns = STATS_COLUMNS[table]
        for values in db.execute(f"SELECT {','.join(columns)} FROM {table}"):
            row = dict(zip(columns, values, strict=True))
            for key in ("captured_utc", "available_utc", "kickoff_utc"):
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
    for table in ("match_stats", "match_events", "match_lineups"):
        if db.execute(f"""SELECT 1 FROM {table} t JOIN matches m USING(match_id)
            WHERE t.team_id <> CASE t.side WHEN 'home' THEN m.home_team_id ELSE m.away_team_id END
            OR (t.provider='fd' AND m.source<>'fd')
            OR (t.provider='apif' AND NOT EXISTS (
                SELECT 1 FROM apif_fixture_index i WHERE i.provider_fixture_id=t.provider_fixture_id
                AND i.match_id=t.match_id AND t.provider_team_id=CASE t.side
                    WHEN 'home' THEN i.home_provider_id ELSE i.away_provider_id END))
            LIMIT 1""").fetchone():
            raise ValueError(f"技术数据 {table} 身份或主客校验失败")
    if db.execute("""SELECT 1 FROM apif_fixture_index i JOIN matches m USING(match_id)
        WHERE i.home_team_id<>m.home_team_id OR i.away_team_id<>m.away_team_id
        OR i.league<>m.league OR abs(julianday(i.kickoff_utc)-julianday(m.kickoff_utc))>0.125
        LIMIT 1""").fetchone():
        raise ValueError("技术数据索引身份或开球时间校验失败")
    return True


def export_stats(db, archive):
    present = check_stats(db)
    counts = {}
    for table, columns in STATS_COLUMNS.items():
        rows = db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1") if present else []
        key = f"state/{table}.jsonl"
        target = archive / key
        target.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with target.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(
                    json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                count += 1
        counts[table] = count
    return {
        "version": 1,
        "columns": {k: list(v) for k, v in STATS_COLUMNS.items()},
        "counts": counts,
    }


def validate_stats_manifest(meta):
    block = meta.get("technical_data")
    present = STATS_FILES.intersection(meta["files"])
    if block is None and not present:
        return  # Archives preceding T31 remain readable.
    if (
        not isinstance(block, dict)
        or block.get("version") != 1
        or present != STATS_FILES
        or block.get("columns") != {k: list(v) for k, v in STATS_COLUMNS.items()}
        or set(block.get("counts", {})) != STATS_COLUMNS.keys()
        or any(type(v) is not int or v < 0 for v in block["counts"].values())
    ):
        raise ValueError("技术数据 manifest 字段、文件或行数清单不完整")


def restore_stats(db, archive, meta):
    validate_stats_manifest(meta)
    if "technical_data" not in meta:
        return
    for table, columns in STATS_COLUMNS.items():
        with (archive / f"state/{table}.jsonl").open(encoding="utf-8") as stream:
            db.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                (json.loads(line) for line in stream),
            )
        if (
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            != meta["technical_data"]["counts"][table]
        ):
            raise ValueError(f"技术数据 {table} 行数校验失败")
    check_stats(db)
