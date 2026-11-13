"""Bookkeeping shared by followed sources: YouTube channels (#99), Instagram
accounts (#107).

Each platform keeps two tables: the sources being followed, and the items
(videos, posts) found on them. An item is queued (``pending``), claimed by one
runner (``writing``) with a conditional update, then ``done``, or retried with
backoff and finally ``failed``. A claim left by a crashed runner expires. This
module holds that logic once. ``StoryQueue`` is the queue alone (interview-prep
explainers, #108, use just that); ``FollowedSources`` adds the followed
sources, and ``VideoStore`` and ``InstagramStore`` supply their tables
(``source_row``/``item_row``) and how a source's items are recorded.

A queue table needs these columns: ``published_at`` (orders the queue),
``state``, ``attempts``, ``not_before``, ``claimed_at``, ``error``; with
followed sources also ``source_id`` and ``title``. Source tables need: ``id``,
``url``, ``title``, ``added_at``, ``removed_at``, ``checked_at``,
``check_error``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Protocol

from sqlalchemy import Engine, and_, or_, update
from sqlmodel import func, select

from ..db import session_scope

_LOG = logging.getLogger(__name__)

# Retries wait 1h, 2h, then 4h: long enough to outlast a Claude usage limit.
MAX_ATTEMPTS = 4
RETRY_BASE = timedelta(hours=1)
CLAIM_TTL = timedelta(minutes=15)

PENDING, WRITING, DONE, SKIPPED, FAILED = (
    "pending",
    "writing",
    "done",
    "skipped",
    "failed",
)


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
    platform: str = "youtube"


@dataclass
class SyncReport:
    checked: int = 0
    queued: int = 0
    # Of those, older videos taken from a quiet source's backlog (#124).
    older: int = 0
    written: int = 0
    waiting: int = 0  # not ready yet (e.g. no captions); retried later
    failed: int = 0
    # Items ready to write that this pass left for the next one.
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


class StoryQueue:
    """Claim/retry bookkeeping over a table of queued items."""

    item_row: ClassVar[Any]
    item_id: ClassVar[str]  # primary-key column of ``item_row``
    # Stories are written newest first; questions (#119) are answered in the
    # order they were asked, so a follow-up sees the earlier answer.
    newest_first: ClassVar[bool] = True
    _engine: Engine

    def _claimable(self, stamp: str, stale: str):
        item = self.item_row
        return or_(
            and_(
                item.state == PENDING,
                or_(item.not_before.is_(None), item.not_before <= stamp),
            ),
            and_(item.state == WRITING, item.claimed_at < stale),
        )

    def ready(self, *, now: datetime, limit: int) -> list:
        """Queued items that may be worked on now (newest first by default)."""
        published = self.item_row.published_at
        stmt = (
            select(self.item_row)
            .where(self._claimable(iso(now), iso(now - CLAIM_TTL)))
            .order_by(
                published.desc() if self.newest_first else published.asc(),
                self._id_col(),
            )
            .limit(limit)
        )
        with session_scope(self._engine) as session:
            return list(session.exec(stmt).all())

    def ready_count(self, *, now: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(self.item_row)
            .where(self._claimable(iso(now), iso(now - CLAIM_TTL)))
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    def _id_col(self):
        return getattr(self.item_row, self.item_id)

    def claim(self, item_id: str, *, now: datetime) -> bool:
        """Take an item to work on; False if another runner has it (or it's done)."""
        stamp = iso(now)
        with session_scope(self._engine) as session:
            result = session.execute(
                update(self.item_row)
                .where(self._id_col() == item_id)
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
            row = session.get(self.item_row, item_id)
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
                update(self.item_row).where(self._id_col() == item_id).values(**values)
            )


class FollowedSources(StoryQueue):
    """A platform's followed sources plus the queue of items found on them."""

    source_row: ClassVar[Any]
    platform: ClassVar[str]

    def unfollow(self, source_id: str, *, now: datetime) -> bool:
        """Stop following; queued items are dropped, written stories stay."""
        item = self.item_row
        with session_scope(self._engine) as session:
            row = session.get(self.source_row, source_id)
            if row is None or row.removed_at:
                return False
            row.removed_at = iso(now)
            session.add(row)
            session.execute(
                update(item)
                .where(item.source_id == source_id)
                .where(item.state.in_((PENDING, FAILED)))
                .values(state=SKIPPED)
            )
            return True

    def source(self, source_id: str):
        with session_scope(self._engine) as session:
            return session.get(self.source_row, source_id)

    def active_sources(self) -> list:
        src = self.source_row
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(src)
                    .where(src.removed_at.is_(None))
                    .order_by(src.added_at, *src.__table__.primary_key.columns)
                ).all()
            )

    def mark_checked(
        self, source_id: str, *, now: datetime, error: str = "", title: str = ""
    ) -> None:
        """Record a check; ``title`` names a source that has no name yet."""
        with session_scope(self._engine) as session:
            row = session.get(self.source_row, source_id)
            if row is None:
                return
            if error:
                row.check_error = error[:300]
            else:
                row.checked_at = iso(now)
                row.check_error = ""
            if title and not row.title:
                row.title = title
            session.add(row)

    def statuses(self) -> list[SourceStatus]:
        sources = self.active_sources()
        item = self.item_row
        with session_scope(self._engine) as session:
            items = session.exec(
                select(item).where(item.source_id.in_([s.id for s in sources]))
            ).all()
        by_source: dict[str, list] = {}
        for row in items:
            by_source.setdefault(row.source_id, []).append(row)
        out = []
        for source in sources:
            rows = by_source.get(source.id, [])
            states = Counter(r.state for r in rows)
            out.append(
                SourceStatus(
                    id=source.id,
                    title=source.title or source.id,
                    url=source.url,
                    added_at=aware(source.added_at),
                    checked_at=aware(source.checked_at) if source.checked_at else None,
                    check_error=source.check_error,
                    stories=states[DONE],
                    queued=states[PENDING],
                    writing=states[WRITING],
                    failed=[(r.title, r.error) for r in rows if r.state == FAILED],
                    platform=self.platform,
                )
            )
        return out


class Syncer(Protocol):
    async def run_once(self) -> SyncReport: ...


async def sync_forever(
    sync: Syncer, wake: asyncio.Event, *, idle: float = 600.0
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
            _LOG.exception("Story sync pass failed")
            report = None
        if report is not None and report.written and report.remaining:
            continue
        try:
            await asyncio.wait_for(wake.wait(), idle)
        except asyncio.TimeoutError:
            pass
