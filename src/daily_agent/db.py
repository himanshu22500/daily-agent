"""Shared SQLModel engine + session plumbing for every store.

The stores used to each hand-roll a ``sqlite3`` connection + ``CREATE TABLE``
``_conn()`` context manager. They now share this one helper: an SQLAlchemy engine
per database file and a ``session_scope`` that commits on clean exit and always
closes — the same commit-on-success/close-always contract the old ``_conn()`` had.

Tables are still created with ``IF NOT EXISTS`` semantics (``create_all`` with
``checkfirst=True``), so opening an existing DB never rewrites its schema — there
is no migration system and live DBs must keep working untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine


def make_engine(db_path: str | Path) -> Engine:
    """Build a SQLite engine for ``db_path`` (relative or absolute).

    ``check_same_thread=False`` because a store's engine outlives a single call
    and may be reused across threads (e.g. the listener daemon); SQLite file
    locking still serializes writers.
    """
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )


def create_tables(engine: Engine, *models: type[SQLModel]) -> None:
    """Create just these models' tables if they don't already exist."""
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in models])


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Yield a session that commits on clean exit and always closes.

    ``expire_on_commit=False`` keeps already-loaded attributes readable after the
    scope closes, so callers can map a fetched row to a plain return type (or read
    its values) without a DetachedInstanceError refresh.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    finally:
        session.close()
