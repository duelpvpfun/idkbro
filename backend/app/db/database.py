"""Async SQLite engine, session factory, and schema init.

SQLite only allows one writer at a time; with several concurrent agent tasks writing we
enable WAL mode + a busy timeout, and funnel all writes through a single async lock so we
never hit 'database is locked'. This keeps the MVP simple while staying correct; swap for
Postgres later for real concurrency.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, "idkbro.db")

engine = create_async_engine(
    f"sqlite+aiosqlite:///{_DB_PATH}",
    echo=False,
    connect_args={"timeout": 30},
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Serialize writes across the app's concurrent tasks.
write_lock = asyncio.Lock()


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _record) -> None:
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)        # Lightweight migrations: add columns that new versions introduced, without
        # wiping the agent's existing memory. SQLite ignores IF NOT EXISTS on ADD COLUMN,
        # so we check the table's columns first.
        await conn.run_sync(_migrate)


def _migrate(sync_conn) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    tables = inspector.get_table_names()
    # trackers.pfp_url added in a later version.
    if "tracked_traders" in tables:
        cols = {c["name"] for c in inspector.get_columns("tracked_traders")}
        if "pfp_url" not in cols:
            sync_conn.execute(text("ALTER TABLE tracked_traders ADD COLUMN pfp_url VARCHAR(256) DEFAULT ''"))
    # watchlist miss post-mortem columns.
    if "watchlist" in tables:
        wcols = {c["name"] for c in inspector.get_columns("watchlist")}
        if "skip_reason" not in wcols:
            sync_conn.execute(text("ALTER TABLE watchlist ADD COLUMN skip_reason TEXT DEFAULT ''"))
        if "postmortem_done" not in wcols:
            sync_conn.execute(text("ALTER TABLE watchlist ADD COLUMN postmortem_done INTEGER DEFAULT 0"))