"""Suite-wide guards: tests never reach a real model or a real shared database.

Set ``DAILY_AGENT_TEST_DATABASE_URL`` to a throwaway Postgres database to run the
whole suite against Postgres instead of SQLite (issue #111). Every SQLite path a
test opens is then mapped to its own Postgres schema, so tests stay isolated
from each other without changing a line of them. Tests that build an old-format
SQLite file by hand are marked ``sqlite_only`` and skipped in that mode.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from daily_agent import db

TEST_DATABASE_URL = os.environ.get("DAILY_AGENT_TEST_DATABASE_URL", "")


def pytest_collection_modifyitems(config, items):
    if not TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="builds a SQLite file by hand")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch, tmp_path_factory):
    # Any `claude-code:` call that a test forgot to fake fails fast instead of
    # running the signed-in Claude Code CLI on the developer's machine.
    missing = tmp_path_factory.getbasetemp() / "no-claude-in-tests"
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(missing))


@pytest.fixture(autouse=True)
def _no_shared_database(monkeypatch):
    # A DAILY_AGENT_DATABASE_URL in the shell or a .env would point CLI tests at
    # the real shared database; an empty env var wins over both.
    monkeypatch.setenv("DAILY_AGENT_DATABASE_URL", "")


@pytest.fixture(scope="session")
def _postgres_admin():
    if not TEST_DATABASE_URL:
        yield None
        return
    engine = create_engine(db.database_url(TEST_DATABASE_URL), poolclass=NullPool)
    with engine.connect() as conn:
        collation = conn.execute(
            text(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            )
        ).scalar()
    if collation not in ("C", "POSIX"):
        pytest.exit(
            f"test database collation is {collation!r}; create it with "
            "`createdb -l C -T template0` (SQLite compares bytes, see #111)",
            returncode=2,
        )
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _postgres_backend(monkeypatch, _postgres_admin):
    if _postgres_admin is None:
        yield
        return
    real_url = db.database_url
    schemas: list[str] = []

    def per_test_schema(target):
        if db.is_url(target):
            return real_url(target)
        digest = hashlib.sha1(str(Path(target).resolve()).encode()).hexdigest()
        schema = f"t_{digest[:20]}"
        if schema not in schemas:
            with _postgres_admin.begin() as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
                conn.execute(text(f"CREATE SCHEMA {schema}"))
            schemas.append(schema)
        base = real_url(TEST_DATABASE_URL)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}options=-csearch_path%3D{schema}"

    monkeypatch.setattr(db, "database_url", per_test_schema)
    yield
    db.dispose_engines()
    with _postgres_admin.begin() as conn:
        for schema in schemas:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
