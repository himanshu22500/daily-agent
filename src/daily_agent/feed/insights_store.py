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

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from ..db import create_tables, make_engine, session_scope
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


class InsightCursorRow(SQLModel, table=True):
    __tablename__ = "insight_cursors"

    file_path: str = Field(primary_key=True)
    processed_lines: int = 0
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InsightStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, InsightRow, InsightCursorRow)

    # --- insights --------------------------------------------------------- #
    def add(self, insight: Insight) -> bool:
        """Insert the insight; return True if new, False if its key already exists.

        Exact-key dedup: a duplicate key is silently ignored (first capture wins).
        """
        with session_scope(self._engine) as session:
            stmt = (
                sqlite_insert(InsightRow)
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
                )
                .on_conflict_do_nothing(index_elements=["key"])
            )
            return session.execute(stmt).rowcount > 0

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

    # --- per-file watermark ---------------------------------------------- #
    def cursor(self, file_path: str) -> int:
        with session_scope(self._engine) as session:
            row = session.get(InsightCursorRow, file_path)
            return row.processed_lines if row else 0

    def set_cursor(self, file_path: str, processed_lines: int) -> None:
        with session_scope(self._engine) as session:
            stmt = (
                sqlite_insert(InsightCursorRow)
                .values(
                    file_path=file_path,
                    processed_lines=processed_lines,
                    updated_at=_now_iso(),
                )
                .on_conflict_do_update(
                    index_elements=["file_path"],
                    set_={
                        "processed_lines": processed_lines,
                        "updated_at": _now_iso(),
                    },
                )
            )
            session.execute(stmt)


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
    )
