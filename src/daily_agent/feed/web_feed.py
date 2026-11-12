"""Reader-side view of the insight store, for the web feed (issues #77, #80).

The web feed is *pull*: it reads the ``insights`` table directly, so an insight
is visible the moment it is stored. It never waits on the outbox, the pacer, or
Telegram, which keeps draining the outbox at its own pace, untouched.

Two ways to read:

- **For you** (default): every open builds a new *edition*, a shuffled,
  non-repeating order of unseen insights plus spaced revisits of seen ones (see
  ``for_you.py``). The order is frozen in ``web_feed_sessions`` so infinite
  scroll pages through one stable edition while the reader's state changes.
- **Latest / Saved / Hidden**: chronological by publish time (``stored_at``,
  falling back to ``captured_at``), keyset-paginated.

Per-reader state lives in ``web_feed_state``, separate from ``insights.status``
(which belongs to the delivery pipeline). An insight counts as *seen* only when
the page reports it was actually on screen (``record_views``), at most once per
edition, and each view schedules its next revisit.
"""

from __future__ import annotations

import json
import random
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import ColumnElement, and_, delete, or_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from ..sources.youtube import embed_url, thumbnail_url, watch_url
from .for_you import Candidate, build_edition, revisit_interval
from .headlines import fallback_headline, plain, split_first_sentence
from .insights_store import InsightCursorRow, InsightRow, InsightStore
from .projects import session_projects
from .videos import VideoStore, video_id_of

FOR_YOU = "for-you"
VIEWS = (FOR_YOU, "latest", "saved", "hidden")
# Editions older than this are pruned; their infinite scroll asks for a refresh.
EDITION_TTL = timedelta(days=3)


class WebFeedStateRow(SQLModel, table=True):
    __tablename__ = "web_feed_state"

    key: str = Field(primary_key=True)
    seen_at: str | None = None  # first time on screen
    saved_at: str | None = None
    dismissed_at: str | None = None
    view_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_seen_at: str | None = None
    due_at: str | None = None  # when it may resurface in For you
    last_session: str | None = None  # edition that last counted a view


class WebFeedSessionRow(SQLModel, table=True):
    """One For you edition: the frozen order of keys it serves."""

    __tablename__ = "web_feed_sessions"

    id: str = Field(primary_key=True)
    created_at: str
    type_filter: str = ""
    keys: str  # JSON list


@dataclass(frozen=True)
class FeedEntry:
    key: str
    text: str
    type: str
    tags: list[str]
    git_branch: str
    captured_at: datetime
    published_at: datetime
    # Cursor of this entry's position, for "load more after this one".
    cursor: str
    new: bool
    saved: bool
    hidden: bool
    view_count: int = 0
    last_seen_at: datetime | None = None
    title: str = ""
    # Project the insight was learned in (the feed's "publisher").
    source: str = ""

    @property
    def revisit(self) -> bool:
        return self.view_count > 0

    @property
    def headline(self) -> str:
        return self.title or fallback_headline(self.text)

    @property
    def video_id(self) -> str:
        """YouTube id for a video story ("" for a coding insight)."""
        return video_id_of(self.key)

    @property
    def thumbnail(self) -> str:
        return thumbnail_url(self.video_id) if self.video_id else ""

    @property
    def video_url(self) -> str:
        return watch_url(self.video_id) if self.video_id else ""

    @property
    def embed_url(self) -> str:
        return embed_url(self.video_id) if self.video_id else ""

    @property
    def snippet(self) -> str:
        """Plain-text teaser under the headline (never repeats it)."""
        if self.title:
            return plain(self.text)
        return split_first_sentence(self.text)[1]


@dataclass(frozen=True)
class FeedPage:
    entries: list[FeedEntry]
    # Pass back as ``cursor`` to fetch the next page; ``None`` at the end.
    next_cursor: str | None


@dataclass(frozen=True)
class EditionPage:
    entries: list[FeedEntry]
    # Pass back as ``offset`` for the next page; ``None`` at the end.
    next_offset: int | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


_PUBLISHED = func.coalesce(InsightRow.stored_at, InsightRow.captured_at)


def _cursor(published: str, key: str) -> str:
    return f"{published}|{key}"


def _parse_cursor(cursor: str) -> tuple[str, str]:
    published, sep, key = cursor.partition("|")
    if not sep or not published or not key:
        raise ValueError(f"invalid feed cursor: {cursor!r}")
    datetime.fromisoformat(published)  # raises ValueError if malformed
    return published, key


class WebFeed:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        # Ensures the insights table exists and carries ``stored_at``.
        InsightStore(self.db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, WebFeedStateRow, WebFeedSessionRow)
        self._videos = VideoStore(self.db_path)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a pre-#80 ``web_feed_state`` forward.

        Adds the view-tracking columns. Before #80, ``seen_at`` meant "rendered",
        so cards that were only loaded (never on screen) counted as seen. Those
        marks are reset once here, so from now on seen means actually viewed.
        """
        with session_scope(self._engine) as session:
            cols = {
                row[1]
                for row in session.execute(
                    text("PRAGMA table_info(web_feed_state)")
                ).all()
            }
            if "view_count" in cols:
                return
            session.execute(
                text(
                    "ALTER TABLE web_feed_state "
                    "ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0"
                )
            )
            for col in ("last_seen_at", "due_at", "last_session"):
                session.execute(
                    text(f"ALTER TABLE web_feed_state ADD COLUMN {col} TEXT")
                )
            session.execute(text("UPDATE web_feed_state SET seen_at = NULL"))

    def _sources(self) -> dict[str, str]:
        """``{source_session: label}``: projects for insights, channels for videos."""
        with session_scope(self._engine) as session:
            paths = session.exec(select(InsightCursorRow.file_path)).all()
        return {**session_projects(paths), **self._videos.channel_names()}

    # --- reading: chronological views -------------------------------------- #
    def _base(self):
        return select(InsightRow, WebFeedStateRow, _PUBLISHED).outerjoin(
            WebFeedStateRow, WebFeedStateRow.key == InsightRow.key
        )

    @staticmethod
    def _filters(
        view: str, type: str | None, query: str | None
    ) -> list[ColumnElement[bool]]:
        if view not in VIEWS or view == FOR_YOU:
            raise ValueError(f"not a chronological view: {view!r}")
        clauses: list[ColumnElement[bool]] = []
        if view == "latest":
            clauses.append(WebFeedStateRow.dismissed_at.is_(None))
        elif view == "saved":
            clauses.append(WebFeedStateRow.saved_at.is_not(None))
        else:
            clauses.append(WebFeedStateRow.dismissed_at.is_not(None))
        if type:
            clauses.append(InsightRow.type == type)
        if query and query.strip():
            q = query.strip()
            clauses.append(
                or_(
                    InsightRow.text.contains(q, autoescape=True),
                    InsightRow.tags.contains(q, autoescape=True),
                    InsightRow.git_branch.contains(q, autoescape=True),
                )
            )
        return clauses

    def page(
        self,
        *,
        view: str = "latest",
        type: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> FeedPage:
        """One page of a chronological view, newest-published first.

        Keyset-paginated on ``(published, key)`` so saving or hiding items
        between page loads never shifts later pages (no skips, no repeats).
        Raises ``ValueError`` for an unknown view or a malformed cursor.
        """
        stmt = self._base().where(*self._filters(view, type, query))
        if cursor:
            published, key = _parse_cursor(cursor)
            stmt = stmt.where(
                or_(
                    _PUBLISHED < published,
                    and_(_PUBLISHED == published, InsightRow.key > key),
                )
            )
        stmt = stmt.order_by(_PUBLISHED.desc(), InsightRow.key).limit(limit + 1)
        with session_scope(self._engine) as session:
            rows = session.exec(stmt).all()
        sources = self._sources()
        entries = [_entry(*row, sources) for row in rows[:limit]]
        next_cursor = entries[-1].cursor if len(rows) > limit and entries else None
        return FeedPage(entries=entries, next_cursor=next_cursor)

    # --- reading: For you editions ----------------------------------------- #
    def new_edition(
        self,
        *,
        type: str | None = None,
        now: datetime | None = None,
        rng: random.Random | None = None,
    ) -> str:
        """Build and store a fresh For you order; return its edition id."""
        moment = now or _now()
        stmt = self._base().where(WebFeedStateRow.dismissed_at.is_(None))
        if type:
            stmt = stmt.where(InsightRow.type == type)
        with session_scope(self._engine) as session:
            candidates = [_candidate(*row) for row in session.exec(stmt).all()]
        keys = build_edition(candidates, now=moment, rng=rng or random.Random())
        edition_id = secrets.token_urlsafe(9)
        with session_scope(self._engine) as session:
            session.execute(
                delete(WebFeedSessionRow).where(
                    WebFeedSessionRow.created_at < _iso(moment - EDITION_TTL)
                )
            )
            session.add(
                WebFeedSessionRow(
                    id=edition_id,
                    created_at=_iso(moment),
                    type_filter=type or "",
                    keys=json.dumps(keys),
                )
            )
        return edition_id

    def edition_page(
        self, edition_id: str, *, offset: int = 0, limit: int = 20
    ) -> EditionPage:
        """A page of a stored edition, skipping insights hidden since it was built.

        Raises ``KeyError`` for an unknown (or pruned) edition.
        """
        with session_scope(self._engine) as session:
            row = session.get(WebFeedSessionRow, edition_id)
            if row is None:
                raise KeyError(edition_id)
            keys: list[str] = json.loads(row.keys)
        entries: list[FeedEntry] = []
        sources = self._sources()
        position = max(offset, 0)
        while position < len(keys) and len(entries) < limit:
            chunk = keys[position : position + limit]
            with session_scope(self._engine) as session:
                found = {
                    insight.key: (insight, state, published)
                    for insight, state, published in session.exec(
                        self._base().where(InsightRow.key.in_(chunk))
                    ).all()
                }
            for key in chunk:
                position += 1
                hit = found.get(key)
                if hit and not (hit[1] and hit[1].dismissed_at):
                    entries.append(_entry(*hit, sources))
                    if len(entries) == limit:
                        break
        return EditionPage(
            entries=entries, next_offset=position if position < len(keys) else None
        )

    def next_revisit_at(self, *, now: datetime | None = None) -> datetime | None:
        """When the next seen insight becomes due again (``None`` if none pending)."""
        stamp = _iso(now or _now())
        stmt = (
            select(func.min(WebFeedStateRow.due_at))
            .where(WebFeedStateRow.dismissed_at.is_(None))
            .where(WebFeedStateRow.due_at > stamp)
        )
        with session_scope(self._engine) as session:
            value = session.scalar(stmt)
        return _aware(value) if value else None

    # --- reading: shared ---------------------------------------------------- #
    def entry(self, key: str) -> FeedEntry | None:
        with session_scope(self._engine) as session:
            row = session.exec(self._base().where(InsightRow.key == key)).first()
        return _entry(*row, self._sources()) if row else None

    def related(
        self, key: str, *, limit: int = 4, now: datetime | None = None
    ) -> list[FeedEntry]:
        """What to read after ``key`` (the reader's "Read next").

        Scored by shared tags (3 each), same project (1.5), and same type (1);
        unseen insights get +1 and ones viewed in the last day -2, so the list
        leans toward something new. Ties go to the newest. At most two per
        coding session, so near-duplicates don't crowd it. Hidden insights never
        appear. With nothing related, unseen recent insights fill the slots.
        """
        current = self.entry(key)
        if current is None:
            return []
        moment = now or _now()
        sources = self._sources()
        with session_scope(self._engine) as session:
            rows = session.exec(
                self._base()
                .where(WebFeedStateRow.dismissed_at.is_(None))
                .where(InsightRow.key != key)
            ).all()
        tags = set(current.tags)
        scored = []
        for insight, state, published in rows:
            other = _entry(insight, state, published, sources)
            score = 3.0 * len(tags & set(other.tags))
            if current.source and other.source == current.source:
                score += 1.5
            if other.type == current.type:
                score += 1.0
            if other.new:
                score += 1.0
            elif other.last_seen_at and moment - other.last_seen_at < timedelta(days=1):
                score -= 2.0
            scored.append((score, other.published_at, insight.source_session, other))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        picked: list[FeedEntry] = []
        per_session: Counter[str] = Counter()
        for _, _, session_id, other in scored:
            if session_id and per_session[session_id] >= 2:
                continue
            per_session[session_id] += 1
            picked.append(other)
            if len(picked) == limit:
                break
        return picked

    def types(self) -> list[tuple[str, int]]:
        """Insight types present in the visible feed, most common first."""
        stmt = (
            select(InsightRow.type, func.count())
            .outerjoin(WebFeedStateRow, WebFeedStateRow.key == InsightRow.key)
            .where(WebFeedStateRow.dismissed_at.is_(None))
            .group_by(InsightRow.type)
            .order_by(func.count().desc(), InsightRow.type)
        )
        with session_scope(self._engine) as session:
            return [(t, n) for t, n in session.exec(stmt).all()]

    def unseen_count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(InsightRow)
            .outerjoin(WebFeedStateRow, WebFeedStateRow.key == InsightRow.key)
            .where(WebFeedStateRow.seen_at.is_(None))
            .where(WebFeedStateRow.dismissed_at.is_(None))
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    def count_published_after(self, since: datetime) -> int:
        """Visible insights published after ``since`` (when a page was opened)."""
        stmt = (
            select(func.count())
            .select_from(InsightRow)
            .outerjoin(WebFeedStateRow, WebFeedStateRow.key == InsightRow.key)
            .where(WebFeedStateRow.dismissed_at.is_(None))
            .where(_PUBLISHED > _iso(since))
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    # --- reader state ----------------------------------------------------- #
    def record_views(
        self,
        keys: Iterable[str],
        *,
        session_id: str,
        now: datetime | None = None,
    ) -> int:
        """Record that ``keys`` were on screen; return how many views counted.

        A key counts once per ``session_id`` (one edition or page load), so
        scrolling back up never double-counts. Each counted view schedules the
        next revisit by the spaced-repetition gap for its new view count.
        """
        moment = now or _now()
        stamp = _iso(moment)
        counted = 0
        with session_scope(self._engine) as session:
            for key in dict.fromkeys(keys):
                if session.get(InsightRow, key) is None:
                    continue
                state = session.get(WebFeedStateRow, key) or WebFeedStateRow(key=key)
                if state.last_session == session_id:
                    continue
                state.view_count = (state.view_count or 0) + 1
                state.seen_at = state.seen_at or stamp
                state.last_seen_at = stamp
                state.last_session = session_id
                gap = revisit_interval(state.view_count, saved=bool(state.saved_at))
                state.due_at = _iso(moment + gap)
                session.add(state)
                counted += 1
        return counted

    def _exists(self, key: str) -> bool:
        with session_scope(self._engine) as session:
            return session.get(InsightRow, key) is not None

    def _set(self, key: str, column: str, value: str | None) -> bool:
        if not self._exists(key):
            return False
        stmt = sqlite_insert(WebFeedStateRow).values(key=key, **{column: value})
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={column: getattr(stmt.excluded, column)}
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)
        return True

    def set_saved(self, key: str, saved: bool) -> bool:
        """Save or unsave ``key``; ``False`` if no such insight."""
        return self._set(key, "saved_at", _iso(_now()) if saved else None)

    def set_hidden(self, key: str, hidden: bool) -> bool:
        """Hide ``key`` from the feed (or restore it); ``False`` if unknown."""
        return self._set(key, "dismissed_at", _iso(_now()) if hidden else None)


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _candidate(
    insight: InsightRow, state: WebFeedStateRow | None, published: str
) -> Candidate:
    return Candidate(
        key=insight.key,
        type=insight.type or "general",
        score=insight.score or 0.0,
        source_session=insight.source_session,
        published_at=_aware(published),
        view_count=(state.view_count or 0) if state else 0,
        due_at=_aware(state.due_at) if state and state.due_at else None,
        saved=bool(state and state.saved_at),
    )


def _entry(
    insight: InsightRow,
    state: WebFeedStateRow | None,
    published: str,
    sources: dict[str, str] | None = None,
) -> FeedEntry:
    return FeedEntry(
        key=insight.key,
        text=insight.text,
        type=insight.type or "general",
        tags=[t for t in insight.tags.split(",") if t],
        git_branch=insight.git_branch,
        captured_at=_aware(insight.captured_at),
        published_at=_aware(published),
        cursor=_cursor(published, insight.key),
        new=state is None or state.seen_at is None,
        saved=state is not None and state.saved_at is not None,
        hidden=state is not None and state.dismissed_at is not None,
        view_count=(state.view_count or 0) if state else 0,
        last_seen_at=_aware(state.last_seen_at)
        if state and state.last_seen_at
        else None,
        title=insight.title or "",
        source=(sources or {}).get(insight.source_session, ""),
    )
