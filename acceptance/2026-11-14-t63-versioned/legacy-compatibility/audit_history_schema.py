"""Read-only schema audit for frozen compatibility runs; no fit or scoring."""

import argparse
import json
import socket
import sqlite3
from pathlib import Path

from compatibility_worker import CORE_COLUMNS, digest_bytes, encoded, reject_network, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("database", "params", "summary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Schema audit output must be new")
    socket.create_connection = reject_network
    import requests
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from zucai.predict.backtest import read_history

    requests.sessions.Session.request = reject_network
    before = digest_bytes(args.database.read_bytes())
    uri = args.database.resolve().as_uri() + "?mode=ro&immutable=1"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    config = json.loads(args.params.read_bytes())["leagues"]
    summary = json.loads(args.summary.read_bytes())
    rows = []
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only=ON")
        with Session(bind=connection) as session:
            for league in sorted({fit["league"] for fit in summary["fits"].values()}):
                entry = config[league]
                history = read_history(
                    session,
                    league=league,
                    source=",".join(entry["sources"]),
                    training_leagues=entry.get("training_leagues"),
                )
                core_hash = digest_bytes(encoded(history[CORE_COLUMNS].to_dict("records")))
                all_hash = digest_bytes(encoded(history.to_dict("records")))
                fits = [fit for fit in summary["fits"].values() if fit["league"] == league]
                assert all(fit["read_history_core_sha256"] == core_hash for fit in fits)
                assert all(fit["read_history_all_columns_sha256"] == all_hash for fit in fits)
                rows.append(
                    {
                        "league": league,
                        "rows": len(history),
                        "columns": list(history.columns),
                        "dtypes": {column: str(dtype) for column, dtype in history.dtypes.items()},
                        "attrs": history.attrs,
                        "read_history_core_sha256": core_hash,
                        "read_history_all_columns_sha256": all_hash,
                        "matches_executed_fit_records": len(fits),
                    }
                )
    engine.dispose()
    assert digest_bytes(args.database.read_bytes()) == before
    write(args.output, {"database_sha256": before, "database_unchanged": True, "leagues": rows})


if __name__ == "__main__":
    main()
