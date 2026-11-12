"""The work queue behind followed sources: YouTube (#99) and Instagram (#105).

Each followed item (a video, a post) is a row that moves through the states
below. Before any work starts, a runner claims the row with a conditional
update, so two runners never write the same story, and a claim left by a
crashed runner expires after ``CLAIM_TTL``. Failed attempts are retried with
backoff, then given up on. ``StoryQueue`` does this for any table whose rows
have the queue columns (``state``, ``attempts``, ``not_before``,
``claimed_at``, ``error``, ``published_at``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import and_, or_, update
from sqlmodel import SQLModel, func, select

from ..db import session_scope

_LOG = logging.getLogger(__name__)

PENDING, WRITING, DONE, SKIPPED, FAILED = (
    "pending",
    "writing",
    "done",
    "skipped",
    "failed",
)
# Retries wait 1h, 2h, then 4h: long enough to outlast a Claude usage limit.
MAX_ATTEMPTS = 4
RETRY_BASE = timedelta(hours=1)
CLAIM_TTL = timedelta(minutes=15)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return (lines[0] if lines else type(exc).__name__)[:200]


@dataclass(frozen=True)
class SourceStatus:
    """One followed source, as the Sources page and the CLI show it."""

    id: str
    title: str
    url: str
    added_at: datetime
    checked_at: datetime | None
    check_error: str
    stories: int
    queued: int
    writing: int
    failed: list[tuple[str, str]]  # (item title, error)
    platform: str = "youtube"  # or "instagram"


@dataclass
class SyncReport:
    checked: int = 0
    queued: int = 0
    written: int = 0
    waiting: int = 0  # not ready yet (e.g. no captions); retried later
    failed: int = 0
    # Items ready to write that this pass left for the next one.
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


class StoryQueue:
    """Claim, finish, and retry the rows of one queue table."""

    def __init__(self, engine, row: type[SQLModel], key: str) -> None:
        self._engine = engine
        self._row = row
        self._key = getattr(row, key)

    def _claimable(self, stamp: str, stale: str):
        row = self._row
        return or_(
            and_(
                row.state == PENDING,
                or_(row.not_before.is_(None), row.not_before <= stamp),
            ),
            and_(row.state == WRITING, row.claimed_at < stale),
        )

    def ready(self, *, now: datetime, limit: int) -> list:
        """Rows that may be worked on now, newest first."""
        stmt = (
            select(self._row)
            .where(self._claimable(iso(now), iso(now - CLAIM_TTL)))
            .order_by(self._row.published_at.desc())
            .limit(limit)
        )
        with session_scope(self._engine) as session:
            return list(session.exec(stmt).all())

    def ready_count(self, *, now: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(self._row)
            .where(self._claimable(iso(now), iso(now - CLAIM_TTL)))
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    def claim(self, item_id: str, *, now: datetime) -> bool:
        """Take a row to work on; False if another runner has it (or it's done)."""
        stamp = iso(now)
        with session_scope(self._engine) as session:
            result = session.execute(
                update(self._row)
                .where(self._key == item_id)
                .where(self._claimable(stamp, iso(now - CLAIM_TTL)))
                .values(state=WRITING, claimed_at=stamp)
            )
            return result.rowcount == 1

    def finish(self, item_id: str) -> None:
        self._set(item_id, state=DONE, error="", not_before=None, claimed_at=None)

    def defer(self, item_id: str, until: datetime, *, error: str = "") -> None:
        """Back to the queue, not before ``until`` (no attempt counted)."""
        self._set(
            item_id,
            state=PENDING,
            not_before=iso(until),
            claimed_at=None,
            error=error[:300],
        )

    def fail_attempt(self, item_id: str, error: str, *, now: datetime) -> bool:
        """Count a failed attempt; retry later or give up. True if given up."""
        with session_scope(self._engine) as session:
            row = session.get(self._row, item_id)
            if row is None:
                return True
            row.attempts += 1
            row.error = error[:300]
            row.claimed_at = None
            if row.attempts >= MAX_ATTEMPTS:
                row.state = FAILED
                row.not_before = None
            else:
                row.state = PENDING
                row.not_before = iso(now + RETRY_BASE * 2 ** (row.attempts - 1))
            session.add(row)
            return row.state == FAILED

    def _set(self, item_id: str, **values) -> None:
        with session_scope(self._engine) as session:
            session.execute(
                update(self._row).where(self._key == item_id).values(**values)
            )


class Syncs(Protocol):
    async def run_once(self) -> SyncReport: ...


async def sync_forever(
    sync: Syncs, wake: asyncio.Event, *, idle: float = 600.0
) -> None:
    """Run sync passes until cancelled (the web server's background loop).

    After a pass that wrote something, the next starts right away while more
    items are ready; otherwise it waits ``idle`` seconds, or until ``wake`` is
    set (a source was just followed).
    """
    while True:
        wake.clear()
        try:
            report = await sync.run_once()
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            _LOG.exception("Sync pass failed")
            report = None
        if report is not None and report.written and report.remaining:
            continue
        try:
            await asyncio.wait_for(wake.wait(), idle)
        except asyncio.TimeoutError:
            pass
