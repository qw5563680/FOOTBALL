#!/usr/bin/env python3
"""Rerunnable old b821ae6 read-only Euro guard reproduction.

Run with PYTHONPATH=acceptance/code-b821ae6/src, not the changing worktree.
"""

import json
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from acceptance import BASE_DB, BASE_SHA, ROOT, T66, sha, write
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from zucai.predict import backtest


def main():
    assert Path(backtest.__file__).resolve().is_relative_to(ROOT / "code-b821ae6")
    meta = json.loads((T66 / "training/欧战.train.json").read_text())
    before = sha(BASE_DB)
    assert before == BASE_SHA
    calls = []

    def blocked(*args, **kwargs):
        calls.append(repr(args[:2]))
        raise AssertionError("HTTP and sockets blocked in read-only reproduction")

    result = dict(
        source_commit="b821ae6",
        source_module=backtest.__file__,
        database_sha256_before=before,
        cutoff_utc=meta["cutoff_utc"],
        training_scope=meta["training_league_scope"],
    )
    engine = create_engine(f"sqlite+pysqlite:///file:{BASE_DB}?mode=ro&uri=true")
    with ExitStack() as stack:
        for name in (
            "requests.sessions.Session.request",
            "socket.create_connection",
            "socket.socket.connect",
        ):
            stack.enter_context(patch(name, side_effect=blocked))
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA query_only=ON")
                with Session(bind=conn) as session:
                    backtest.read_history(
                        session,
                        "欧战",
                        ",".join(meta["sources"]),
                        training_leagues=meta["training_league_scope"],
                    )
        except Exception as exc:
            traceback.print_exc()
            result.update(status="expected_failure", exception=type(exc).__name__, message=str(exc))
        finally:
            engine.dispose()
    result.update(database_sha256_after=sha(BASE_DB), http_calls=calls)
    assert result["database_sha256_after"] == before
    assert result.get("status") == "expected_failure"
    assert (
        "apif:1494327" in result["message"]
        and "fd:SWE:20261103:sirius:halmstad" in result["message"]
    )
    assert not calls
    write(ROOT / "b821ae6-euro-red-rerun.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
