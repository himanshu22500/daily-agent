"""Followed Instagram accounts and the feed stories written from them (#107).

Following an account records it in ``instagram_sources`` and queues its 5
newest posts in ``instagram_posts``; after that the account is re-checked
every few hours and each new post is queued. A queued post's images (up to 4:
carousel slides, a photo, or a video's cover frame) are downloaded and kept in
``instagram_images``, because Instagram's image URLs expire. Claude writes the
story from the caption and images (``agents.post_writer``), and it is stored as
an ordinary ``insights`` row:

- key ``ig:<shortcode>``, type ``instagram``, the account as its source (via
  ``source_session = "ig:<username>"``), ``captured_at`` = post time.
- status ``web-only``: the Telegram outbox only queues ``new`` insights.

Queueing, claiming and retries are shared with video stories
(``story_queue``); the web server runs ``InstagramSync.run_once`` in the same
kind of background loop, and ``daily-agent instagram sync`` runs it by hand.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Column, LargeBinary, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from ..models import Insight, VideoStory
from ..sources.instagram import (
    SHORTCODE,
    AccountFeed,
    BadSourceLink,
    InstagramError,
    MediaExpired,
    Post,
    PostImage,
    parse_username,
    post_url,
    profile_url,
)
from .insights_store import InsightStore
from .story_queue import (
    FAILED,
    PENDING,
    SKIPPED,
    StoryQueue,
    SyncReport,
    aware,
    first_line,
    iso,
    now_utc,
)

_LOG = logging.getLogger(__name__)

KEY_PREFIX = "ig:"
SOURCE_PREFIX = "ig:"
POST_TYPE = "instagram"
# Not "new": the Telegram outbox only queues new insights.
STORY_STATUS = "web-only"
STORY_SCORE = 0.5

INITIAL_POSTS = 5
CHECK_EVERY = timedelta(hours=3)
PER_RUN = 5


def post_key(shortcode: str) -> str:
    return KEY_PREFIX + shortcode


def shortcode_of(key: str) -> str:
    """The shortcode in an ``ig:<shortcode>`` story key ("" for anything else)."""
    if not key.startswith(KEY_PREFIX):
        return ""
    candidate = key[len(KEY_PREFIX) :]
    return candidate if SHORTCODE.fullmatch(candidate) else ""


def _caption_title(caption: str) -> str:
    """A short label for a post (the Sources page names failed posts by it)."""
    line = next((ln.strip() for ln in caption.splitlines() if ln.strip()), "")
    return line[:80] or "Untitled post"


class InstagramSourceRow(SQLModel, table=True):
    __tablename__ = "instagram_sources"

    id: str = Field(primary_key=True)  # "instagram:<username>"
    username: str
    url: str
    title: str = ""
    added_at: str
    removed_at: str | None = None
    checked_at: str | None = None  # last successful check; None = never
    check_error: str = ""


class InstagramPostRow(SQLModel, table=True):
    __tablename__ = "instagram_posts"

    shortcode: str = Field(primary_key=True)
    source_id: str
    username: str
    account_name: str = ""
    title: str = ""
    caption: str = ""
    media_type: str = ""
    product_type: str = ""
    permalink: str = ""
    image_urls: str = "[]"  # JSON list; they expire, refreshed on each check
    alt_texts: str = "[]"  # JSON list
    published_at: str
    found_at: str
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""

    def to_post(self) -> Post:
        return Post(
            shortcode=self.shortcode,
            username=self.username,
            caption=self.caption,
            media_type=self.media_type,
            product_type=self.product_type,
            permalink=self.permalink or post_url(self.shortcode),
            published_at=aware(self.published_at),
            image_urls=tuple(json.loads(self.image_urls or "[]")),
            alt_texts=tuple(json.loads(self.alt_texts or "[]")),
        )


class InstagramImageRow(SQLModel, table=True):
    __tablename__ = "instagram_images"

    shortcode: str = Field(primary_key=True)
    position: int = Field(primary_key=True)
    content_type: str
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))


@dataclass(frozen=True)
class PostMedia:
    """What the web feed shows for a post story."""

    shortcode: str
    username: str
    kind: str  # carousel, reel, video, photo
    images: int  # stored images (positions 0..images-1)
    url: str  # the post on Instagram

    @property
    def is_video(self) -> bool:
        return self.kind in ("reel", "video")

    def image_url(self, position: int = 0) -> str:
        return f"/ig/{self.shortcode}/{position}.jpg" if position < self.images else ""


class InstagramStore(StoryQueue):
    """The ``instagram_sources``, ``instagram_posts`` and ``instagram_images`` tables."""

    source_row = InstagramSourceRow
    item_row = InstagramPostRow
    item_id = "shortcode"
    platform = "instagram"

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(
            self._engine, InstagramSourceRow, InstagramPostRow, InstagramImageRow
        )

    # --- sources ---------------------------------------------------------- #
    def follow(self, username: str, title: str, *, now: datetime) -> str:
        """Add (or re-add) an account; a re-added one starts over as never checked."""
        source_id = f"instagram:{username}"
        stmt = sqlite_insert(InstagramSourceRow).values(
            id=source_id,
            username=username,
            url=profile_url(username),
            title=title,
            added_at=iso(now),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "title": title,
                "removed_at": None,
                "checked_at": None,
                "check_error": "",
            },
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)
        return source_id

    # --- posts ------------------------------------------------------------ #
    def record(
        self,
        source: InstagramSourceRow,
        feed: AccountFeed,
        *,
        first: bool,
        now: datetime,
    ) -> int:
        """Queue the account's new posts; return how many were queued.

        On an account's first check only the newest ``INITIAL_POSTS`` are
        queued and older ones are recorded as skipped, so they never count as
        new later. Posts still waiting get their (expiring) image URLs renewed.
        """
        posts = sorted(feed.posts, key=lambda p: p.published_at, reverse=True)
        wanted = {p.shortcode for p in posts[:INITIAL_POSTS]} if first else None
        name = feed.account.name or feed.account.username
        queued = 0
        with session_scope(self._engine) as session:
            known = {
                row.shortcode: row
                for row in session.exec(
                    select(InstagramPostRow).where(
                        InstagramPostRow.shortcode.in_([p.shortcode for p in posts])
                    )
                ).all()
            }
            for post in posts:
                take = wanted is None or post.shortcode in wanted
                row = known.get(post.shortcode)
                if row is not None:
                    if row.state in (PENDING, FAILED, SKIPPED):
                        row.image_urls = json.dumps(list(post.image_urls))
                    # Following again brings back recent posts that were
                    # dropped or failed earlier.
                    if first and take and row.state in (SKIPPED, FAILED):
                        row.state, row.attempts, row.error = PENDING, 0, ""
                        row.source_id, row.not_before = source.id, None
                        queued += 1
                    session.add(row)
                    continue
                session.add(
                    InstagramPostRow(
                        shortcode=post.shortcode,
                        source_id=source.id,
                        username=post.username,
                        account_name=name,
                        title=_caption_title(post.caption),
                        caption=post.caption,
                        media_type=post.media_type,
                        product_type=post.product_type,
                        permalink=post.permalink,
                        image_urls=json.dumps(list(post.image_urls)),
                        alt_texts=json.dumps(list(post.alt_texts)),
                        published_at=iso(post.published_at),
                        found_at=iso(now),
                        state=PENDING if take else SKIPPED,
                    )
                )
                queued += int(take)
        return queued

    def renew_urls(self, shortcode: str, post: Post) -> None:
        self._set(shortcode, image_urls=json.dumps(list(post.image_urls)))

    def skip(self, shortcode: str, reason: str) -> None:
        self._set(shortcode, state=SKIPPED, error=reason[:300], claimed_at=None)

    def account_names(self) -> dict[str, str]:
        """``{"ig:<username>": account name}`` for every story's source label."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(
                    InstagramPostRow.username, InstagramPostRow.account_name
                ).distinct()
            ).all()
        return {SOURCE_PREFIX + user: name or "@" + user for user, name in rows}

    # --- images ----------------------------------------------------------- #
    def save_images(self, shortcode: str, images: list[PostImage]) -> None:
        with session_scope(self._engine) as session:
            session.execute(
                delete(InstagramImageRow).where(
                    InstagramImageRow.shortcode == shortcode
                )
            )
            for position, image in enumerate(images):
                session.add(
                    InstagramImageRow(
                        shortcode=shortcode,
                        position=position,
                        content_type=image.content_type,
                        data=image.data,
                    )
                )

    def image(self, shortcode: str, position: int) -> PostImage | None:
        with session_scope(self._engine) as session:
            row = session.get(InstagramImageRow, (shortcode, position))
            if row is None:
                return None
            return PostImage(content_type=row.content_type, data=row.data)

    def media(self, shortcode: str) -> PostMedia | None:
        with session_scope(self._engine) as session:
            row = session.get(InstagramPostRow, shortcode)
            if row is None:
                return None
            count = session.scalar(
                select(func.count())
                .select_from(InstagramImageRow)
                .where(InstagramImageRow.shortcode == shortcode)
            )
            post = row.to_post()
        return PostMedia(
            shortcode=shortcode,
            username=post.username,
            kind=post.kind,
            images=count or 0,
            url=post.permalink,
        )


# (post, account name, its images) -> the story
PostWriter = Callable[[Post, str, list[PostImage]], Awaitable[VideoStory]]


class InstagramSync:
    """Follow accounts and turn their new posts into feed stories."""

    def __init__(
        self,
        db_path: str | Path,
        instagram,
        write_story: PostWriter,
        *,
        check_every: timedelta = CHECK_EVERY,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = InstagramStore(db_path)
        self.insights = InsightStore(db_path)
        self.instagram = instagram
        self.write_story = write_story
        self.check_every = check_every
        self.per_run = max(1, per_run)
        self.clock = clock

    async def follow(self, link: str) -> InstagramSourceRow:
        """Follow a pasted profile link and queue its latest posts.

        Raises ``BadSourceLink`` (tell the reader) or ``InstagramError`` (try
        again later, or reconnect). Nothing is written to the feed here.
        """
        username = parse_username(link)
        feed = await self.instagram.account(username)
        now = self.clock()
        source_id = self.store.follow(feed.account.username, feed.account.name, now=now)
        source = self.store.source(source_id)
        assert source is not None
        self.store.record(source, feed, first=True, now=now)
        self.store.mark_checked(source_id, now=now)
        return source

    def unfollow(self, source_id: str) -> bool:
        return self.store.unfollow(source_id, now=self.clock())

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Check the accounts that are due, then write up to ``limit`` posts."""
        report = SyncReport()
        now = self.clock()
        for source in self.store.active_sources():
            if source.checked_at and now - aware(source.checked_at) < self.check_every:
                continue
            await self._check(source, report)
        budget = limit or self.per_run
        for row in self.store.ready(now=self.clock(), limit=budget):
            await self._write(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _check(self, source: InstagramSourceRow, report: SyncReport) -> None:
        try:
            feed = await self.instagram.account(source.username)
        except (BadSourceLink, InstagramError) as exc:
            self.store.mark_checked(source.id, now=self.clock(), error=str(exc))
            report.errors.append(f"{source.title or source.id}: {exc}")
            return
        report.checked += 1
        report.queued += self.store.record(
            source, feed, first=source.checked_at is None, now=self.clock()
        )
        self.store.mark_checked(source.id, now=self.clock())

    async def _images(self, post: Post) -> list[PostImage]:
        """The post's images; expired URLs are renewed by re-reading the account."""
        try:
            return await self.instagram.images(post)
        except MediaExpired:
            pass
        feed = await self.instagram.account(post.username)
        fresh = next((p for p in feed.posts if p.shortcode == post.shortcode), None)
        if fresh is None:  # no longer among the latest posts: write without images
            return []
        self.store.renew_urls(post.shortcode, fresh)
        try:
            return await self.instagram.images(fresh)
        except MediaExpired:
            return []

    async def _write(self, row: InstagramPostRow, report: SyncReport) -> None:
        if not self.store.claim(row.shortcode, now=self.clock()):
            return
        post = row.to_post()
        try:
            images = await self._images(post)
        except (BadSourceLink, InstagramError) as exc:
            self._failed(row, f"images: {exc}", report)
            return
        if not post.caption and not images:
            self.store.skip(row.shortcode, "no caption and no images to write from")
            return
        try:
            story = await self.write_story(post, row.account_name, images)
        except Exception as exc:  # noqa: BLE001 - one post never stops the rest
            self._failed(row, f"writing: {first_line(exc)}", report)
            return
        self.store.save_images(row.shortcode, images)
        self.insights.add(
            Insight(
                key=post_key(row.shortcode),
                text=story.text,
                title=story.headline,
                type=POST_TYPE,
                tags=story.tags,
                score=STORY_SCORE,
                source_session=SOURCE_PREFIX + row.username,
                captured_at=post.published_at,
                status=STORY_STATUS,
            )
        )
        self.store.finish(row.shortcode)
        report.written += 1

    def _failed(self, row: InstagramPostRow, error: str, report: SyncReport) -> None:
        gave_up = self.store.fail_attempt(row.shortcode, error, now=self.clock())
        report.errors.append(f"{row.title or row.shortcode}: {error}")
        if gave_up:
            report.failed += 1
        _LOG.warning("Instagram post %s: %s", row.shortcode, error)
