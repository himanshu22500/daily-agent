"""Followed YouTube channels and the feed stories written from them (issue #99).

Following a channel (or playlist) records it in ``video_sources``. Its 5 most
recent uploads are queued in ``videos`` right away; after that the source is
re-checked every few hours and each new upload is queued. On a day a source
brings nothing new, it brings one older video instead: the newest one not yet
taken, so the 6th, then the 7th, and so on back through the channel (issue
#124; see ``VideoSync._backfill``). A queued video is
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
from contextlib import aclosing
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from sqlalchemy import update
from sqlmodel import Field, SQLModel, func, select

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
    VideoUnavailable,
    YouTubeError,
    uploads_list,
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
    FollowedSources,
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
# Going back (issue #124): older videos tried in one check when the ones found
# are private or removed; the next check goes on from there.
BACKFILL_TRIES = 3
# A skipped video with this error is never taken from the backlog.
UNAVAILABLE = "unavailable: "


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
    # When the video was found, or taken from the backlog (issue #124).
    found_at: str
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""


class VideoStore(FollowedSources):
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

    # --- going back (issue #124) ------------------------------------------- #
    def queued_since(self, source_id: str, since: datetime) -> bool:
        """Whether the source queued anything (new, initial, or older) since then."""
        stmt = (
            select(func.count())
            .select_from(VideoRow)
            .where(VideoRow.source_id == source_id)
            .where(VideoRow.state != SKIPPED)
            .where(VideoRow.found_at >= _iso(since))
        )
        with session_scope(self._engine) as session:
            return bool(session.scalar(stmt))

    def backlog(self, video_ids: list[str]) -> list[tuple[str, VideoRow | None]]:
        """The ids not taken yet, in order, with their row if one exists.

        Not taken: never seen, or skipped (older than a source's first five, or
        dropped on unfollow), unless it was found to be unavailable.
        """
        with session_scope(self._engine) as session:
            known = {
                row.video_id: row
                for row in session.exec(
                    select(VideoRow).where(VideoRow.video_id.in_(video_ids))
                ).all()
            }
        out = []
        for video_id in video_ids:
            row = known.get(video_id)
            if row is None or (
                row.state == SKIPPED and not row.error.startswith(UNAVAILABLE)
            ):
                out.append((video_id, row))
        return out

    def take(
        self,
        source: VideoSourceRow,
        video: Video | None,
        video_id: str,
        *,
        now: datetime,
    ) -> None:
        """Queue an older video: a skipped row, or ``video`` read from its page."""
        if video is None:
            with session_scope(self._engine) as session:
                session.execute(
                    update(VideoRow)
                    .where(VideoRow.video_id == video_id)
                    .where(VideoRow.state == SKIPPED)
                    .values(
                        state=PENDING,
                        attempts=0,
                        error="",
                        not_before=None,
                        source_id=source.id,
                        found_at=_iso(now),
                    )
                )
            return
        self._insert(
            video_id=video.video_id,
            source_id=source.id,
            channel_id=video.channel_id,
            channel=video.channel or source.title,
            title=video.title,
            description=video.description,
            published_at=_iso(video.published_at),
            found_at=_iso(now),
            state=PENDING,
        )

    def skip_unavailable(
        self, source: VideoSourceRow, video_id: str, reason: str, *, now: datetime
    ) -> None:
        """Remember a private or removed video so it's never tried again."""
        self._insert(
            video_id=video_id,
            source_id=source.id,
            published_at=_iso(now),
            found_at=_iso(now),
            state=SKIPPED,
            error=(UNAVAILABLE + reason)[:300],
        )

    def _insert(self, **values) -> None:
        stmt = conflict_insert(self._engine, VideoRow).values(**values)
        with session_scope(self._engine) as session:
            session.execute(stmt.on_conflict_do_nothing(index_elements=["video_id"]))

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
        backfill: bool = True,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = VideoStore(db_path)
        self.insights = InsightStore(db_path)
        self.youtube = youtube
        self.write_story = write_story
        self.check_every = check_every
        self.per_run = max(1, per_run)
        self.backfill = backfill
        # Days for going back start at local midnight (this machine's zone).
        self.tz = tz or datetime.now().astimezone().tzinfo
        self.clock = clock
        # Sources whose backlog ran out, and the day it did (tried again the
        # next day; the list may have grown).
        self._backlog_done: dict[str, date] = {}

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
            checked = await self._check(source, report)
            if self.backfill and (checked or source.checked_at):
                await self._backfill(source, report)
        budget = limit or self.per_run
        for video in self.store.ready(now=self.clock(), limit=budget):
            await self._write(video, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _check(self, source: VideoSourceRow, report: SyncReport) -> bool:
        try:
            feed = await self.youtube.feed(source.feed_url)
        except (BadSourceLink, YouTubeError) as exc:
            self.store.mark_checked(source.id, now=self.clock(), error=str(exc))
            report.errors.append(f"{source.title or source.id}: {exc}")
            return False
        report.checked += 1
        report.queued += self.store.record(
            source, feed, first=source.checked_at is None, now=self.clock()
        )
        self.store.mark_checked(source.id, now=self.clock(), title=feed.title)
        return True

    async def _backfill(self, source: VideoSourceRow, report: SyncReport) -> None:
        """On a day the source queued nothing, queue its next older video.

        "Next older" is the first video in the source's list (a channel's
        long-form uploads, newest first, or the playlist) that hasn't been
        taken: the 6th after the first five, then the 7th, and so on. Runs
        after each check, so at most once every few hours, and only while
        nothing from the source was queued today (local time): a day with a new
        upload brings no older video. A source never checked successfully
        waits, so its first check can queue the latest five first.
        """
        now = self.clock()
        today = now.astimezone(self.tz).date()
        midnight = now.astimezone(self.tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if self._backlog_done.get(source.id) == today:
            return
        if self.store.queued_since(source.id, midnight):
            return
        try:
            list_id = uploads_list(source.id)
        except ValueError:
            return
        tries = 0
        try:
            async with aclosing(self.youtube.list_pages(list_id)) as pages:
                async for page in pages:
                    for video_id, row in self.store.backlog(page):
                        if tries == BACKFILL_TRIES:
                            return
                        tries += 1
                        if await self._take(source, video_id, row, report):
                            return
        except (BadSourceLink, YouTubeError) as exc:
            report.errors.append(f"{source.title or source.id}: older videos: {exc}")
            _LOG.warning("Older videos of %s: %s", source.id, exc)
            return
        self._backlog_done[source.id] = today

    async def _take(
        self,
        source: VideoSourceRow,
        video_id: str,
        row: VideoRow | None,
        report: SyncReport,
    ) -> bool:
        """Queue one older video; False if it turned out to be unavailable."""
        video = None
        if row is None:  # older than the RSS feed reaches: read its page
            try:
                video = await self.youtube.video(video_id)
            except VideoUnavailable as exc:
                self.store.skip_unavailable(
                    source, video_id, str(exc), now=self.clock()
                )
                return False
        self.store.take(source, video, video_id, now=self.clock())
        report.queued += 1
        report.older += 1
        return True

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
