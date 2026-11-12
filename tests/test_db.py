"""Database plumbing for a shared Postgres (issue #111): URLs, upserts, copying."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.config import Settings, get_settings
from daily_agent.db import conflict_insert, database_url, display_url, is_url
from daily_agent.db_copy import CopyError, copy_database, table_counts
from daily_agent.feed.insights_store import InsightRow, InsightStore
from daily_agent.feed.outbox import Bite, Outbox
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _insight(key: str) -> Insight:
    return Insight(key=f"insight:{key}", text=f"Lesson {key}.", captured_at=T0)


def _seed(db) -> None:
    store = InsightStore(db)
    outbox = Outbox(db)
    for key in ("a", "b", "c"):
        store.add(_insight(key))
        outbox.enqueue(
            Bite(
                dedup_key=f"insight:{key}",
                subject=f"insight:{key}",
                kind="insight",
                content=key,
            )
        )
    WebFeed(db).set_saved("insight:b", True)


# --- URLs ------------------------------------------------------------------- #
def test_paths_are_sqlite_and_postgres_urls_use_psycopg():
    assert not is_url("daily_agent.db")
    assert database_url("/tmp/x.db") == "sqlite:////tmp/x.db"
    for url in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        assert is_url(url)
        assert database_url(url) == "postgresql+psycopg://u:p@h/db"
    assert database_url("postgresql+psycopg://u:p@h/db") == (
        "postgresql+psycopg://u:p@h/db"
    )


def test_display_url_never_shows_the_password():
    shown = display_url("postgresql://daily_agent:s3cret@daily-agent/daily_agent")
    assert "s3cret" not in shown
    assert "daily-agent/daily_agent" in shown
    assert display_url("daily_agent.db") == "daily_agent.db"


def test_settings_prefer_the_shared_database_over_the_sqlite_file():
    assert Settings(db_path="x.db", database_url="").db == "x.db"
    url = "postgresql://u:p@h/db"
    assert Settings(db_path="x.db", database_url=url).db == url


def test_conflict_insert_follows_the_engine_dialect():
    sqlite_engine = create_engine("sqlite://")
    pg_engine = create_engine("postgresql+psycopg://u:p@h/db")
    sqlite_stmt = conflict_insert(sqlite_engine, InsightRow)
    pg_stmt = conflict_insert(pg_engine, InsightRow)
    assert type(sqlite_stmt).__module__.startswith("sqlalchemy.dialects.sqlite")
    assert type(pg_stmt).__module__.startswith("sqlalchemy.dialects.postgresql")
    compiled = (
        pg_stmt.values(key="k")
        .on_conflict_do_nothing()
        .compile(dialect=pg_engine.dialect)
    )
    assert "ON CONFLICT DO NOTHING" in str(compiled)


def test_new_rows_are_reported_and_duplicates_are_not(tmp_path):
    store = InsightStore(tmp_path / "f.db")
    assert store.add(_insight("a")) is True
    assert store.add(_insight("a")) is False


# --- copy ------------------------------------------------------------------- #
def test_copy_moves_every_store_row_and_reader_state(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "shared.db"
    _seed(source)

    report = copy_database(source, target)

    assert report.copied["insights"] == 3
    assert report.copied["outbox"] == 3
    assert report.copied["web_feed_state"] == 1
    assert [i.key for i in InsightStore(target).all()] == [
        i.key for i in InsightStore(source).all()
    ]
    assert WebFeed(target).entry("insight:b").saved
    source_counts = {name: n or 0 for name, n in table_counts(source).items()}
    assert table_counts(target) == source_counts


def test_copied_ids_do_not_collide_with_new_rows(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "shared.db"
    _seed(source)
    copy_database(source, target)

    outbox = Outbox(target)
    assert outbox.enqueue(
        Bite(dedup_key="insight:d", subject="insight:d", kind="insight", content="d")
    )
    assert outbox.stats()["pending"] == 4


def test_copy_refuses_a_target_that_already_has_rows(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "shared.db"
    _seed(source)
    InsightStore(target).add(_insight("z"))

    with pytest.raises(CopyError, match="insights"):
        copy_database(source, target)
    assert [i.key for i in InsightStore(target).all()] == ["insight:z"]

    copy_database(source, target, replace=True)
    assert len(InsightStore(target).all()) == 3


@pytest.mark.sqlite_only
def test_copy_reports_missing_and_foreign_tables(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "shared.db"
    InsightStore(source).add(_insight("a"))
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE legacy_digest (id INTEGER PRIMARY KEY)")

    report = copy_database(source, target)

    assert report.copied == {"insights": 1, "insight_cursors": 0}
    assert "outbox" in report.missing_in_source
    assert report.left_behind == ["legacy_digest"]


def test_cli_copy_and_info(tmp_path, monkeypatch):
    source, target = tmp_path / "live.db", tmp_path / "shared.db"
    _seed(source)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(target))
    get_settings.cache_clear()
    runner = CliRunner()

    copied = runner.invoke(cli.app, ["db", "copy", str(source), "--to", str(target)])
    assert copied.exit_code == 0, copied.output
    assert "insights: 3" in copied.output

    again = runner.invoke(cli.app, ["db", "copy", str(source), "--to", str(target)])
    assert again.exit_code == 1
    assert "already has rows" in again.output

    info = runner.invoke(cli.app, ["db", "info"])
    assert info.exit_code == 0, info.output
    assert "insights: 3" in info.output
    get_settings.cache_clear()
