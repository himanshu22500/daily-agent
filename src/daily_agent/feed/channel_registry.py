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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from ..db import create_tables, make_engine, session_scope


class ChannelRow(SQLModel, table=True):
    __tablename__ = "channels"

    stream_key: str = Field(primary_key=True)
    channel_id: int
    title: str
    created_at: str
    last_used_at: str


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
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, ChannelRow)

    def get(self, stream_key: str) -> ChannelRecord | None:
        with session_scope(self._engine) as session:
            row = session.get(ChannelRow, stream_key)
            return _to_record(row) if row else None

    def put(
        self,
        stream_key: str,
        channel_id: int,
        title: str,
        *,
        now: datetime | None = None,
    ) -> None:
        stamp = (now or _now()).isoformat()
        with session_scope(self._engine) as session:
            stmt = sqlite_insert(ChannelRow).values(
                stream_key=stream_key,
                channel_id=channel_id,
                title=title,
                created_at=stamp,
                last_used_at=stamp,
            )
            # created_at is intentionally left untouched on conflict.
            stmt = stmt.on_conflict_do_update(
                index_elements=["stream_key"],
                set_={
                    "channel_id": stmt.excluded.channel_id,
                    "title": stmt.excluded.title,
                    "last_used_at": stmt.excluded.last_used_at,
                },
            )
            session.execute(stmt)

    def touch(self, stream_key: str, *, now: datetime | None = None) -> None:
        with session_scope(self._engine) as session:
            row = session.get(ChannelRow, stream_key)
            if row is None:
                return
            row.last_used_at = (now or _now()).isoformat()
            session.add(row)

    def delete(self, stream_key: str) -> None:
        with session_scope(self._engine) as session:
            row = session.get(ChannelRow, stream_key)
            if row is not None:
                session.delete(row)

    def all(self) -> list[ChannelRecord]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(ChannelRow).order_by(ChannelRow.created_at)
            ).all()
            return [_to_record(r) for r in rows]


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


def _to_record(row: ChannelRow) -> ChannelRecord:
    return ChannelRecord(
        stream_key=row.stream_key,
        channel_id=row.channel_id,
        title=row.title,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )
