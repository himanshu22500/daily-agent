"""A small SQLite-backed response cache for external sources.

Two modes per entry:
  * **permanent** — the cached value represents a terminal entity that will
    never change again (e.g. a merged PR). Never expires.
  * **TTL** — everything else (open issues, lists, search results). Expires
    after ``ttl`` seconds so it stays reasonably fresh.

Lives in the same DB file as the activity store, in its own ``cache`` table.
Values are JSON; callers serialize/deserialize their own shapes. ``fetched_at``
stays an epoch float (REAL column) so the on-disk schema is unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, delete, func, select

from .db import create_tables, make_engine, session_scope


class CacheRow(SQLModel, table=True):
    __tablename__ = "cache"

    key: str = Field(primary_key=True)
    value: str
    fetched_at: float
    permanent: int = 0


class Cache:
    def __init__(self, db_path: str | Path, *, enabled: bool = True) -> None:
        self.db_path = str(db_path)
        self.enabled = enabled
        # create_engine is lazy (no file until first connect), so a disabled
        # cache that never queries touches nothing on disk.
        self._engine = make_engine(self.db_path)
        if enabled:
            create_tables(self._engine, CacheRow)

    def get(self, key: str, ttl: float | None) -> Any | None:
        """Return the cached value if a permanent entry exists or it's within ttl."""
        if not self.enabled:
            return None
        with session_scope(self._engine) as session:
            row = session.get(CacheRow, key)
        if row is None:
            return None
        if row.permanent or (ttl is not None and (time.time() - row.fetched_at) <= ttl):
            return json.loads(row.value)
        return None

    def set(self, key: str, value: Any, *, permanent: bool = False) -> None:
        if not self.enabled:
            return
        with session_scope(self._engine) as session:
            stmt = sqlite_insert(CacheRow).values(
                key=key,
                value=json.dumps(value),
                fetched_at=time.time(),
                permanent=int(permanent),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": stmt.excluded.value,
                    "fetched_at": stmt.excluded.fetched_at,
                    "permanent": stmt.excluded.permanent,
                },
            )
            session.execute(stmt)

    def clear(self) -> int:
        if not self.enabled:
            return 0
        with session_scope(self._engine) as session:
            n = session.scalar(select(func.count()).select_from(CacheRow)) or 0
            session.execute(delete(CacheRow))
        return n

    def stats(self) -> tuple[int, int]:
        """(total entries, permanent entries)."""
        if not self.enabled:
            return (0, 0)
        with session_scope(self._engine) as session:
            total = session.scalar(select(func.count()).select_from(CacheRow)) or 0
            perm = (
                session.scalar(
                    select(func.count())
                    .select_from(CacheRow)
                    .where(CacheRow.permanent == 1)
                )
                or 0
            )
        return (total, perm)
