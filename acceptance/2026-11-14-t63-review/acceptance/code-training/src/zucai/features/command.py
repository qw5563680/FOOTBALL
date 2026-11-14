"""Read-only feature extraction and exclusive, hashed Parquet exports."""

import hashlib
import json
import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.features.build import ALL_COLUMNS, MARKET_COLUMNS, MODEL_FEATURE_COLUMNS, build_features

LOG = logging.getLogger(__name__)


def _json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_features(frame, output):
    """Preserve original files, including a competitor's file created during export."""
    output = Path(output)
    if output.suffix.lower() != ".parquet":
        raise ValueError("特征输出须使用.parquet扩展名")
    metadata_path = output.with_suffix(".metadata.json")
    manifest_path = output.with_suffix(".manifest.json")
    for path in (output, metadata_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"输出已存在，须使用新文件：{path}")
    if tuple(frame.columns) != ALL_COLUMNS or frame.attrs.get("model_feature_columns") != list(
        MODEL_FEATURE_COLUMNS
    ):
        raise ValueError("特征表schema或模型字段白名单不符")
    count = len(frame)
    metadata = dict(
        frame.attrs,
        rows=count,
        columns=list(frame.columns),
        model_missing_rates={
            c: float(frame[c].isna().mean()) if count else None for c in MODEL_FEATURE_COLUMNS
        },
        market_missing_rates={
            c: float(frame[c].isna().mean()) if count else None for c in MARKET_COLUMNS
        },
        rows_with_complete_model_features=int(
            frame[list(MODEL_FEATURE_COLUMNS)].notna().all(axis=1).sum()
        ),
    )
    # Serialization finishes before any destination is created.
    parquet = frame.to_parquet(index=False, engine="pyarrow")
    payloads = {output: parquet, metadata_path: _json_bytes(metadata)}
    manifest = dict(
        schema_version=1,
        algorithm="sha256",
        files={p.name: hashlib.sha256(raw).hexdigest() for p, raw in payloads.items()},
    )
    payloads[manifest_path] = _json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, raw in payloads.items():
            with path.open("xb") as stream:
                created.append(path)
                stream.write(raw)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return manifest


def run_features(args):
    database = Path(args.db or os.environ.get("ZUCAI_DB", "data/zucai.db")).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("输入数据库不存在，请先恢复已有归档或采集数据")
    engine = get_engine(database)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only=ON")
            # sqlite3 legacy transaction mode does not BEGIN for SELECT itself.
            # Hold one read snapshot across target/history/quote queries.
            conn.exec_driver_sql("BEGIN")
            with Session(bind=conn) as session:
                frame = build_features(
                    session, args.match_id, args.as_of, strict_availability=args.strict_availability
                )
        manifest = write_features(frame, args.out)
    finally:
        engine.dispose()
    LOG.info("已冻结%d场赛前特征：%s", len(frame), args.out)
    LOG.info("球队模型字段（白名单）：%s", ", ".join(MODEL_FEATURE_COLUMNS))
    LOG.info("市场对比字段（不入模型）：%s", ", ".join(MARKET_COLUMNS))
    return manifest
