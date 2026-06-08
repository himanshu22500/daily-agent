"""Channel registry + provisioning orchestration for the multi-stream feed.

Different notification types (org-activity, insights, alerts, …) go to separate
Telegram channels the tool creates and deletes on its own. This module is the
*provisioner-agnostic* core: it remembers which channel backs each stream and
decides when to create or reap one — independent of how channels are actually
created (that's a ``Provisioner``, implemented live with Telethon elsewhere).

Built and tested offline first (a fake ``Provisioner`` in tests), the same way
the outbox was channel-agnostic before any real channel existed.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    stream_key    TEXT    NOT NULL PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    last_used_at  TEXT    NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ChannelRecord:
    stream_key: str
    channel_id: int
    title: str
    created_at: str
    last_used_at: str


class Provisioner(Protocol):
    """Creates/deletes the actual Telegram channels (Telethon impl is live)."""

    def create_channel(self, title: str, about: str = "") -> int: ...

    def delete_channel(self, channel_id: int) -> None: ...


class ChannelRegistry:
    """Persists the stream → channel mapping in SQLite."""

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

    def get(self, stream_key: str) -> ChannelRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE stream_key=?", (stream_key,)
            ).fetchone()
        return _row(row) if row else None

    def put(
        self,
        stream_key: str,
        channel_id: int,
        title: str,
        *,
        now: datetime | None = None,
    ) -> None:
        stamp = (now or _now()).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO channels (stream_key, channel_id, title, created_at, last_used_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(stream_key) DO UPDATE SET
                  channel_id=excluded.channel_id, title=excluded.title,
                  last_used_at=excluded.last_used_at
                """,
                (stream_key, channel_id, title, stamp, stamp),
            )

    def touch(self, stream_key: str, *, now: datetime | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE channels SET last_used_at=? WHERE stream_key=?",
                ((now or _now()).isoformat(), stream_key),
            )

    def delete(self, stream_key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM channels WHERE stream_key=?", (stream_key,))

    def all(self) -> list[ChannelRecord]:
        with self._conn() as conn:
            return [
                _row(r)
                for r in conn.execute("SELECT * FROM channels ORDER BY created_at")
            ]


def ensure_channel(
    stream_key: str,
    title: str,
    *,
    registry: ChannelRegistry,
    provisioner: Provisioner,
    now: datetime | None = None,
) -> int:
    """Return the channel id for ``stream_key``, provisioning it on first use.

    Idempotent: an existing stream returns its channel (and its last_used is
    refreshed); a new stream is provisioned exactly once and registered.
    """
    moment = now or _now()
    existing = registry.get(stream_key)
    if existing is not None:
        registry.touch(stream_key, now=moment)
        return existing.channel_id
    channel_id = provisioner.create_channel(title)
    registry.put(stream_key, channel_id, title, now=moment)
    return channel_id


def reap_stale(
    registry: ChannelRegistry,
    provisioner: Provisioner,
    max_idle_days: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Delete channels unused for more than ``max_idle_days``.

    Returns the stream keys that were reaped. A channel deleted upstream that
    errors here is left in the registry to retry next time (we don't drop the
    record unless the delete succeeds).
    """
    moment = now or _now()
    cutoff = moment - timedelta(days=max_idle_days)
    reaped: list[str] = []
    for rec in registry.all():
        if datetime.fromisoformat(rec.last_used_at) < cutoff:
            provisioner.delete_channel(rec.channel_id)
            registry.delete(rec.stream_key)
            reaped.append(rec.stream_key)
    return reaped


def _row(row: sqlite3.Row) -> ChannelRecord:
    return ChannelRecord(
        stream_key=row["stream_key"],
        channel_id=row["channel_id"],
        title=row["title"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )
