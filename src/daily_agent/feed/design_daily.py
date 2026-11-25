"""A daily system design card in the Interview tab (issue #134).

Each local day, a background loop in ``daily-agent web`` on the Mac (or
``daily-agent prep design write``) queues the next ``prep_design_per_day`` case studies and
``prep_blocks_per_day`` building blocks from the curated list
(``design_topics``) and has Claude write them (``agents/design_writer``). Cards
go in through ``PrepItems.put`` under the source "Daily system design": type
``design``, ``web-only``, tagged with the topic's slug and the building blocks
it uses.

- **Order**, per kind: topics that touch the reader's weak design tags
  (``prep_grades.weak_tags``, weakest first) and Blinkit-flavoured ones, then
  the classics, then the rest; the list's own order within a group. A topic
  that failed comes back only after every untried one.
- **Each topic once.** A queued topic keeps its row in ``prep_design_topics``,
  so it's never picked again. ``write --topic`` writes it again on request,
  updating the card in place.
- **Pacing.** A day's quota counts the topics queued for that local day
  (midnight on the Mac). The loop looks every 15 minutes, so after the Mac
  sleeps it queues today's cards on waking; missed days aren't made up.
  Topics asked for by hand (``--topic``, ``--limit``) are extras and don't
  count toward the day.
- **Retries** use the shared ``StoryQueue`` backoff (1h, 2h, 4h, then given up).
- **The cheat sheet** of latency numbers goes in once, with no model call.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlmodel import Field, SQLModel, select

from ..agents.design_writer import DesignCard
from ..db import conflict_insert, create_tables, make_engine, session_scope
from .design_topics import (
    BLOCK,
    CASE,
    CHEAT_SHEET_PROMPT,
    CHEAT_SHEET_SLUG,
    CHEAT_SHEET_STEPS,
    CHEAT_SHEET_TAGS,
    CHEAT_SHEET_TITLE,
    TOPICS,
    DesignTopic,
)
from .prep_grades import GradeStore
from .prep_tracks import (
    DESIGN,
    DESIGN_TYPE,
    PrepItem,
    PrepItems,
    item_key,
    join_steps,
    types_for,
)
from .story_queue import (
    DONE,
    FAILED,
    PENDING,
    SKIPPED,
    WRITING,
    StoryQueue,
    Syncer,
    SyncReport,
    first_line,
    iso,
    now_utc,
    sync_forever,
)

_LOG = logging.getLogger(__name__)

SOURCE_ID = "design-daily"
SOURCE_LABEL = "Daily system design"
PER_RUN = 3
IDLE_SECONDS = 900.0
WEAK_TAGS = 5
# Tags that name where a card is from rather than a skill.
NOT_SKILLS = ("blinkit",)
KINDS = (CASE, BLOCK)


def card_key(slug: str) -> str:
    return item_key(SOURCE_ID, slug)


def card_tags(topic: DesignTopic) -> list[str]:
    return [topic.slug, *topic.tags, *(["blinkit"] if topic.blinkit else [])]


def local_day(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).date().isoformat()


def order_topics(
    topics: Iterable[DesignTopic], weak: Sequence[str] = ()
) -> list[DesignTopic]:
    """Topics touching a weak tag (weakest first) and Blinkit ones, then the
    classics, then the rest; the given order within each group."""
    rank = {tag: i for i, tag in enumerate(weak)}

    def key(pair: tuple[int, DesignTopic]) -> tuple[int, int, int]:
        index, topic = pair
        hit = min(
            (rank[tag] for tag in (topic.slug, *topic.tags) if tag in rank),
            default=len(rank),
        )
        group = 0 if topic.blinkit or hit < len(rank) else 1 if topic.classic else 2
        return group, hit, index

    return [topic for _, topic in sorted(enumerate(topics), key=key)]


class DesignTopicRow(SQLModel, table=True):
    """A topic once it's been queued. Topics without a row are still to come."""

    __tablename__ = "prep_design_topics"

    id: str = Field(primary_key=True)  # the topic's slug
    kind: str  # "case" or "block"
    day: str  # the local day it was queued for, e.g. "2026-11-25"
    extra: bool = False  # asked for by hand; doesn't count toward the day
    published_at: str  # when it was (re)queued; orders the queue, oldest first
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""


class DesignStore(StoryQueue):
    """``prep_design_topics``, queued and claimed through ``StoryQueue``."""

    item_row = DesignTopicRow
    item_id = "id"
    newest_first = False

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, DesignTopicRow)

    def rows(self) -> dict[str, DesignTopicRow]:
        with session_scope(self._engine) as session:
            return {row.id: row for row in session.exec(select(DesignTopicRow)).all()}

    def row(self, slug: str) -> DesignTopicRow | None:
        with session_scope(self._engine) as session:
            return session.get(DesignTopicRow, slug)

    def queued_for(self, day: str) -> Counter[str]:
        """How many topics the daily pace queued for ``day``, per kind."""
        with session_scope(self._engine) as session:
            kinds = session.exec(
                select(DesignTopicRow.kind)
                .where(DesignTopicRow.day == day)
                .where(DesignTopicRow.extra.is_(False))
            ).all()
        return Counter(kinds)

    def queue(
        self, topic: DesignTopic, *, now: datetime, day: str, extra: bool = False
    ) -> bool:
        """Queue a topic: new, or again (written, failed, waiting to retry).
        False while it's being written. A daily pick moves the row to ``day``;
        a topic asked for again by hand keeps the day it first counted for."""
        stamp = iso(now)
        with session_scope(self._engine) as session:
            new = session.execute(
                conflict_insert(session, DesignTopicRow)
                .values(
                    id=topic.slug,
                    kind=topic.kind,
                    day=day,
                    extra=extra,
                    published_at=stamp,
                    state=PENDING,
                    attempts=0,
                    error="",
                )
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(DesignTopicRow.id)
            ).first()
            if new is not None:
                return True
            values: dict[str, Any] = {
                "state": PENDING,
                "attempts": 0,
                "error": "",
                "not_before": None,
                "claimed_at": None,
                "published_at": stamp,
            }
            if not extra:
                values.update(day=day, extra=False)
            result = session.execute(
                update(DesignTopicRow)
                .where(DesignTopicRow.id == topic.slug)
                .where(DesignTopicRow.state != WRITING)
                .values(**values)
            )
            return result.rowcount == 1

    def skip(self, slug: str) -> None:
        self._set(slug, state=SKIPPED, claimed_at=None)


@dataclass(frozen=True)
class DesignProgress:
    topics: int
    written: int
    queued: int  # waiting or being written
    failed: list[tuple[str, str]]  # (slug, error)
    today: Counter[str]  # the day's picks so far, per kind
    next_up: list[DesignTopic]
    weak: list[str]
    states: dict[str, str]  # slug -> state, for topics queued so far


CardWriter = Callable[[DesignTopic], Awaitable[DesignCard]]


class DesignSync:
    """Queue each day's design topics and write their cards."""

    def __init__(
        self,
        db_path: str | Path,
        write_card: CardWriter,
        *,
        cases_per_day: int = 1,
        blocks_per_day: int = 1,
        topics: Sequence[DesignTopic] = TOPICS,
        weak_tags: Callable[[], Sequence[str]] | None = None,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
        tz: tzinfo | None = None,
    ) -> None:
        self.store = DesignStore(db_path)
        self.items = PrepItems(db_path)
        self.write_card = write_card
        self.per_day = {CASE: max(0, cases_per_day), BLOCK: max(0, blocks_per_day)}
        self.topics = tuple(topics)
        self.by_slug = {topic.slug: topic for topic in self.topics}
        self._db = db_path
        self._weak_tags = weak_tags or self._graded_weak_tags
        self.per_run = max(1, per_run)
        self.clock = clock
        # Days start at local midnight (this machine's zone), as for videos.
        self.tz = tz or datetime.now().astimezone().tzinfo

    # --- order ------------------------------------------------------------- #
    def _graded_weak_tags(self) -> list[str]:
        return GradeStore(self._db).weak_tags(
            types_for(DESIGN), limit=WEAK_TAGS, ignore=NOT_SKILLS
        )

    def weak_tags(self) -> list[str]:
        try:
            return list(self._weak_tags())
        except Exception as exc:  # noqa: BLE001 - grades steer the order, never block it
            _LOG.warning("Weak design tags unavailable: %s", first_line(exc))
            return []

    def _upcoming(
        self, kind: str, rows: dict[str, DesignTopicRow], weak: Sequence[str]
    ) -> list[DesignTopic]:
        ordered = order_topics((t for t in self.topics if t.kind == kind), weak)
        fresh = [t for t in ordered if t.slug not in rows]
        failed = [t for t in ordered if t.slug in rows and rows[t.slug].state == FAILED]
        return fresh + failed

    def schedule(
        self, n: int, *, weak: Sequence[str] | None = None
    ) -> list[DesignTopic]:
        """The next ``n`` topics, in the order the days would bring them."""
        weak = self.weak_tags() if weak is None else weak
        rows = self.store.rows()
        pools = {kind: self._upcoming(kind, rows, weak) for kind in KINDS}
        pattern = [k for k in KINDS for _ in range(self.per_day[k])] or list(KINDS)
        out: list[DesignTopic] = []
        while len(out) < n and any(pools[kind] for kind in pattern):
            for kind in pattern:
                if pools[kind] and len(out) < n:
                    out.append(pools[kind].pop(0))
        return out

    # --- queueing ---------------------------------------------------------- #
    def today(self) -> str:
        return local_day(self.clock(), self.tz)

    def queue_today(self) -> int:
        """Queue what today's quota still lacks; return how many were queued."""
        now = self.clock()
        day = local_day(now, self.tz)
        have = self.store.queued_for(day)
        need = {kind: self.per_day[kind] - have[kind] for kind in KINDS}
        if all(count <= 0 for count in need.values()):
            return 0
        weak, rows = self.weak_tags(), self.store.rows()
        queued = 0
        for kind, count in need.items():
            for topic in self._upcoming(kind, rows, weak)[: max(0, count)]:
                queued += self.store.queue(topic, now=now, day=day)
        return queued

    def queue_topic(self, slug: str) -> DesignTopic:
        """Queue one topic by hand, written before or not. ``KeyError`` if
        there's no such topic."""
        topic = self.by_slug[slug]
        now = self.clock()
        self.store.queue(topic, now=now, day=local_day(now, self.tz), extra=True)
        return topic

    def queue_ahead(self, n: int) -> int:
        """Queue the next ``n`` topics now, as extras beyond the daily pace."""
        now = self.clock()
        day = local_day(now, self.tz)
        return sum(
            self.store.queue(topic, now=now, day=day, extra=True)
            for topic in self.schedule(n)
        )

    def put_cheat_sheet(self) -> bool:
        """Put the hand-written cheat sheet in (again only if its text changed)."""
        key = card_key(CHEAT_SHEET_SLUG)
        text = join_steps(CHEAT_SHEET_PROMPT, CHEAT_SHEET_STEPS)
        current = self.items.insights.get(key)
        if current is not None and current.text == text:
            return False
        item = PrepItem(
            key,
            DESIGN_TYPE,
            CHEAT_SHEET_TITLE,
            text,
            [CHEAT_SHEET_SLUG, *CHEAT_SHEET_TAGS],
        )
        self.items.put(SOURCE_ID, SOURCE_LABEL, [item], track=DESIGN, now=self.clock())
        return True

    # --- writing ----------------------------------------------------------- #
    async def run_once(
        self, *, limit: int | None = None, ahead: bool = False
    ) -> SyncReport:
        """Queue today's topics and write up to ``limit`` ready cards. With
        ``ahead``, the next topics are queued as extras to make up ``limit``."""
        report = SyncReport()
        self.put_cheat_sheet()
        report.queued = self.queue_today()
        limit = max(1, limit or self.per_run)
        if ahead:
            short = limit - self.store.ready_count(now=self.clock())
            if short > 0:
                report.queued += self.queue_ahead(short)
        for row in self.store.ready(now=self.clock(), limit=limit):
            await self._write(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def write_topic(self, slug: str) -> SyncReport:
        """Write one topic now, by hand. ``KeyError`` if there's no such topic."""
        self.queue_topic(slug)
        report = SyncReport(queued=1)
        row = self.store.row(slug)
        if row is not None:
            await self._write(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _write(self, row: DesignTopicRow, report: SyncReport) -> None:
        if not self.store.claim(row.id, now=self.clock()):
            return
        topic = self.by_slug.get(row.id)
        if topic is None:  # dropped from the list since it was queued
            self.store.skip(row.id)
            return
        try:
            card = await self.write_card(topic)
        except Exception as exc:  # noqa: BLE001 - one topic never stops the rest
            error = f"writing: {first_line(exc)}"
            if self.store.fail_attempt(row.id, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"{topic.slug}: {error}")
            _LOG.warning("Design topic %s: %s", topic.slug, error)
            return
        item = PrepItem(
            key=card_key(topic.slug),
            type=DESIGN_TYPE,
            title=card.title,
            text=card.text,
            tags=card_tags(topic),
        )
        self.items.put(SOURCE_ID, SOURCE_LABEL, [item], track=DESIGN, now=self.clock())
        self.store.finish(row.id)
        report.written += 1

    # --- reading ----------------------------------------------------------- #
    def progress(self, *, next_up: int = 4) -> DesignProgress:
        rows = self.store.rows()
        states = Counter(row.state for row in rows.values())
        weak = self.weak_tags()
        return DesignProgress(
            topics=len(self.topics),
            written=states[DONE],
            queued=states[PENDING] + states[WRITING],
            failed=[
                (slug, rows[slug].error)
                for slug in sorted(rows)
                if rows[slug].state == FAILED
            ],
            today=self.store.queued_for(self.today()),
            next_up=self.schedule(next_up, weak=weak),
            weak=weak,
            states={slug: row.state for slug, row in rows.items()},
        )


class WithBackground:
    """An ASGI app that runs ``sync`` passes (``sync_forever``) while ``app``
    is up: started and stopped with its lifespan. ``daily-agent web`` wraps
    the feed in it, so the design loop needs nothing from the web app."""

    def __init__(self, app: Any, sync: Syncer, *, idle: float = IDLE_SECONDS) -> None:
        self.app = app
        self.sync = sync
        self.idle = idle

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return
        task = asyncio.create_task(
            sync_forever(self.sync, asyncio.Event(), idle=self.idle)
        )
        try:
            await self.app(scope, receive, send)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
