"""Reader-side view of the insight store, for the web feed (issue #77).

The web feed is *pull*: it reads the ``insights`` table directly, so an insight
is visible the moment it is stored — it never waits on the outbox, the pacer, or
Telegram. Telegram keeps draining the outbox at its own pace, untouched.

Ordering is by **publish time** — ``insights.stored_at``, falling back to
``captured_at`` for rows stored before that column existed — newest first. A
catch-up flush that stores weeks-old transcript lessons therefore still lands
them at the top of the feed instead of burying them by transcript date.

Per-reader state (seen / saved / dismissed) lives in its own ``web_feed_state``
table rather than on ``insights.status``, which belongs to the delivery pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, func, select

from ..db import create_tables, make_engine, session_scope
from .insights_store import InsightRow, InsightStore

VIEWS = ("all", "saved", "hidden")


class WebFeedStateRow(SQLModel, table=True):
    __tablename__ = "web_feed_state"

    key: str = Field(primary_key=True)
    seen_at: str | None = None
    saved_at: str | None = None
    dismissed_at: str | None = None


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


@dataclass(frozen=True)
class FeedPage:
    entries: list[FeedEntry]
    # Pass back as ``cursor`` to fetch the next page; ``None`` at the end.
    next_cursor: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
        create_tables(self._engine, WebFeedStateRow)

    # --- reading ---------------------------------------------------------- #
    def _base(self):
        return select(InsightRow, WebFeedStateRow, _PUBLISHED).outerjoin(
            WebFeedStateRow, WebFeedStateRow.key == InsightRow.key
        )

    @staticmethod
    def _filters(
        view: str, type: str | None, query: str | None
    ) -> list[ColumnElement[bool]]:
        if view not in VIEWS:
            raise ValueError(f"unknown view: {view!r}")
        clauses: list[ColumnElement[bool]] = []
        if view == "all":
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
        view: str = "all",
        type: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> FeedPage:
        """One page of the feed, newest-published first.

        Keyset-paginated on ``(published, key)`` so saving or dismissing items
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
        entries = [_entry(*row) for row in rows[:limit]]
        next_cursor = entries[-1].cursor if len(rows) > limit and entries else None
        return FeedPage(entries=entries, next_cursor=next_cursor)

    def entry(self, key: str) -> FeedEntry | None:
        with session_scope(self._engine) as session:
            row = session.exec(self._base().where(InsightRow.key == key)).first()
        return _entry(*row) if row else None

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

    def count_newer_than(self, cursor: str) -> int:
        """Visible insights published after ``cursor`` (the top of a loaded page)."""
        published, key = _parse_cursor(cursor)
        stmt = (
            select(func.count())
            .select_from(InsightRow)
            .outerjoin(WebFeedStateRow, WebFeedStateRow.key == InsightRow.key)
            .where(WebFeedStateRow.dismissed_at.is_(None))
            .where(
                or_(
                    _PUBLISHED > published,
                    and_(_PUBLISHED == published, InsightRow.key < key),
                )
            )
        )
        with session_scope(self._engine) as session:
            return session.scalar(stmt) or 0

    # --- reader state ----------------------------------------------------- #
    def _exists(self, key: str) -> bool:
        with session_scope(self._engine) as session:
            return session.get(InsightRow, key) is not None

    def mark_seen(self, keys: Iterable[str], *, now: str | None = None) -> None:
        """Record first sight of each key; later calls never move ``seen_at``."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return
        stamp = now or _now_iso()
        stmt = sqlite_insert(WebFeedStateRow).values(
            [{"key": k, "seen_at": stamp} for k in keys]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "seen_at": func.coalesce(WebFeedStateRow.seen_at, stmt.excluded.seen_at)
            },
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

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
        return self._set(key, "saved_at", _now_iso() if saved else None)

    def set_hidden(self, key: str, hidden: bool) -> bool:
        """Hide ``key`` from the main feed (or restore it); ``False`` if unknown."""
        return self._set(key, "dismissed_at", _now_iso() if hidden else None)


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _entry(
    insight: InsightRow, state: WebFeedStateRow | None, published: str
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
    )
