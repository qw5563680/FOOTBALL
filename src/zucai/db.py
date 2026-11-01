"""SQLite 连接、建表和事务边界。"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event, inspect
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from zucai.models import Base, Match


def get_engine(path: str | Path | None = None) -> Engine:
    database = str(path if path is not None else os.environ.get("ZUCAI_DB", "data/zucai.db"))
    if not database:
        raise ValueError("数据库路径不能为空")
    if database != ":memory:":
        file = Path(database).expanduser().resolve()
        file.parent.mkdir(parents=True, exist_ok=True)
        database = str(file)
    engine = create_engine(URL.create("sqlite+pysqlite", database=database))

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(conn)
        for table, columns in {
            "predictions": {"batch_id": "VARCHAR", "mode": "VARCHAR", "as_of_utc": "VARCHAR(27)"},
            "results": {"available_utc": "VARCHAR(27)", "available_source": "VARCHAR"},
            "matches": {
                "result_checked_utc": "VARCHAR(27)",
                "result_provider": "VARCHAR",
                "counterpart_id": "VARCHAR",
            },
        }.items():
            existing = {col["name"] for col in inspect(conn).get_columns(table)}
            for column, datatype in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {datatype}")
        # Older model databases have the full-time-only trigger with this name.
        trigger = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='result_correction_clock'"
        ).scalar()
        if trigger and "available_source" not in trigger:
            conn.exec_driver_sql("DROP TRIGGER result_correction_clock")
        conn.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS result_correction_clock
            AFTER UPDATE OF home_goals, away_goals, ht_home_goals, ht_away_goals ON results
            WHEN (NEW.home_goals IS NOT OLD.home_goals OR NEW.away_goals IS NOT OLD.away_goals
               OR NEW.ht_home_goals IS NOT OLD.ht_home_goals
               OR NEW.ht_away_goals IS NOT OLD.ht_away_goals)
               AND NEW.available_utc IS OLD.available_utc
            BEGIN
                UPDATE results SET available_utc=strftime('%Y-%m-%dT%H:%M:%f000Z','now'),
                    available_source='captured' WHERE match_id=NEW.match_id;
            END
        """)


def upsert_match(session: Session, *, match_id: str, **fields: Any) -> Match:
    """Insert a complete fixture or update its supplied fields under the same source ID."""
    if not fields:
        raise ValueError("需要提供赛程字段")
    stmt = insert(Match).values(match_id=match_id, **fields)
    upsert = stmt.on_conflict_do_update(
        index_elements=[Match.match_id], set_={key: stmt.excluded[key] for key in fields}
    ).returning(Match)
    return session.scalars(upsert, execution_options={"populate_existing": True}).one()


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Commit the unit of work on success and roll it back if an exception escapes."""
    owned = engine is None
    if engine is None:
        engine = get_engine()
    try:
        with Session(engine, expire_on_commit=False) as session, session.begin():
            yield session
    finally:
        if owned:
            engine.dispose()
