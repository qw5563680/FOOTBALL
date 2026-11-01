"""SQLite 连接、建表和事务边界。"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event
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
    Base.metadata.create_all(engine)


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
