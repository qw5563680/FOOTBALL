"""One isolated code-version worker; no production files or network may be changed."""
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import responses
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def canonical(value):
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if value is None or pd.isna(value):
        return None
    return value


def encoded(value):
    return (json.dumps(canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(database):
    result = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            info = connection.execute(f"PRAGMA table_info({quote(name)})").fetchall()
            columns = [row[1] for row in info]
            names_sql = ",".join(map(quote, columns))
            header = encoded(columns)
            digest = hashlib.sha256(header)
            count, size = 0, len(header)
            for row in connection.execute(f"SELECT {names_sql} FROM {quote(name)} ORDER BY {names_sql}"):
                raw = encoded(row)
                digest.update(raw)
                count += 1
                size += len(raw)
            result[name] = {"columns": columns, "rows": count, "canonical_bytes": size, "sha256": digest.hexdigest(), "schema_sha256": hashlib.sha256(encoded(info)).hexdigest()}
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name").fetchall()
    return result, hashlib.sha256(encoded(schema)).hexdigest()


def histories(database, params):
    from zucai.predict.backtest import read_history

    configuration = json.loads(params.read_bytes())
    assert len(configuration["leagues"]) == 19
    uri = database.resolve().as_uri() + "?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    result = {}
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                for league, entry in configuration["leagues"].items():
                    frame = read_history(session, league=league, source=",".join(entry["sources"]), training_leagues=entry.get("training_leagues"))
                    header = encoded({"columns": list(frame.columns), "dtypes": [str(value) for value in frame.dtypes], "attrs": frame.attrs})
                    digest = hashlib.sha256(header)
                    size = len(header)
                    for row in frame.itertuples(index=False, name=None):
                        raw = encoded(row)
                        digest.update(raw)
                        size += len(raw)
                    result[league] = {"rows": len(frame), "columns": list(frame.columns), "sources": entry["sources"], "training_leagues": entry.get("training_leagues"), "canonical_bytes": size, "sha256": digest.hexdigest(), "params_entry_sha256": hashlib.sha256(encoded(entry)).hexdigest()}
    finally:
        engine.dispose()
    return result


def static_comparison(arguments):
    from zucai.cloud_feeds import restore_feeds

    folder = Path(arguments["folder"])
    database = folder / "data/zucai.db"
    assert not database.exists()
    before = time.perf_counter()
    with responses.RequestsMock(assert_all_requests_are_fired=False):
        restored = restore_feeds(Path(arguments["archive"]), database)
        elapsed = time.perf_counter() - before
        digest_before = file_sha(database)
        inventory, schema = tables(database)
        model_inputs = histories(database, Path(arguments["params"]))
        assert file_sha(database) == digest_before
    return {
        "restore_seconds": elapsed,
        "database_bytes": database.stat().st_size,
        "database_sha256": digest_before,
        "sqlite_schema_sha256": schema,
        "tables": inventory,
        "model_inputs": model_inputs,
        "model_count": len(model_inputs),
        "restored_source_counts": restored["counts"],
        "database_unchanged_during_readonly_comparison": True,
        "source_archive": arguments["archive"],
        "code_root": arguments["code_root"],
        "params_sha256": file_sha(Path(arguments["params"])),
    }


def offline_daily(arguments):
    import pytest

    # Use the same existing fixture orchestration with both implementations.
    helpers = importlib.import_module("test_cloud_apis")
    folder = Path(arguments["folder"])
    folder.mkdir(parents=True, exist_ok=False)
    mp = pytest.MonkeyPatch()
    try:
        mp.delenv("ZUCAI_NATIONS_LEAGUE", raising=False)
        mp.delenv("ZUCAI_APIF_DAILY_CAP", raising=False)
        archive = helpers.environment.__wrapped__(folder, mp)
        helpers.configure_fd(mp, folder)
        with responses.RequestsMock() as http:
            helpers.mock_apis(http, fd=True)
            started = time.perf_counter()
            work = helpers.run_apis(folder, mp, archive, "daily-fixture")
            elapsed = time.perf_counter() - started
            requests = [call.request.url for call in http.calls]
        info = helpers.archive_info(archive)
        inventory, schema = tables(work / "data/zucai.db")
        assert info["counts"]["matches"] == 6
        assert len(requests) == 5
        return {
            "description": "Small synthetic offline daily API chain: restore, one FD CSV, one FDORG window, three APIF dates, export. Not the production daily chain or a production latency estimate.",
            "elapsed_seconds": elapsed,
            "requests": requests,
            "match_count": info["counts"]["matches"],
            "counts": info["counts"],
            "tables": inventory,
            "sqlite_schema_sha256": schema,
            "source_inventory": info["sources"],
            "database_bytes": (work / "data/zucai.db").stat().st_size,
        }
    finally:
        mp.undo()


arguments = json.loads(Path(sys.argv[1]).read_bytes())
result = static_comparison(arguments) if arguments["mode"] == "static" else offline_daily(arguments)
Path(arguments["output"]).write_bytes(encoded(result))
print(json.dumps({"mode": arguments["mode"], "label": arguments["label"], "output": arguments["output"], "restore_seconds": result.get("restore_seconds"), "elapsed_seconds": result.get("elapsed_seconds"), "database_bytes": result["database_bytes"], "models": result.get("model_count")}, ensure_ascii=False), flush=True)
