"""The reader's feed settings: a tiny key/value table (issue #132).

Kept on the server, not in a cookie, so the phone and the Mac show the same
feed (there is one reader). A missing key means "use the default".

Today it holds one switch: **Prep mode**, which blends interview prep into
For you (on unless turned off).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel

from ..db import conflict_insert, create_tables, make_engine, session_scope

PREP_MODE = "prep_mode"
ON, OFF = "on", "off"


class PreferenceRow(SQLModel, table=True):
    __tablename__ = "feed_preferences"

    key: str = Field(primary_key=True)
    value: str
    updated_at: str


class Preferences:
    """The ``feed_preferences`` table."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, PreferenceRow)

    def get(self, key: str) -> str | None:
        with session_scope(self._engine) as session:
            row = session.get(PreferenceRow, key)
            return row.value if row else None

    def set(self, key: str, value: str, *, now: datetime | None = None) -> None:
        """Store ``value`` for ``key``, replacing any earlier one."""
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        updated = stamp.isoformat(timespec="microseconds")
        stmt = conflict_insert(self._engine, PreferenceRow).values(
            key=key, value=value, updated_at=updated
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": updated}
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

    def prep_mode(self) -> bool:
        """Whether For you blends in interview prep (default: on)."""
        return self.get(PREP_MODE) != OFF

    def set_prep_mode(self, on: bool) -> None:
        self.set(PREP_MODE, ON if on else OFF)
