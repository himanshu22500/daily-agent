"""Durable, deduplicated delivery queue for personal insights."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, Session, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from ..models import Bite


class OutboxRow(SQLModel, table=True):
    __tablename__ = "outbox"

    id: int | None = Field(default=None, primary_key=True)
    dedup_key: str = Field(unique=True)
    subject: str
    kind: str
    content: str
    status: str = "pending"
    attempts: int = 0
    not_before: str | None = None
    created_at: str
    sent_at: str | None = None
    last_error: str | None = None


class DeliveredLedgerRow(SQLModel, table=True):
    __tablename__ = "delivered_ledger"

    item_key: str = Field(primary_key=True)
    sent_at: str


class WatermarkRow(SQLModel, table=True):
    __tablename__ = "watermark"

    subject: str = Field(primary_key=True)
    last_sent_at: str


MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OutboxItem:
    id: int
    dedup_key: str
    subject: str
    kind: str
    content: str
    attempts: int
    created_at: str = ""


@dataclass(frozen=True)
class DrainResult:
    sent: int
    failed: int
    dead: int


class Channel(Protocol):
    name: str

    def send(self, item: OutboxItem) -> None: ...


class Outbox:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, OutboxRow, DeliveredLedgerRow, WatermarkRow)

    def enqueue(self, bite: Bite, *, now: datetime | None = None) -> bool:
        stamp = (now or _now()).isoformat()
        with session_scope(self._engine) as session:
            if session.get(DeliveredLedgerRow, bite.dedup_key) is not None:
                return False
            stmt = (
                sqlite_insert(OutboxRow)
                .values(
                    dedup_key=bite.dedup_key,
                    subject=bite.subject,
                    kind=bite.kind,
                    content=bite.content,
                    created_at=stamp,
                )
                .on_conflict_do_nothing(index_elements=["dedup_key"])
            )
            return session.execute(stmt).rowcount > 0

    def _due(
        self, session: Session, now: datetime, limit: int | None, *, kind: str | None
    ) -> list[OutboxItem]:
        stmt = (
            select(OutboxRow)
            .where(OutboxRow.status.in_(("pending", "failed")))
            .where(
                (OutboxRow.not_before.is_(None))
                | (OutboxRow.not_before <= now.isoformat())
            )
            .order_by(OutboxRow.created_at, OutboxRow.id)
        )
        if kind is not None:
            stmt = stmt.where(OutboxRow.kind == kind)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            OutboxItem(
                id=row.id,
                dedup_key=row.dedup_key,
                subject=row.subject,
                kind=row.kind,
                content=row.content,
                attempts=row.attempts,
                created_at=row.created_at,
            )
            for row in session.exec(stmt).all()
        ]

    def drain(
        self,
        channel: Channel,
        *,
        limit: int | None = None,
        now: datetime | None = None,
        kind: str | None = None,
    ) -> DrainResult:
        moment = now or _now()
        sent = failed = dead = 0
        with session_scope(self._engine) as session:
            due = self._due(session, moment, limit, kind=kind)
        for item in due:
            try:
                channel.send(item)
            except Exception as exc:  # noqa: BLE001
                with session_scope(self._engine) as session:
                    self._mark_failed(session, item, str(exc), moment)
                if item.attempts + 1 >= MAX_ATTEMPTS:
                    dead += 1
                else:
                    failed += 1
                continue
            with session_scope(self._engine) as session:
                self._mark_sent(session, item, moment)
            sent += 1
        return DrainResult(sent=sent, failed=failed, dead=dead)

    def _mark_sent(self, session: Session, item: OutboxItem, now: datetime) -> None:
        stamp = now.isoformat()
        row = session.get(OutboxRow, item.id)
        if row is not None:
            row.status = "sent"
            row.sent_at = stamp
            row.last_error = None
            session.add(row)
        session.execute(
            sqlite_insert(DeliveredLedgerRow)
            .values(item_key=item.dedup_key, sent_at=stamp)
            .on_conflict_do_nothing(index_elements=["item_key"])
        )
        watermark = sqlite_insert(WatermarkRow).values(
            subject=item.subject, last_sent_at=stamp
        )
        session.execute(
            watermark.on_conflict_do_update(
                index_elements=["subject"],
                set_={"last_sent_at": watermark.excluded.last_sent_at},
                where=watermark.excluded.last_sent_at > WatermarkRow.last_sent_at,
            )
        )

    def _mark_failed(
        self, session: Session, item: OutboxItem, error: str, now: datetime
    ) -> None:
        row = session.get(OutboxRow, item.id)
        if row is None:
            return
        attempts = item.attempts + 1
        row.attempts = attempts
        row.last_error = error
        if attempts >= MAX_ATTEMPTS:
            row.status = "dead"
        else:
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
            row.status = "failed"
            row.not_before = (now + timedelta(seconds=backoff)).isoformat()
        session.add(row)

    def watermark_for(self, subject: str) -> datetime | None:
        with session_scope(self._engine) as session:
            row = session.get(WatermarkRow, subject)
            return datetime.fromisoformat(row.last_sent_at) if row else None

    def delivered_keys(self) -> set[str]:
        with session_scope(self._engine) as session:
            return set(session.exec(select(DeliveredLedgerRow.item_key)).all())

    def stats(self) -> dict[str, int]:
        with session_scope(self._engine) as session:
            counts = {
                status: count
                for status, count in session.exec(
                    select(OutboxRow.status, func.count()).group_by(OutboxRow.status)
                ).all()
            }
            delivered = (
                session.scalar(select(func.count()).select_from(DeliveredLedgerRow))
                or 0
            )
        return {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "sent": counts.get("sent", 0),
            "dead": counts.get("dead", 0),
            "delivered": delivered,
        }
