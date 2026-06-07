"""A small SQLite-backed response cache for external sources.

Two modes per entry:
  * **permanent** — the cached value represents a terminal entity that will
    never change again (a merged PR, a DONE Huly issue). Never expires.
  * **TTL** — everything else (open issues, lists, search results). Expires
    after ``ttl`` seconds so it stays reasonably fresh.

Lives in the same DB file as the activity store, in its own ``cache`` table.
Values are JSON; callers serialize/deserialize their own shapes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    fetched_at REAL    NOT NULL,
    permanent  INTEGER NOT NULL DEFAULT 0
);
"""


class Cache:
    def __init__(self, db_path: str | Path, *, enabled: bool = True) -> None:
        self.db_path = str(db_path)
        self.enabled = enabled
        if enabled:
            with self._conn() as conn:
                conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str, ttl: float | None) -> Any | None:
        """Return the cached value if a permanent entry exists or it's within ttl."""
        if not self.enabled:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value, fetched_at, permanent FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value, fetched_at, permanent = row
        if permanent or (ttl is not None and (time.time() - fetched_at) <= ttl):
            return json.loads(value)
        return None

    def set(self, key: str, value: Any, *, permanent: bool = False) -> None:
        if not self.enabled:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, fetched_at, permanent) "
                "VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), time.time(), int(permanent)),
            )

    def clear(self) -> int:
        if not self.enabled:
            return 0
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            conn.execute("DELETE FROM cache")
        return n

    def stats(self) -> tuple[int, int]:
        """(total entries, permanent entries)."""
        if not self.enabled:
            return (0, 0)
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            perm = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE permanent = 1"
            ).fetchone()[0]
        return (total, perm)
