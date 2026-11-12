"""Persistence for the personal insight feed.

Two tables, both over the shared ``db.py`` engine (no migration system — schema is
created ``IF NOT EXISTS``):

- ``insights`` — the captured items, keyed by a **canonical key** so re-capturing
  the same insight is an exact-key no-op (the "exact-key dedup, not similarity"
  architecture the feed already uses).
- ``insight_cursors`` — a per-transcript-file line watermark, so ``insights
  collect`` only reads records appended since the last run (mirrors how ``collect``
  avoids re-doing work).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Field, SQLModel, select

from ..db import (
    column_names,
    conflict_insert,
    create_tables,
    make_engine,
    session_scope,
)
from ..models import Insight


class InsightRow(SQLModel, table=True):
    __tablename__ = "insights"

    key: str = Field(primary_key=True)
    text: str
    type: str = "general"
    tags: str = ""  # comma-joined
    score: float = 0.0
    source_session: str = ""
    git_branch: str = ""
    captured_at: str
    status: str = "new"
    stored_at: str | None = None
    title: str | None = None


class InsightCursorRow(SQLModel, table=True):
    __tablename__ = "insight_cursors"

    file_path: str = Field(primary_key=True)
    processed_lines: int = 0
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: datetime) -> str:
    """Fixed-width UTC ISO string, so stored timestamps sort lexicographically."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class InsightStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, InsightRow, InsightCursorRow)
        self._migrate()

    def _migrate(self) -> None:
        """Add ``stored_at`` to an ``insights`` table created before it existed.

        ``create_all`` never adds columns and there is no migration system, so a
        live DB keeps its old schema until brought forward here. Existing rows
        stay ``NULL``; readers fall back to ``captured_at`` for them.
        """
        with session_scope(self._engine) as session:
            cols = column_names(session, "insights")
            for col in ("stored_at", "title"):
                if col not in cols:
                    session.execute(text(f"ALTER TABLE insights ADD COLUMN {col} TEXT"))

    # --- insights --------------------------------------------------------- #
    def add(self, insight: Insight) -> bool:
        """Insert the insight; return True if new, False if its key already exists.

        Exact-key dedup: a duplicate key is silently ignored (first capture wins).
        """
        with session_scope(self._engine) as session:
            stmt = (
                conflict_insert(self._engine, InsightRow)
                .values(
                    key=insight.key,
                    text=insight.text,
                    type=insight.type,
                    tags=",".join(insight.tags),
                    score=insight.score,
                    source_session=insight.source_session,
                    git_branch=insight.git_branch,
                    captured_at=insight.captured_at.isoformat(),
                    status=insight.status,
                    stored_at=_iso(insight.stored_at or datetime.now(timezone.utc)),
                    title=insight.title or None,
                )
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(InsightRow.key)
            )
            # RETURNING, not rowcount: psycopg reports -1 for INSERTs (#111).
            return session.execute(stmt).first() is not None

    def get(self, key: str) -> Insight | None:
        with session_scope(self._engine) as session:
            row = session.get(InsightRow, key)
            return _to_insight(row) if row else None

    def all(self) -> list[Insight]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(InsightRow).order_by(
                    InsightRow.score.desc(), InsightRow.captured_at.desc()
                )
            ).all()
            return [_to_insight(r) for r in rows]

    def by_status(self, status: str) -> list[Insight]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(InsightRow)
                .where(InsightRow.status == status)
                .order_by(InsightRow.score.desc(), InsightRow.captured_at.desc())
            ).all()
            return [_to_insight(r) for r in rows]

    def set_status(self, key: str, status: str) -> bool:
        with session_scope(self._engine) as session:
            row = session.get(InsightRow, key)
            if row is None:
                return False
            row.status = status
            session.add(row)
            return True

    def missing_titles(self, limit: int | None = None) -> list[Insight]:
        """Insights without a headline yet, most recently stored first."""
        stmt = (
            select(InsightRow)
            .where((InsightRow.title.is_(None)) | (InsightRow.title == ""))
            .order_by(
                InsightRow.stored_at.desc().nulls_last(),
                InsightRow.captured_at.desc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        with session_scope(self._engine) as session:
            return [_to_insight(r) for r in session.exec(stmt).all()]

    def set_title(self, key: str, title: str) -> bool:
        with session_scope(self._engine) as session:
            row = session.get(InsightRow, key)
            if row is None:
                return False
            row.title = title
            session.add(row)
            return True

    # --- per-file watermark ---------------------------------------------- #
    def cursor(self, file_path: str) -> int:
        with session_scope(self._engine) as session:
            row = session.get(InsightCursorRow, file_path)
            return row.processed_lines if row else 0

    def set_cursor(
        self,
        file_path: str,
        processed_lines: int,
        *,
        read_at: datetime | None = None,
    ) -> None:
        """Record how far ``file_path`` was read.

        ``read_at`` is when reading started (default: now). The sweep compares
        it with the file's mtime, so anything written after that moment is
        always read again.
        """
        stamp = read_at.isoformat() if read_at else _now_iso()
        with session_scope(self._engine) as session:
            stmt = (
                conflict_insert(self._engine, InsightCursorRow)
                .values(
                    file_path=file_path,
                    processed_lines=processed_lines,
                    updated_at=stamp,
                )
                .on_conflict_do_update(
                    index_elements=["file_path"],
                    set_={"processed_lines": processed_lines, "updated_at": stamp},
                )
            )
            session.execute(stmt)

    def last_read_at(self, file_path: str) -> datetime | None:
        """When ``file_path`` was last read (``None`` if it never was)."""
        with session_scope(self._engine) as session:
            row = session.get(InsightCursorRow, file_path)
        if row is None:
            return None
        try:
            stamp = datetime.fromisoformat(row.updated_at)
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _to_insight(row: InsightRow) -> Insight:
    return Insight(
        key=row.key,
        text=row.text,
        type=row.type,
        tags=[t for t in row.tags.split(",") if t],
        score=row.score,
        source_session=row.source_session,
        git_branch=row.git_branch,
        captured_at=datetime.fromisoformat(row.captured_at),
        status=row.status,
        stored_at=datetime.fromisoformat(row.stored_at) if row.stored_at else None,
        title=row.title or "",
    )
