"""Restore pinned feed tables and compare original T04 builder without fetching sports sites."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from zucai.cloud_feeds import archive_info, restore_feeds
from zucai.db import get_engine

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ARCHIVE = HERE / "archive"
DATABASE = HERE / "restored" / "zucai.db"
AS_OF = "2026-11-01T16:25:56.611980Z"
COMMIT = "ddeb76ff75fb23da736745c354db946c24b87107"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = archive_info(ARCHIVE)
    if DATABASE.exists():
        raise FileExistsError(DATABASE)
    restore_feeds(ARCHIVE, DATABASE)
    before = sha256(DATABASE)
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as database:
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("teams", "matches", "results", "odds", "predictions")
        }
        statuses = dict(database.execute("SELECT status,COUNT(*) FROM matches GROUP BY status"))
        result_basis = dict(
            database.execute("SELECT available_source,COUNT(*) FROM results GROUP BY available_source")
        )
        kickoff_range = database.execute("SELECT MIN(kickoff_utc),MAX(kickoff_utc) FROM matches").fetchone()
        result_range = database.execute("SELECT MIN(available_utc),MAX(available_utc) FROM results").fetchone()
        source_counts = dict(database.execute("SELECT source,COUNT(*) FROM matches GROUP BY source"))
    spec = importlib.util.spec_from_file_location("original_t04_builder", HERE / "build_v1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = get_engine(DATABASE)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            with Session(bind=connection) as session:
                frame = module.build_features(session, as_of=AS_OF)
    finally:
        engine.dispose()
    assert sha256(DATABASE) == before
    frame.to_parquet(HERE / "restored-v1.parquet", index=False)
    (HERE / "restored-v1.attrs.json").write_text(
        json.dumps(frame.attrs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    original = pd.read_parquet(ROOT / "reports/2026-11-02-t04-acceptance/features.parquet")
    old = original.set_index("meta_match_id").sort_index()
    restored = frame.set_index("meta_match_id").sort_index()
    assert list(old.index) == list(restored.index)
    differences = {}
    for column in old.columns:
        if column == "meta_generated_utc":
            continue
        equal = old[column].eq(restored[column]) | (old[column].isna() & restored[column].isna())
        if not equal.all():
            differences[column] = list(old.index[~equal])
    metrics = {}
    for name, data in (("frozen_t04", original), ("restored_v1", frame)):
        metrics[name] = {
            "rows": len(data),
            "home_rest_nonnull": int(data.home_rest_days.notna().sum()),
            "away_rest_nonnull": int(data.away_rest_days.notna().sum()),
            "both_rest_nonnull": int(data[["home_rest_days", "away_rest_days"]].notna().all(axis=1).sum()),
            "had_nonnull": int(data.market_p_home.notna().sum()),
            "rows_with_any_model_feature": int(data[list(module.MODEL_FEATURE_COLUMNS)].notna().any(axis=1).sum()),
        }
    summary = {
        "archive_commit": COMMIT,
        "archive_manifest_sha256": sha256(ARCHIVE / "feed_manifest.json"),
        "archive_files_sha256": manifest["files"],
        "archive_counts": manifest["counts"],
        "database_path": str(DATABASE),
        "database_sha256_before_and_after": before,
        "database_counts": counts,
        "match_source_counts": source_counts,
        "statuses": statuses,
        "kickoff_range": kickoff_range,
        "result_availability_range": result_range,
        "result_basis": result_basis,
        "as_of_utc": AS_OF,
        "metrics": metrics,
        "columns_differing_from_frozen_t04": differences,
        "limitation": "Original T04 database is unavailable. Pinned archive has 2319 odds versus original 3450; third capture at 2026-11-01T14:27:52.953051Z is absent. No quotes synthesized.",
    }
    (HERE / "input-provenance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
