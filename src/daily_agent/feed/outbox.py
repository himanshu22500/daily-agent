"""The outbox — durable delivery queue (the robustness core of the feed).

Design goals (see ROADMAP "Delivery feed"): a bite is delivered **at-least-once
and never duplicated**, and a crash mid-send never double-sends or drops.

How those are guaranteed:
- ``enqueue`` is idempotent: ``outbox.dedup_key`` is ``UNIQUE`` and we also skip
  anything already in ``delivered_ledger``. Re-running the delta engine over the
  same activity is a no-op.
- ``drain`` marks a bite ``sent`` AND records it in ``delivered_ledger`` AND
  advances the subject ``watermark`` in a single transaction, only after the
  channel send succeeds. A crash before commit leaves the row ``pending`` (it
  retries); a crash after commit leaves it ``sent`` (the ledger blocks re-enqueue).
- Failures increment ``attempts`` and set ``not_before`` for backoff; past
  ``max_attempts`` a bite goes ``dead`` so a poison message can't wedge the queue.

This is deliberately channel-agnostic: ``drain`` takes any object with a
``send(item)`` method, so Phase 1 verifies dedup end-to-end against a console
channel before any Slack credentials exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, Session, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from ..models import Bite, StoryStateUpdate
from .initiatives_store import InitiativeRow


class OutboxRow(SQLModel, table=True):
    __tablename__ = "outbox"

    id: int | None = Field(default=None, primary_key=True)
    dedup_key: str = Field(unique=True)
    subject: str
    kind: str
    content: str
    # Story-state to commit to the initiative *only after* this bite is
    # delivered (issue #27). NULL for bites with no storyline to advance.
    story_state_key: str | None = None
    story_state: str | None = None
    status: str = "pending"  # pending|sent|failed|dead
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


class SentMessageRow(SQLModel, table=True):
    """Every message the bot has posted, keyed to the bite it carried. Two jobs:

    1. the disambiguation set — an inbound ``channel_post`` whose id is in here is
       one of our own posts, not a human follow-up (see issue #49);
    2. grounding — an inbound reply whose ``reply_to`` id is in here maps back to a
       bite's ``dedup_key``/``subject``, so the answer can be grounded on it.
    """

    __tablename__ = "sent_messages"

    chat_id: str = Field(primary_key=True)
    message_id: int = Field(primary_key=True)
    dedup_key: str
    subject: str
    sent_at: str


# A bite that has failed this many times is parked as `dead`.
MAX_ATTEMPTS = 5
# Exponential backoff base (seconds): wait grows 60s, 120s, 240s, ...
BACKOFF_BASE_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OutboxItem:
    """A queued bite as stored in the outbox."""

    id: int
    dedup_key: str
    subject: str
    kind: str
    content: str
    attempts: int
    # Pending storyline update to commit on successful delivery (issue #27).
    story_state_update: StoryStateUpdate | None = None
    # The bite's enqueue time — guards against an older queued chapter
    # overwriting a story-state already advanced by a newer one.
    created_at: str = ""


@dataclass(frozen=True)
class DrainResult:
    sent: int
    failed: int
    dead: int


@dataclass(frozen=True)
class SendReceipt:
    """Identifies the message a channel just posted, so it can be replied to.

    Returned by channels that address messages (Telegram); the outbox persists it
    to ``sent_messages`` keyed to the bite. Channels with no addressable message
    (console, file) return ``None`` and nothing is persisted.
    """

    chat_id: str
    message_id: int


class Channel(Protocol):
    """Anything that can deliver an :class:`OutboxItem`.

    ``send`` must raise on failure (so the outbox retries) and return normally on
    success (so the outbox commits the delivery). It may return a
    :class:`SendReceipt` identifying the posted message; ``None`` means the
    channel has nothing addressable to record.
    """

    name: str

    def send(self, item: OutboxItem) -> "SendReceipt | None": ...


class Outbox:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(
            self._engine,
            OutboxRow,
            DeliveredLedgerRow,
            WatermarkRow,
            SentMessageRow,
            # The outbox advances an initiative's story-state on delivery
            # (issue #27); ensure the table exists even if no InitiativeStore
            # has touched this DB yet.
            InitiativeRow,
        )
        self._migrate()

    def _migrate(self) -> None:
        """Add story-state columns to a pre-existing ``outbox`` table.

        ``create_all`` only creates missing *tables*, never adds columns, and
        there is no migration system (see ``db.py``) — so a live DB created
        before issue #27 keeps its old ``outbox`` schema. Bring it forward.
        """
        with session_scope(self._engine) as session:
            cols = {
                row[1]
                for row in session.execute(text("PRAGMA table_info(outbox)")).all()
            }
            for col in ("story_state_key", "story_state"):
                if col not in cols:
                    session.execute(text(f"ALTER TABLE outbox ADD COLUMN {col} TEXT"))

    # --- enqueue ---------------------------------------------------------- #
    def enqueue(self, bite: Bite, *, now: datetime | None = None) -> bool:
        """Add a bite to the queue. Idempotent.

        Returns ``True`` if this bite was newly queued, ``False`` if it was
        already queued (same ``dedup_key``) or already delivered (in the ledger).
        """
        stamp = (now or _now()).isoformat()
        with session_scope(self._engine) as session:
            if session.get(DeliveredLedgerRow, bite.dedup_key) is not None:
                return False
            ssu = bite.story_state_update
            stmt = (
                sqlite_insert(OutboxRow)
                .values(
                    dedup_key=bite.dedup_key,
                    subject=bite.subject,
                    kind=bite.kind,
                    content=bite.content,
                    story_state_key=ssu.initiative_key if ssu else None,
                    story_state=ssu.story_state if ssu else None,
                    created_at=stamp,
                )
                .on_conflict_do_nothing(index_elements=["dedup_key"])
            )
            result = session.execute(stmt)
            return result.rowcount > 0

    def enqueue_all(self, bites: list[Bite], *, now: datetime | None = None) -> int:
        """Enqueue many bites; returns how many were newly queued."""
        stamp = now or _now()
        return sum(self.enqueue(b, now=stamp) for b in bites)

    # --- drain ------------------------------------------------------------ #
    def _due(
        self, session: Session, now: datetime, limit: int | None
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
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            OutboxItem(
                id=r.id,
                dedup_key=r.dedup_key,
                subject=r.subject,
                kind=r.kind,
                content=r.content,
                attempts=r.attempts,
                story_state_update=(
                    StoryStateUpdate(
                        initiative_key=r.story_state_key,
                        story_state=r.story_state,
                    )
                    if r.story_state_key and r.story_state is not None
                    else None
                ),
                created_at=r.created_at,
            )
            for r in session.exec(stmt).all()
        ]

    def drain(
        self,
        channel: Channel,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> DrainResult:
        """Send due bites through ``channel``, oldest first.

        Each successful send commits (mark sent + ledger + watermark) on its own,
        so a failure partway through never rolls back already-delivered bites.
        """
        moment = now or _now()
        sent = failed = dead = 0
        with session_scope(self._engine) as session:
            due = self._due(session, moment, limit)
        for item in due:
            try:
                receipt = channel.send(item)
            except Exception as exc:  # noqa: BLE001 - any send failure retries
                with session_scope(self._engine) as session:
                    self._mark_failed(session, item, str(exc), moment)
                if item.attempts + 1 >= MAX_ATTEMPTS:
                    dead += 1
                else:
                    failed += 1
                continue
            with session_scope(self._engine) as session:
                self._mark_sent(session, item, moment, receipt)
            sent += 1
        return DrainResult(sent=sent, failed=failed, dead=dead)

    def _mark_sent(
        self,
        session: Session,
        item: OutboxItem,
        now: datetime,
        receipt: "SendReceipt | None" = None,
    ) -> None:
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
        wm = sqlite_insert(WatermarkRow).values(
            subject=item.subject, last_sent_at=stamp
        )
        # Only advance the watermark forward, never backward.
        session.execute(
            wm.on_conflict_do_update(
                index_elements=["subject"],
                set_={"last_sent_at": wm.excluded.last_sent_at},
                where=wm.excluded.last_sent_at > WatermarkRow.last_sent_at,
            )
        )
        if receipt is not None:
            # Re-delivery to the same (chat, message) just refreshes the mapping.
            sm = sqlite_insert(SentMessageRow).values(
                chat_id=str(receipt.chat_id),
                message_id=int(receipt.message_id),
                dedup_key=item.dedup_key,
                subject=item.subject,
                sent_at=stamp,
            )
            session.execute(
                sm.on_conflict_do_update(
                    index_elements=["chat_id", "message_id"],
                    set_={
                        "dedup_key": sm.excluded.dedup_key,
                        "subject": sm.excluded.subject,
                        "sent_at": sm.excluded.sent_at,
                    },
                )
            )
        ssu = item.story_state_update
        if ssu is not None:
            # Advance the initiative's storyline now that the chapter is out the
            # door — atomically with the sent/ledger/watermark writes (issue #27).
            # Guard so an older queued chapter can't clobber a story-state a newer
            # one already advanced (delivery order isn't guaranteed monotone).
            session.execute(
                update(InitiativeRow)
                .where(InitiativeRow.key == ssu.initiative_key)
                .where(
                    (InitiativeRow.last_narrated_at.is_(None))
                    | (InitiativeRow.last_narrated_at <= item.created_at)
                )
                .values(
                    story_state=ssu.story_state,
                    last_narrated_at=stamp,
                    updated_at=stamp,
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

    # --- introspection ---------------------------------------------------- #
    def watermark_for(self, subject: str) -> datetime | None:
        with session_scope(self._engine) as session:
            row = session.get(WatermarkRow, subject)
            return datetime.fromisoformat(row.last_sent_at) if row else None

    def sent_message(self, chat_id: str, message_id: int) -> dict | None:
        """Look up a message the bot posted, by ``(chat_id, message_id)``.

        Returns ``{dedup_key, subject, sent_at}`` if we sent it, else ``None``.
        The listener uses this twice per inbound update: the incoming message id
        (is it one of ours? → ignore) and its ``reply_to`` id (does it reply to a
        known bite? → answer, grounded on that bite's subject).
        """
        with session_scope(self._engine) as session:
            row = session.get(SentMessageRow, (str(chat_id), int(message_id)))
            if row is None:
                return None
            return {
                "dedup_key": row.dedup_key,
                "subject": row.subject,
                "sent_at": row.sent_at,
            }

    def delivered_keys(self) -> set[str]:
        with session_scope(self._engine) as session:
            return set(session.exec(select(DeliveredLedgerRow.item_key)).all())

    def stats(self) -> dict[str, int]:
        with session_scope(self._engine) as session:
            counts = {
                status: n
                for status, n in session.exec(
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
