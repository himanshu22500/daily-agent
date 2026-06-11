"""Per-initiative story-state — the memory that makes the feed a *storyline*.

Each initiative carries a running plain-language summary of the effort and when
it was last narrated, so the next chapter only adds what's new rather than
re-describing the whole thing. No health/status is stored — the feed describes,
it doesn't judge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from ..db import create_tables, make_engine, session_scope


class InitiativeRow(SQLModel, table=True):
    __tablename__ = "initiatives"

    key: str = Field(primary_key=True)
    lane: str
    title: str
    story_state: str | None = None
    last_narrated_at: str | None = None
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InitiativeState:
    key: str
    lane: str
    title: str
    story_state: str | None
    last_narrated_at: str | None


class InitiativeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, InitiativeRow)

    def get(self, key: str) -> InitiativeState | None:
        with session_scope(self._engine) as session:
            row = session.get(InitiativeRow, key)
            return _to_state(row) if row else None

    def upsert(self, key: str, lane: str, title: str) -> None:
        """Ensure the initiative row exists / refresh its lane + title."""
        with session_scope(self._engine) as session:
            stmt = sqlite_insert(InitiativeRow).values(
                key=key, lane=lane, title=title, updated_at=_now_iso()
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "lane": stmt.excluded.lane,
                    "title": stmt.excluded.title,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            session.execute(stmt)

    def record_chapter(
        self, key: str, story_state: str, *, now: datetime | None = None
    ) -> None:
        """Persist the updated story-state after a chapter is delivered."""
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with session_scope(self._engine) as session:
            row = session.get(InitiativeRow, key)
            if row is None:
                return
            row.story_state = story_state
            row.last_narrated_at = stamp
            row.updated_at = stamp
            session.add(row)

    def all(self) -> list[InitiativeState]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(InitiativeRow).order_by(InitiativeRow.updated_at.desc())
            ).all()
            return [_to_state(r) for r in rows]


def _to_state(row: InitiativeRow) -> InitiativeState:
    return InitiativeState(
        key=row.key,
        lane=row.lane,
        title=row.title,
        story_state=row.story_state,
        last_narrated_at=row.last_narrated_at,
    )
