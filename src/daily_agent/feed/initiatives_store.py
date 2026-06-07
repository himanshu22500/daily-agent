"""Per-initiative story-state — the memory that makes the feed a *storyline*.

Each initiative carries a running plain-language summary of the effort and when
it was last narrated, so the next chapter only adds what's new rather than
re-describing the whole thing. No health/status is stored — the feed describes,
it doesn't judge.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS initiatives (
    key               TEXT NOT NULL PRIMARY KEY,
    lane              TEXT NOT NULL,
    title             TEXT NOT NULL,
    story_state       TEXT,
    last_narrated_at  TEXT,
    updated_at        TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InitiativeState:
    key: str
    lane: str
    title: str
    story_state: str | None
    last_narrated_at: str | None


class InitiativeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> InitiativeState | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM initiatives WHERE key=?", (key,)).fetchone()
        return _row(row) if row else None

    def upsert(self, key: str, lane: str, title: str) -> None:
        """Ensure the initiative row exists / refresh its lane + title."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO initiatives (key, lane, title, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  lane=excluded.lane, title=excluded.title, updated_at=excluded.updated_at
                """,
                (key, lane, title, _now_iso()),
            )

    def record_chapter(self, key: str, story_state: str, *, now: datetime | None = None) -> None:
        """Persist the updated story-state after a chapter is delivered."""
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE initiatives SET story_state=?, last_narrated_at=?, updated_at=? WHERE key=?",
                (story_state, stamp, stamp, key),
            )

    def all(self) -> list[InitiativeState]:
        with self._conn() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM initiatives ORDER BY updated_at DESC")]


def _row(row: sqlite3.Row) -> InitiativeState:
    return InitiativeState(
        key=row["key"], lane=row["lane"], title=row["title"],
        story_state=row["story_state"], last_narrated_at=row["last_narrated_at"],
    )
