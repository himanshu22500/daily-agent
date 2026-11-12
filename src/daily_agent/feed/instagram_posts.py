"""Followed Instagram creators and the feed stories written from their posts
(issue #105).

Following a creator records them in ``instagram_sources``; nothing is fetched
then. The background loop checks each creator every few hours with logged-out
headless Chrome (``sources.instagram``): their profile lists the latest posts,
and only posts not seen before are opened. On a creator's first check the
first ``FIRST_LOOK`` grid posts are opened (pinned posts come first and can be
old) and the newest ``INITIAL_POSTS`` are queued. Later checks queue each new
post. Each queued post's image is stored, since Instagram's image links
expire.

A queued post is written up by Claude from its caption (``agents.post_writer``)
and stored as an ordinary ``insights`` row, like video stories (#99):

- key ``instagram:<shortcode>``, type ``instagram``, the creator as its source
  (``source_session = "ig:<username>"``), ``captured_at`` = when it was posted.
- status ``web-only``, so it never reaches Telegram.

If Instagram shows a login wall, all Instagram checks pause for a few hours, so
this Mac doesn't keep knocking.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Column, LargeBinary, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from ..db import create_tables, make_engine, session_scope
from ..models import Insight, VideoStory
from ..sources.instagram import (
    SHORTCODE,
    AccountUnavailable,
    InstagramBlocked,
    InstagramError,
    Post,
    parse_profile_link,
    profile_url,
)
from .insights_store import InsightStore
from .story_queue import (
    DONE,
    FAILED,
    PENDING,
    SKIPPED,
    WRITING,
    SourceStatus,
    StoryQueue,
    SyncReport,
    aware,
    first_line,
    iso,
    now_utc,
)

_LOG = logging.getLogger(__name__)

KEY_PREFIX = "instagram:"
SOURCE_ID_PREFIX = "instagram:"
SOURCE_PREFIX = "ig:"
POST_TYPE = "instagram"
STORY_STATUS = "web-only"
STORY_SCORE = 0.5

INITIAL_POSTS = 5
FIRST_LOOK = 8
MAX_NEW_PER_CHECK = 6
CHECK_EVERY = timedelta(hours=6)
PER_RUN = 5
# A post older than this before the follow is an old one newly pinned, not new.
NEW_POST_SLACK = timedelta(days=1)
# Waits before checking again after a failed check.
RETRY_CHECK = timedelta(hours=1)
UNAVAILABLE_RETRY = timedelta(hours=24)
BLOCKED_PAUSE = timedelta(hours=3)


def post_key(code: str) -> str:
    return KEY_PREFIX + code


def code_of(key: str) -> str:
    """The shortcode in an ``instagram:<code>`` story key ("" for anything else)."""
    if not key.startswith(KEY_PREFIX):
        return ""
    candidate = key[len(KEY_PREFIX) :]
    return candidate if SHORTCODE.fullmatch(candidate) else ""


def source_id(username: str) -> str:
    return SOURCE_ID_PREFIX + username


class InstagramSourceRow(SQLModel, table=True):
    __tablename__ = "instagram_sources"

    id: str = Field(primary_key=True)  # "instagram:<username>"
    username: str
    title: str = ""  # the creator's name, once checked
    added_at: str
    removed_at: str | None = None
    checked_at: str | None = None  # last successful check; None = never
    next_check_at: str | None = None  # None = as soon as possible
    check_error: str = ""


class InstagramPostRow(SQLModel, table=True):
    __tablename__ = "instagram_posts"

    code: str = Field(primary_key=True)
    source_id: str
    username: str = ""
    author: str = ""
    kind: str = ""
    caption: str = ""
    image_description: str = ""
    # When it was posted; None for grid posts never opened (recorded as skipped).
    published_at: str | None = None
    found_at: str
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""


class InstagramImageRow(SQLModel, table=True):
    __tablename__ = "instagram_images"

    code: str = Field(primary_key=True)
    content_type: str
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    fetched_at: str


class InstagramStore:
    """The ``instagram_sources``, ``instagram_posts``, and ``instagram_images``
    tables."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(
            self._engine, InstagramSourceRow, InstagramPostRow, InstagramImageRow
        )
        self._queue = StoryQueue(self._engine, InstagramPostRow, "code")

    # --- sources ---------------------------------------------------------- #
    def follow(self, username: str, *, now: datetime) -> None:
        """Add (or re-add) a creator; a re-added one starts over as never checked."""
        stmt = sqlite_insert(InstagramSourceRow).values(
            id=source_id(username), username=username, added_at=iso(now)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "removed_at": None,
                "checked_at": None,
                "next_check_at": None,
                "check_error": "",
            },
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

    def unfollow(self, source: str, *, now: datetime) -> bool:
        """Stop following; queued posts are dropped, written stories stay."""
        with session_scope(self._engine) as session:
            row = session.get(InstagramSourceRow, source)
            if row is None or row.removed_at:
                return False
            row.removed_at = iso(now)
            session.add(row)
            session.execute(
                update(InstagramPostRow)
                .where(InstagramPostRow.source_id == source)
                .where(InstagramPostRow.state.in_((PENDING, FAILED)))
                .values(state=SKIPPED)
            )
            return True

    def source(self, source: str) -> InstagramSourceRow | None:
        with session_scope(self._engine) as session:
            return session.get(InstagramSourceRow, source)

    def active_sources(self) -> list[InstagramSourceRow]:
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(InstagramSourceRow)
                    .where(InstagramSourceRow.removed_at.is_(None))
                    .order_by(InstagramSourceRow.added_at)
                ).all()
            )

    def due_sources(self, *, now: datetime) -> list[InstagramSourceRow]:
        stamp = iso(now)
        return [
            s
            for s in self.active_sources()
            if s.next_check_at is None or s.next_check_at <= stamp
        ]

    def mark_checked(
        self, source: str, *, now: datetime, next_at: datetime, title: str = ""
    ) -> None:
        with session_scope(self._engine) as session:
            row = session.get(InstagramSourceRow, source)
            if row is None:
                return
            row.checked_at = iso(now)
            row.next_check_at = iso(next_at)
            row.check_error = ""
            if title:
                row.title = title
            session.add(row)

    def mark_failed(self, source: str, error: str, *, retry_at: datetime) -> None:
        with session_scope(self._engine) as session:
            row = session.get(InstagramSourceRow, source)
            if row is None:
                return
            row.check_error = error[:300]
            row.next_check_at = iso(retry_at)
            session.add(row)

    def statuses(self) -> list[SourceStatus]:
        sources = self.active_sources()
        with session_scope(self._engine) as session:
            posts = session.exec(
                select(InstagramPostRow).where(
                    InstagramPostRow.source_id.in_([s.id for s in sources])
                )
            ).all()
        by_source: dict[str, list[InstagramPostRow]] = {}
        for post in posts:
            by_source.setdefault(post.source_id, []).append(post)
        out = []
        for source in sources:
            rows = by_source.get(source.id, [])
            states = Counter(p.state for p in rows)
            out.append(
                SourceStatus(
                    id=source.id,
                    title=source.title or f"@{source.username}",
                    url=profile_url(source.username),
                    added_at=aware(source.added_at),
                    checked_at=aware(source.checked_at) if source.checked_at else None,
                    check_error=source.check_error,
                    stories=states[DONE],
                    queued=states[PENDING],
                    writing=states[WRITING],
                    failed=[
                        (_post_title(p), p.error) for p in rows if p.state == FAILED
                    ],
                    platform="instagram",
                )
            )
        return out

    def author_names(self) -> dict[str, str]:
        """``{"ig:<username>": creator name}`` for every story's source label."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(InstagramSourceRow.username, InstagramSourceRow.title)
            ).all()
        return {SOURCE_PREFIX + user: title or f"@{user}" for user, title in rows}

    # --- posts ------------------------------------------------------------- #
    def known_codes(self, codes: list[str]) -> set[str]:
        with session_scope(self._engine) as session:
            return set(
                session.exec(
                    select(InstagramPostRow.code).where(
                        InstagramPostRow.code.in_(codes)
                    )
                ).all()
            )

    def record(
        self,
        source: InstagramSourceRow,
        posts: list[Post],
        *,
        first_look: list[str],
        unopened: list[str],
        first: bool,
        now: datetime,
    ) -> list[str]:
        """Store the posts opened in a check; return the codes queued.

        On a first check only the newest ``INITIAL_POSTS`` of ``first_look``
        (the grid posts it covers) are queued, counting posts already known, so
        following again brings back recent ones that were dropped. Later, a
        post is new unless it was posted well before the follow (an old post
        newly pinned). ``unopened`` grid posts are recorded as skipped so
        they're never opened later.
        """
        codes = [p.code for p in posts] + list(first_look) + list(unopened)
        queued: list[str] = []
        with session_scope(self._engine) as session:
            known = {
                row.code: row
                for row in session.exec(
                    select(InstagramPostRow).where(InstagramPostRow.code.in_(codes))
                ).all()
            }
            dated = {p.code: p.taken_at for p in posts}
            if first:
                for code in first_look:
                    row = known.get(code)
                    if row is not None and row.published_at:
                        dated.setdefault(code, aware(row.published_at))
                newest = sorted(dated, key=lambda c: dated[c], reverse=True)
                wanted = set(newest[:INITIAL_POSTS])
            else:
                floor = aware(source.added_at) - NEW_POST_SLACK
                wanted = {p.code for p in posts if p.taken_at >= floor}
            for post in posts:
                row = known.get(post.code)
                take = post.code in wanted
                if row is None:
                    session.add(
                        InstagramPostRow(
                            code=post.code,
                            source_id=source.id,
                            username=post.username or source.username,
                            author=post.author,
                            kind=post.kind,
                            caption=post.caption,
                            image_description=post.image_description,
                            published_at=iso(post.taken_at),
                            found_at=iso(now),
                            state=PENDING if take else SKIPPED,
                        )
                    )
                    if take:
                        queued.append(post.code)
            if first:
                for code, row in known.items():
                    if code in wanted and row.state in (SKIPPED, FAILED):
                        row.state, row.attempts, row.error = PENDING, 0, ""
                        row.source_id, row.not_before = source.id, None
                        session.add(row)
                        queued.append(code)
            for code in unopened:
                if code not in known:
                    session.add(
                        InstagramPostRow(
                            code=code,
                            source_id=source.id,
                            username=source.username,
                            found_at=iso(now),
                            state=SKIPPED,
                        )
                    )
        return queued

    def ready(self, *, now: datetime, limit: int) -> list[InstagramPostRow]:
        """Queued posts that may be written now, newest first."""
        return self._queue.ready(now=now, limit=limit)

    def ready_count(self, *, now: datetime) -> int:
        return self._queue.ready_count(now=now)

    def claim(self, code: str, *, now: datetime) -> bool:
        return self._queue.claim(code, now=now)

    def finish(self, code: str) -> None:
        self._queue.finish(code)

    def fail_attempt(self, code: str, error: str, *, now: datetime) -> bool:
        return self._queue.fail_attempt(code, error, now=now)

    # --- images ------------------------------------------------------------ #
    def save_image(
        self, code: str, data: bytes, content_type: str, *, now: datetime
    ) -> None:
        stmt = sqlite_insert(InstagramImageRow).values(
            code=code, content_type=content_type, data=data, fetched_at=iso(now)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={"content_type": content_type, "data": data, "fetched_at": iso(now)},
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

    def image(self, code: str) -> tuple[bytes, str] | None:
        with session_scope(self._engine) as session:
            row = session.get(InstagramImageRow, code)
            return (row.data, row.content_type) if row is not None else None

    def has_image(self, code: str) -> bool:
        with session_scope(self._engine) as session:
            return (
                session.exec(
                    select(InstagramImageRow.code).where(InstagramImageRow.code == code)
                ).first()
                is not None
            )


def _post_title(row: InstagramPostRow) -> str:
    text = " ".join(row.caption.split())
    return (text[:60] + "…") if len(text) > 60 else (text or row.code)


# post -> its story
PostWriter = Callable[[Post], Awaitable[VideoStory]]


class InstagramSync:
    """Follow Instagram creators and turn their new posts into feed stories."""

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
        # Set when Instagram shows a login wall: no checks until then.
        self.paused_until: datetime | None = None

    def follow(self, link: str) -> InstagramSourceRow:
        """Follow a pasted profile link. Raises ``BadInstagramLink``.

        Nothing is fetched here: the next ``run_once`` checks the creator.
        """
        username = parse_profile_link(link)
        self.store.follow(username, now=self.clock())
        source = self.store.source(source_id(username))
        assert source is not None
        return source

    def unfollow(self, source: str) -> bool:
        return self.store.unfollow(source, now=self.clock())

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Check the creators that are due, then write up to ``limit`` posts."""
        report = SyncReport()
        now = self.clock()
        paused = self.paused_until is not None and now < self.paused_until
        due = [] if paused else self.store.due_sources(now=now)
        if due:
            await self._check_all(due, report)
        budget = limit or self.per_run
        for row in self.store.ready(now=self.clock(), limit=budget):
            await self._write(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _check_all(
        self, due: list[InstagramSourceRow], report: SyncReport
    ) -> None:
        try:
            async with self.instagram.session() as browser:
                for source in due:
                    if not await self._check_one(browser, source, report):
                        return
        except InstagramError as exc:  # the browser didn't start
            retry_at = self.clock() + RETRY_CHECK
            for source in due:
                self.store.mark_failed(source.id, str(exc), retry_at=retry_at)
            report.errors.append(f"Instagram: {exc}")

    async def _check_one(self, browser, source, report: SyncReport) -> bool:
        """Check one creator; False when Instagram blocked and checks should stop."""
        label = f"@{source.username}"
        try:
            await self._check(browser, source, report)
        except InstagramBlocked as exc:
            self.paused_until = self.clock() + BLOCKED_PAUSE
            hours = int(BLOCKED_PAUSE.total_seconds() // 3600)
            message = f"{exc}; Instagram checks are paused for {hours} hours"
            self.store.mark_failed(source.id, message, retry_at=self.paused_until)
            report.errors.append(f"{label}: {message}")
            _LOG.warning("Instagram: %s", message)
            return False
        except AccountUnavailable as exc:
            retry_at = self.clock() + UNAVAILABLE_RETRY
            self.store.mark_failed(source.id, str(exc), retry_at=retry_at)
            report.errors.append(f"{label}: {exc}")
        except InstagramError as exc:
            retry_at = self.clock() + RETRY_CHECK
            self.store.mark_failed(source.id, str(exc), retry_at=retry_at)
            report.errors.append(f"{label}: {exc}")
        return True

    async def _check(self, browser, source: InstagramSourceRow, report) -> None:
        profile = await browser.profile(source.username)
        first = source.checked_at is None
        known = self.store.known_codes(profile.codes)
        if first:
            to_open = [c for c in profile.codes[:FIRST_LOOK] if c not in known]
            unopened = [c for c in profile.codes[FIRST_LOOK:] if c not in known]
        else:
            # Anything left over is opened on the next check.
            to_open = [c for c in profile.codes if c not in known][:MAX_NEW_PER_CHECK]
            unopened = []
        posts: list[Post] = []
        for code in to_open:
            try:
                posts.append(await browser.post(code))
            except InstagramBlocked:
                raise
            except InstagramError as exc:  # tried again on the next check
                report.errors.append(f"@{source.username} post {code}: {exc}")
        queued = self.store.record(
            source,
            posts,
            first_look=profile.codes[:FIRST_LOOK] if first else [],
            unopened=unopened,
            first=first,
            now=self.clock(),
        )
        by_code = {p.code: p for p in posts}
        for code in queued:
            post = by_code.get(code)
            if post is None or not post.image_url or self.store.has_image(code):
                continue
            try:
                data, kind = await browser.image(post.image_url)
            except InstagramError as exc:  # the card falls back to a placeholder
                _LOG.info("Instagram image for %s: %s", code, exc)
                continue
            self.store.save_image(code, data, kind, now=self.clock())
        report.checked += 1
        report.queued += len(queued)
        now = self.clock()
        self.store.mark_checked(
            source.id, now=now, next_at=now + self.check_every, title=profile.name
        )

    async def _write(self, row: InstagramPostRow, report: SyncReport) -> None:
        if not self.store.claim(row.code, now=self.clock()):
            return
        post = Post(
            code=row.code,
            username=row.username,
            author=row.author,
            kind=row.kind,
            caption=row.caption,
            taken_at=aware(row.published_at or row.found_at),
            image_description=row.image_description,
        )
        try:
            story = await self.write_story(post)
        except Exception as exc:  # noqa: BLE001 - one post never stops the rest
            error = f"writing: {first_line(exc)}"
            if self.store.fail_attempt(row.code, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"{_post_title(row)}: {error}")
            _LOG.warning("Instagram post %s: %s", row.code, error)
            return
        self.insights.add(
            Insight(
                key=post_key(row.code),
                text=story.text,
                title=story.headline,
                type=POST_TYPE,
                tags=story.tags,
                score=STORY_SCORE,
                # The followed creator, even for a collab post owned by another.
                source_session=SOURCE_PREFIX + row.source_id[len(SOURCE_ID_PREFIX) :],
                captured_at=post.taken_at,
                status=STORY_STATUS,
            )
        )
        self.store.finish(row.code)
        report.written += 1
