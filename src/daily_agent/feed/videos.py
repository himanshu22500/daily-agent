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

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
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
from .story_queue import (  # noqa: F401 - re-exported for callers and tests
    CLAIM_TTL,
    DONE,
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    RETRY_BASE,
    SKIPPED,
    WRITING,
    SourceStatus,
    StoryQueue,
    SyncReport,
    sync_forever,
)
from .story_queue import aware as _aware
from .story_queue import first_line as _first_line
from .story_queue import iso as _iso
from .story_queue import now_utc as _now

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


def video_key(video_id: str) -> str:
    return KEY_PREFIX + video_id


def video_id_of(key: str) -> str:
    """The YouTube id in a ``video:<id>`` story key ("" for anything else)."""
    if not key.startswith(KEY_PREFIX):
        return ""
    candidate = key[len(KEY_PREFIX) :]
    return candidate if VIDEO_ID.fullmatch(candidate) else ""


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


class VideoStore(StoryQueue):
    """The ``video_sources`` and ``videos`` tables."""

    source_row = VideoSourceRow
    item_row = VideoRow
    item_id = "video_id"
    platform = "youtube"

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, VideoSourceRow, VideoRow)

    # --- sources ---------------------------------------------------------- #
    def follow(self, ref: SourceRef, title: str, *, now: datetime) -> None:
        """Add (or re-add) a source; a re-added one starts over as never checked."""
        stmt = conflict_insert(self._engine, VideoSourceRow).values(
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
