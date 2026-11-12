"""Followed YouTube channels and the feed stories written from them (issue #99).

Following a channel (or playlist) records it in ``video_sources``. Its 5 most
recent uploads are queued in ``videos`` right away; after that the source is
re-checked every few hours and each new upload is queued. A queued video is
written up by Claude from its captions (``agents.video_writer``) and stored as
an ordinary ``insights`` row, so it shows in For you, Latest, search, Saved,
and Read next like everything else:

- key ``video:<video id>``, type ``video``, the channel as its source (via
  ``source_session = "yt:<channel id>"``), ``captured_at`` = upload time.
- status ``web-only``: the Telegram outbox only queues ``new`` insights, so
  video stories never reach Telegram.

``VideoSync.run_once`` does one pass (check due sources, then write a few
queued videos); the web server runs it in a background loop and ``daily-agent
videos sync`` runs it by hand. Two runners never write the same video: a video
is claimed with a conditional update before any work starts, and a claim left
by a crashed runner expires.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from ..models import Insight, VideoStory
from ..sources.youtube import (
    VIDEO_ID,
    BadSourceLink,
    ChannelFeed,
    NoCaptions,
    SourceRef,
    Transcript,
    Video,
    YouTubeError,
)
from .insights_store import InsightStore

_LOG = logging.getLogger(__name__)

KEY_PREFIX = "video:"
SOURCE_PREFIX = "yt:"
VIDEO_TYPE = "video"
# Not "new": the Telegram outbox only queues new insights.
STORY_STATUS = "web-only"
STORY_SCORE = 0.5

INITIAL_VIDEOS = 5
CHECK_EVERY = timedelta(hours=3)
PER_RUN = 5
# Auto-captions usually appear within a few hours of upload; until then a
# video without captions waits instead of being written from its description.
CAPTION_WAIT = timedelta(hours=6)
CAPTION_RETRY = timedelta(hours=1)
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


def video_key(video_id: str) -> str:
    return KEY_PREFIX + video_id


def video_id_of(key: str) -> str:
    """The YouTube id in a ``video:<id>`` story key ("" for anything else)."""
    if not key.startswith(KEY_PREFIX):
        return ""
    candidate = key[len(KEY_PREFIX) :]
    return candidate if VIDEO_ID.fullmatch(candidate) else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class VideoSourceRow(SQLModel, table=True):
    __tablename__ = "video_sources"

    id: str = Field(primary_key=True)  # "channel:UC…" or "playlist:PL…"
    feed_url: str
    url: str
    title: str = ""
    added_at: str
    removed_at: str | None = None
    checked_at: str | None = None  # last successful check; None = never
    check_error: str = ""


class VideoRow(SQLModel, table=True):
    __tablename__ = "videos"

    video_id: str = Field(primary_key=True)
    source_id: str
    channel_id: str = ""
    channel: str = ""
    title: str = ""
    description: str = ""
    published_at: str
    found_at: str
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""


@dataclass(frozen=True)
class SourceStatus:
    """One followed source, as the Sources page and ``videos list`` show it."""

    id: str
    title: str
    url: str
    added_at: datetime
    checked_at: datetime | None
    check_error: str
    stories: int
    queued: int
    writing: int
    failed: list[tuple[str, str]]  # (video title, error)


class VideoStore:
    """The ``video_sources`` and ``videos`` tables."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, VideoSourceRow, VideoRow)

    # --- sources ---------------------------------------------------------- #
    def follow(self, ref: SourceRef, title: str, *, now: datetime) -> None:
        """Add (or re-add) a source; a re-added one starts over as never checked."""
        stmt = sqlite_insert(VideoSourceRow).values(
            id=ref.id,
            feed_url=ref.feed_url,
            url=ref.url,
            title=title,
            added_at=_iso(now),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "feed_url": ref.feed_url,
                "url": ref.url,
                "title": title,
                "removed_at": None,
                "checked_at": None,
                "check_error": "",
            },
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

    def unfollow(self, source_id: str, *, now: datetime) -> bool:
        """Stop following; queued videos are dropped, written stories stay."""
        with session_scope(self._engine) as session:
            row = session.get(VideoSourceRow, source_id)
            if row is None or row.removed_at:
                return False
            row.removed_at = _iso(now)
            session.add(row)
            session.execute(
                update(VideoRow)
                .where(VideoRow.source_id == source_id)
                .where(VideoRow.state.in_((PENDING, FAILED)))
                .values(state=SKIPPED)
            )
            return True

    def source(self, source_id: str) -> VideoSourceRow | None:
        with session_scope(self._engine) as session:
            return session.get(VideoSourceRow, source_id)

    def active_sources(self) -> list[VideoSourceRow]:
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(VideoSourceRow)
                    .where(VideoSourceRow.removed_at.is_(None))
                    .order_by(VideoSourceRow.added_at)
                ).all()
            )

    def mark_checked(
        self, source_id: str, *, now: datetime, error: str = "", title: str = ""
    ) -> None:
        """Record a check; ``title`` names a source that has no name yet."""
        with session_scope(self._engine) as session:
            row = session.get(VideoSourceRow, source_id)
            if row is None:
                return
            if error:
                row.check_error = error[:300]
            else:
                row.checked_at = _iso(now)
                row.check_error = ""
            if title and not row.title:
                row.title = title
            session.add(row)

    def statuses(self) -> list[SourceStatus]:
        sources = self.active_sources()
        with session_scope(self._engine) as session:
            videos = session.exec(
                select(VideoRow).where(VideoRow.source_id.in_([s.id for s in sources]))
            ).all()
        by_source: dict[str, list[VideoRow]] = {}
        for video in videos:
            by_source.setdefault(video.source_id, []).append(video)
        out = []
        for source in sources:
            rows = by_source.get(source.id, [])
            states = Counter(v.state for v in rows)
            out.append(
                SourceStatus(
                    id=source.id,
                    title=source.title or source.id,
                    url=source.url,
                    added_at=_aware(source.added_at),
                    checked_at=_aware(source.checked_at) if source.checked_at else None,
                    check_error=source.check_error,
                    stories=states[DONE],
                    queued=states[PENDING],
                    writing=states[WRITING],
                    failed=[(v.title, v.error) for v in rows if v.state == FAILED],
                )
            )
        return out

    # --- videos ------------------------------------------------------------ #
    def record(
        self, source: VideoSourceRow, feed: ChannelFeed, *, first: bool, now: datetime
    ) -> int:
        """Queue the feed's new videos; return how many were queued.

        On a source's first check only the newest ``INITIAL_VIDEOS`` are queued
        and the older ones are recorded as skipped, so they never count as new
        later. Shorts are always skipped.
        """
        videos = sorted(feed.videos, key=lambda v: v.published_at, reverse=True)
        full = [v for v in videos if not v.short]
        wanted = {v.video_id for v in full[:INITIAL_VIDEOS]} if first else None
        queued = 0
        with session_scope(self._engine) as session:
            known = {
                row.video_id: row
                for row in session.exec(
                    select(VideoRow).where(
                        VideoRow.video_id.in_([v.video_id for v in videos])
                    )
                ).all()
            }
            for video in videos:
                take = not video.short and (wanted is None or video.video_id in wanted)
                row = known.get(video.video_id)
                if row is not None:
                    # Following again brings back recent videos that were
                    # dropped or failed earlier.
                    if first and take and row.state in (SKIPPED, FAILED):
                        row.state, row.attempts, row.error = PENDING, 0, ""
                        row.source_id, row.not_before = source.id, None
                        session.add(row)
                        queued += 1
                    continue
                session.add(
                    VideoRow(
                        video_id=video.video_id,
                        source_id=source.id,
                        channel_id=video.channel_id,
                        channel=video.channel or feed.title,
                        title=video.title,
                        description=video.description,
                        published_at=_iso(video.published_at),
                        found_at=_iso(now),
                        state=PENDING if take else SKIPPED,
                    )
                )
                queued += int(take)
        return queued

    def ready(self, *, now: datetime, limit: int) -> list[VideoRow]:
        """Queued videos that may be worked on now, newest upload first."""
        stamp = _iso(now)
        stmt = (
            select(VideoRow)
            .where(self._claimable(stamp, _iso(now - CLAIM_TTL)))
            .order_by(VideoRow.published_at.desc())
            .limit(limit)
        )
        with session_scope(self._engine) as session:
            return list(session.exec(stmt).all())

    def ready_count(self, *, now: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(VideoRow)
            .where(self._claimable(_iso(now), _iso(now - CLAIM_TTL)))
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    @staticmethod
    def _claimable(stamp: str, stale: str):
        return or_(
            and_(
                VideoRow.state == PENDING,
                or_(VideoRow.not_before.is_(None), VideoRow.not_before <= stamp),
            ),
            and_(VideoRow.state == WRITING, VideoRow.claimed_at < stale),
        )

    def claim(self, video_id: str, *, now: datetime) -> bool:
        """Take a video to work on; False if another runner has it (or it's done)."""
        stamp = _iso(now)
        with session_scope(self._engine) as session:
            result = session.execute(
                update(VideoRow)
                .where(VideoRow.video_id == video_id)
                .where(self._claimable(stamp, _iso(now - CLAIM_TTL)))
                .values(state=WRITING, claimed_at=stamp)
            )
            return result.rowcount == 1

    def finish(self, video_id: str) -> None:
        self._set(video_id, state=DONE, error="", not_before=None, claimed_at=None)

    def defer(self, video_id: str, until: datetime, *, error: str = "") -> None:
        """Back to the queue, not before ``until`` (no attempt counted)."""
        self._set(
            video_id,
            state=PENDING,
            not_before=_iso(until),
            claimed_at=None,
            error=error[:300],
        )

    def fail_attempt(self, video_id: str, error: str, *, now: datetime) -> bool:
        """Count a failed attempt; retry later or give up. True if given up."""
        with session_scope(self._engine) as session:
            row = session.get(VideoRow, video_id)
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
                row.not_before = _iso(now + RETRY_BASE * 2 ** (row.attempts - 1))
            session.add(row)
            return row.state == FAILED

    def _set(self, video_id: str, **values) -> None:
        with session_scope(self._engine) as session:
            session.execute(
                update(VideoRow).where(VideoRow.video_id == video_id).values(**values)
            )

    def channel_names(self) -> dict[str, str]:
        """``{"yt:<channel id>": channel name}`` for every story's source label."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(VideoRow.channel_id, VideoRow.channel).where(
                    VideoRow.channel_id != ""
                )
            ).all()
        return {SOURCE_PREFIX + cid: name for cid, name in rows if name}


# (video, its captions or None when it has none) -> the story
StoryWriter = Callable[[Video, "Transcript | None"], Awaitable[VideoStory]]


@dataclass
class SyncReport:
    checked: int = 0
    queued: int = 0
    written: int = 0
    waiting: int = 0  # no captions yet; retried later
    failed: int = 0
    # Videos ready to write that this pass left for the next one.
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


class VideoSync:
    """Follow sources and turn their new videos into feed stories."""

    def __init__(
        self,
        db_path: str | Path,
        youtube,
        write_story: StoryWriter,
        *,
        check_every: timedelta = CHECK_EVERY,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = VideoStore(db_path)
        self.insights = InsightStore(db_path)
        self.youtube = youtube
        self.write_story = write_story
        self.check_every = check_every
        self.per_run = max(1, per_run)
        self.clock = clock

    async def follow(self, link: str) -> VideoSourceRow:
        """Follow a pasted link and queue its latest videos.

        YouTube's RSS server has outages that last hours (issue #103). If it
        doesn't answer, the source is still followed once its own page shows it
        exists, and stays unchecked: every ``run_once`` retries it, and the
        first check that gets through queues its latest videos.

        Raises ``BadSourceLink`` (tell the reader) or ``YouTubeError`` (try
        again later). Nothing is written to the feed here; that's ``run_once``.
        """
        ref = await self.youtube.resolve(link)
        try:
            feed = await self.youtube.feed(ref.feed_url)
        except YouTubeError as exc:
            title = await self.youtube.page_title(ref)
            now = self.clock()
            self.store.follow(ref, title, now=now)
            self.store.mark_checked(ref.id, now=now, error=str(exc))
            source = self.store.source(ref.id)
            assert source is not None
            return source
        now = self.clock()
        self.store.follow(ref, feed.title, now=now)
        source = self.store.source(ref.id)
        assert source is not None
        self.store.record(source, feed, first=True, now=now)
        self.store.mark_checked(ref.id, now=now)
        return source

    def unfollow(self, source_id: str) -> bool:
        return self.store.unfollow(source_id, now=self.clock())

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Check the sources that are due, then write up to ``limit`` videos."""
        report = SyncReport()
        now = self.clock()
        for source in self.store.active_sources():
            if source.checked_at and now - _aware(source.checked_at) < self.check_every:
                continue
            await self._check(source, report)
        budget = limit or self.per_run
        for video in self.store.ready(now=self.clock(), limit=budget):
            await self._write(video, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _check(self, source: VideoSourceRow, report: SyncReport) -> None:
        try:
            feed = await self.youtube.feed(source.feed_url)
        except (BadSourceLink, YouTubeError) as exc:
            self.store.mark_checked(source.id, now=self.clock(), error=str(exc))
            report.errors.append(f"{source.title or source.id}: {exc}")
            return
        report.checked += 1
        report.queued += self.store.record(
            source, feed, first=source.checked_at is None, now=self.clock()
        )
        self.store.mark_checked(source.id, now=self.clock(), title=feed.title)

    async def _write(self, row: VideoRow, report: SyncReport) -> None:
        now = self.clock()
        if not self.store.claim(row.video_id, now=now):
            return
        video = Video(
            video_id=row.video_id,
            title=row.title,
            published_at=_aware(row.published_at),
            description=row.description,
            channel_id=row.channel_id,
            channel=row.channel,
        )
        try:
            transcript: Transcript | None = await self.youtube.transcript(row.video_id)
        except NoCaptions as exc:
            if now - video.published_at < CAPTION_WAIT:
                self.store.defer(
                    row.video_id, now + CAPTION_RETRY, error=f"no captions yet: {exc}"
                )
                report.waiting += 1
                return
            transcript = None  # written from the title and description
        except YouTubeError as exc:
            self._failed(row, f"captions: {exc}", report)
            return
        try:
            story = await self.write_story(video, transcript)
        except Exception as exc:  # noqa: BLE001 - one video never stops the rest
            self._failed(row, f"writing: {_first_line(exc)}", report)
            return
        self.insights.add(
            Insight(
                key=video_key(row.video_id),
                text=story.text,
                title=story.headline,
                type=VIDEO_TYPE,
                tags=story.tags,
                score=STORY_SCORE,
                source_session=SOURCE_PREFIX + (row.channel_id or row.source_id),
                captured_at=video.published_at,
                status=STORY_STATUS,
            )
        )
        self.store.finish(row.video_id)
        report.written += 1

    def _failed(self, row: VideoRow, error: str, report: SyncReport) -> None:
        gave_up = self.store.fail_attempt(row.video_id, error, now=self.clock())
        report.errors.append(f"{row.title or row.video_id}: {error}")
        if gave_up:
            report.failed += 1
        _LOG.warning("Video %s: %s", row.video_id, error)


async def sync_forever(
    sync: VideoSync, wake: asyncio.Event, *, idle: float = 600.0
) -> None:
    """Run sync passes until cancelled (the web server's background loop).

    After a pass that wrote something, the next starts right away while more
    videos are ready; otherwise it waits ``idle`` seconds, or until ``wake`` is
    set (a source was just followed).
    """
    while True:
        wake.clear()
        try:
            report = await sync.run_once()
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            _LOG.exception("Video sync pass failed")
            report = None
        if report is not None and report.written and report.remaining:
            continue
        try:
            await asyncio.wait_for(wake.wait(), idle)
        except asyncio.TimeoutError:
            pass


def _first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return (lines[0] if lines else type(exc).__name__)[:200]
