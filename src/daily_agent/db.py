"""Shared SQLModel engine + session plumbing for every store.

The stores used to each hand-roll a ``sqlite3`` connection + ``CREATE TABLE``
``_conn()`` context manager. They now share this one helper: an SQLAlchemy engine
per database and a ``session_scope`` that commits on clean exit and always
closes — the same commit-on-success/close-always contract the old ``_conn()`` had.

A store's ``db`` argument is either a SQLite file path (the default, and what the
tests use) or a database URL such as ``postgresql://user:pass@host/db`` (issue
#111: one Postgres shared by the Mac and the always-on feed server). Everything
dialect-specific lives here: upserts go through :func:`conflict_insert` and
schema checks through :func:`column_names`.

Tables are still created with ``IF NOT EXISTS`` semantics (``create_all`` with
``checkfirst=True``), so opening an existing DB never rewrites its schema — there
is no migration system and live DBs must keep working untouched.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine

_URL_ENGINES: dict[str, Engine] = {}
_URL_ENGINES_LOCK = threading.Lock()


def is_url(db: str | Path) -> bool:
    """True when ``db`` is a database URL rather than a SQLite file path."""
    return "://" in str(db)


def database_url(db: str | Path) -> str:
    """SQLAlchemy URL for a store's ``db`` argument (a file path or a URL).

    ``postgres://`` and bare ``postgresql://`` URLs use the psycopg 3 driver,
    the only Postgres driver installed.
    """
    if not is_url(db):
        return f"sqlite:///{db}"
    url = str(db)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def display_url(db: str | Path) -> str:
    """``db`` safe to print or log: a path as-is, a URL with its password masked."""
    if not is_url(db):
        return str(db)
    return make_url(database_url(db)).render_as_string(hide_password=True)


def make_engine(db: str | Path) -> Engine:
    """Engine for ``db``: a SQLite file path (relative or absolute) or a URL.

    SQLite: a fresh engine per call, ``check_same_thread=False`` because a store's
    engine outlives a single call and may be reused across threads; SQLite file
    locking still serializes writers.

    URL: one shared engine per URL, so the half-dozen stores a process opens
    share one small connection pool instead of each holding their own. Stale
    connections (the Mac sleeps, Wi-Fi drops) are detected and replaced.
    """
    url = database_url(db)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    with _URL_ENGINES_LOCK:
        engine = _URL_ENGINES.get(url)
        if engine is None:
            engine = create_engine(
                url,
                pool_size=3,
                max_overflow=7,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            _URL_ENGINES[url] = engine
        return engine


def dispose_engines() -> None:
    """Close every shared URL engine's connections (tests, process shutdown)."""
    with _URL_ENGINES_LOCK:
        engines = list(_URL_ENGINES.values())
        _URL_ENGINES.clear()
    for engine in engines:
        engine.dispose()


def create_tables(engine: Engine, *models: type[SQLModel]) -> None:
    """Create just these models' tables if they don't already exist."""
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in models])


def conflict_insert(bind: Engine | Session, model: type[SQLModel]):
    """``INSERT`` for ``model`` that supports ``on_conflict_do_nothing/update``.

    SQLite and Postgres spell upserts the same way in SQLAlchemy but through
    their own dialect's ``insert``; this picks the one matching ``bind``.
    """
    engine = bind.get_bind() if isinstance(bind, Session) else bind
    dialect = postgresql if engine.dialect.name == "postgresql" else sqlite
    return dialect.insert(model)


def column_names(session: Session, table: str) -> set[str]:
    """Names of ``table``'s current columns, for add-a-column migrations."""
    return {col["name"] for col in inspect(session.connection()).get_columns(table)}


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
