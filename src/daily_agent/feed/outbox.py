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

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol

from ..models import Bite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT    NOT NULL UNIQUE,
    subject     TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|sent|failed|dead
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  TEXT,
    created_at  TEXT    NOT NULL,
    sent_at     TEXT,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS delivered_ledger (
    item_key  TEXT NOT NULL PRIMARY KEY,
    sent_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watermark (
    subject       TEXT NOT NULL PRIMARY KEY,
    last_sent_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status, not_before);
"""

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


@dataclass(frozen=True)
class DrainResult:
    sent: int
    failed: int
    dead: int


class Channel(Protocol):
    """Anything that can deliver an :class:`OutboxItem`.

    ``send`` must raise on failure (so the outbox retries) and return normally on
    success (so the outbox commits the delivery).
    """

    name: str

    def send(self, item: OutboxItem) -> None: ...


class Outbox:
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

    # --- enqueue ---------------------------------------------------------- #
    def enqueue(self, bite: Bite, *, now: datetime | None = None) -> bool:
        """Add a bite to the queue. Idempotent.

        Returns ``True`` if this bite was newly queued, ``False`` if it was
        already queued (same ``dedup_key``) or already delivered (in the ledger).
        """
        stamp = (now or _now()).isoformat()
        with self._conn() as conn:
            already = conn.execute(
                "SELECT 1 FROM delivered_ledger WHERE item_key = ?",
                (bite.dedup_key,),
            ).fetchone()
            if already:
                return False
            cur = conn.execute(
                """
                INSERT INTO outbox (dedup_key, subject, kind, content, created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(dedup_key) DO NOTHING
                """,
                (bite.dedup_key, bite.subject, bite.kind, bite.content, stamp),
            )
            return cur.rowcount > 0

    def enqueue_all(self, bites: list[Bite], *, now: datetime | None = None) -> int:
        """Enqueue many bites; returns how many were newly queued."""
        stamp = now or _now()
        return sum(self.enqueue(b, now=stamp) for b in bites)

    # --- drain ------------------------------------------------------------ #
    def _due(
        self, conn: sqlite3.Connection, now: datetime, limit: int | None
    ) -> list[OutboxItem]:
        sql = (
            "SELECT id, dedup_key, subject, kind, content, attempts FROM outbox "
            "WHERE status IN ('pending','failed') "
            "AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY created_at, id"
        )
        params: tuple = (now.isoformat(),)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [
            OutboxItem(
                id=r["id"],
                dedup_key=r["dedup_key"],
                subject=r["subject"],
                kind=r["kind"],
                content=r["content"],
                attempts=r["attempts"],
            )
            for r in conn.execute(sql, params)
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
        with self._conn() as conn:
            due = self._due(conn, moment, limit)
        for item in due:
            try:
                channel.send(item)
            except Exception as exc:  # noqa: BLE001 - any send failure retries
                with self._conn() as conn:
                    self._mark_failed(conn, item, str(exc), moment)
                if item.attempts + 1 >= MAX_ATTEMPTS:
                    dead += 1
                else:
                    failed += 1
                continue
            with self._conn() as conn:
                self._mark_sent(conn, item, moment)
            sent += 1
        return DrainResult(sent=sent, failed=failed, dead=dead)

    def _mark_sent(
        self, conn: sqlite3.Connection, item: OutboxItem, now: datetime
    ) -> None:
        stamp = now.isoformat()
        conn.execute(
            "UPDATE outbox SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
            (stamp, item.id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO delivered_ledger (item_key, sent_at) VALUES (?,?)",
            (item.dedup_key, stamp),
        )
        conn.execute(
            """
            INSERT INTO watermark (subject, last_sent_at) VALUES (?,?)
            ON CONFLICT(subject) DO UPDATE SET
              last_sent_at=excluded.last_sent_at
              WHERE excluded.last_sent_at > watermark.last_sent_at
            """,
            (item.subject, stamp),
        )

    def _mark_failed(
        self, conn: sqlite3.Connection, item: OutboxItem, error: str, now: datetime
    ) -> None:
        attempts = item.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                (attempts, error, item.id),
            )
            return
        backoff = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
        not_before = (now + timedelta(seconds=backoff)).isoformat()
        conn.execute(
            "UPDATE outbox SET status='failed', attempts=?, not_before=?, last_error=? WHERE id=?",
            (attempts, not_before, error, item.id),
        )

    # --- introspection ---------------------------------------------------- #
    def watermark_for(self, subject: str) -> datetime | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_sent_at FROM watermark WHERE subject=?", (subject,)
            ).fetchone()
        return datetime.fromisoformat(row["last_sent_at"]) if row else None

    def delivered_keys(self) -> set[str]:
        with self._conn() as conn:
            return {
                r["item_key"]
                for r in conn.execute("SELECT item_key FROM delivered_ledger")
            }

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            counts = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) n FROM outbox GROUP BY status"
                )
            }
            delivered = conn.execute(
                "SELECT COUNT(*) n FROM delivered_ledger"
            ).fetchone()["n"]
        return {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "sent": counts.get("sent", 0),
            "dead": counts.get("dead", 0),
            "delivered": delivered,
        }
