"""Web feed store: publish-time ordering, keyset paging, filters, reader state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _insight(
    key: str,
    *,
    stored: int = 0,
    captured: datetime = T0,
    type: str = "gotcha",
    text: str | None = None,
    tags: list[str] | None = None,
    branch: str = "main",
) -> Insight:
    """An insight stored ``stored`` minutes after T0."""
    return Insight(
        key=f"insight:{key}",
        text=text or f"Lesson {key}.",
        type=type,
        tags=tags or [],
        git_branch=branch,
        captured_at=captured,
        stored_at=T0 + timedelta(minutes=stored),
    )


def _seed(db: str, *insights: Insight) -> WebFeed:
    store = InsightStore(db)
    for insight in insights:
        store.add(insight)
    return WebFeed(db)


def _keys(feed: WebFeed, **kwargs) -> list[str]:
    return [e.key.removeprefix("insight:") for e in feed.page(**kwargs).entries]


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "feed.db")


def test_add_stamps_stored_at_when_unset(db):
    store = InsightStore(db)
    before = datetime.now(timezone.utc)
    store.add(Insight(key="insight:x", text="x", captured_at=T0))
    stored = store.get("insight:x").stored_at
    assert stored is not None and stored >= before - timedelta(seconds=1)


def test_old_insights_table_gains_stored_at_and_sorts_by_captured(db):
    # A live DB created before `stored_at` existed.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE insights (key VARCHAR PRIMARY KEY, text VARCHAR NOT NULL, "
            "type VARCHAR NOT NULL, tags VARCHAR NOT NULL, score FLOAT NOT NULL, "
            "source_session VARCHAR NOT NULL, git_branch VARCHAR NOT NULL, "
            "captured_at VARCHAR NOT NULL, status VARCHAR NOT NULL)"
        )
        for key, day in (("older", 1), ("newer", 2)):
            conn.execute(
                "INSERT INTO insights VALUES (?, ?, 'repo', '', 0, 's', 'main', ?, "
                "'queued')",
                (f"insight:{key}", key, f"2026-07-0{day}T10:00:00.000000+00:00"),
            )
    feed = WebFeed(db)
    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(insights)")}
    assert "stored_at" in cols
    assert InsightStore(db).get("insight:older").stored_at is None
    # Legacy rows fall back to captured_at; a freshly stored one tops them.
    InsightStore(db).add(Insight(key="insight:fresh", text="f", captured_at=T0))
    assert _keys(feed) == ["fresh", "newer", "older"]


def test_orders_by_publish_time_not_transcript_time(db):
    weeks_ago = T0 - timedelta(days=30)
    feed = _seed(
        db,
        _insight("caught-up", stored=10, captured=weeks_ago),
        _insight("recent", stored=5, captured=T0),
    )
    # The catch-up insight was learned a month ago but published last: top.
    assert _keys(feed) == ["caught-up", "recent"]
    top = feed.page().entries[0]
    assert top.captured_at == weeks_ago
    assert top.published_at == T0 + timedelta(minutes=10)


def test_keyset_paging_covers_ties_without_repeats(db):
    # Five insights published in the same instant: ties break on key.
    feed = _seed(db, *[_insight(k) for k in "edcba"])
    seen, cursor = [], None
    while True:
        page = feed.page(cursor=cursor, limit=2)
        seen += [e.key for e in page.entries]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == [f"insight:{k}" for k in "abcde"]


def test_hiding_items_never_shifts_later_pages(db):
    feed = _seed(db, *[_insight(k, stored=i) for i, k in enumerate("abcdef")])
    first = feed.page(limit=2)
    assert [e.key[-1] for e in first.entries] == ["f", "e"]
    for entry in first.entries:
        feed.set_hidden(entry.key, True)
    second = feed.page(cursor=first.next_cursor, limit=2)
    assert [e.key[-1] for e in second.entries] == ["d", "c"]


def test_views_type_filter_and_search(db):
    feed = _seed(
        db,
        _insight("a", stored=3, type="gotcha", text="SQLite upserts need a key."),
        _insight("b", stored=2, type="repo", tags=["pinia"]),
        _insight("c", stored=1, type="repo", branch="feat/web-feed"),
    )
    feed.set_saved("insight:b", True)
    feed.set_hidden("insight:c", True)

    assert _keys(feed) == ["a", "b"]
    assert _keys(feed, view="saved") == ["b"]
    assert _keys(feed, view="hidden") == ["c"]
    assert _keys(feed, type="repo") == ["b"]
    assert _keys(feed, query="sqlite") == ["a"]  # case-insensitive text
    assert _keys(feed, query="pinia") == ["b"]  # tags
    assert _keys(feed, view="hidden", query="web-feed") == ["c"]  # branch
    assert _keys(feed, query="%") == []  # LIKE wildcards are literal
    assert feed.types() == [("gotcha", 1), ("repo", 1)]  # hidden not counted


def test_seen_is_first_sight_only_and_drives_unseen_count(db):
    feed = _seed(db, _insight("a", stored=2), _insight("b", stored=1))
    assert feed.unseen_count() == 2
    assert all(e.new for e in feed.page().entries)

    feed.mark_seen(["insight:a"], now="2026-11-02T00:00:00.000000+00:00")
    feed.mark_seen(["insight:a"], now="2026-11-09T00:00:00.000000+00:00")
    entries = {e.key: e for e in feed.page().entries}
    assert not entries["insight:a"].new and entries["insight:b"].new
    assert feed.unseen_count() == 1

    feed.set_hidden("insight:b", True)
    assert feed.unseen_count() == 0


def test_count_newer_than_a_loaded_top(db):
    feed = _seed(db, _insight("a", stored=1))
    top = feed.page().entries[0].cursor
    assert feed.count_newer_than(top) == 0
    InsightStore(db).add(_insight("b", stored=2))
    InsightStore(db).add(_insight("c", stored=3))
    assert feed.count_newer_than(top) == 2


def test_unknown_key_and_bad_inputs(db):
    feed = _seed(db, _insight("a"))
    assert feed.set_saved("insight:missing", True) is False
    assert feed.set_hidden("insight:missing", True) is False
    with pytest.raises(ValueError):
        feed.page(view="everything")
    with pytest.raises(ValueError):
        feed.page(cursor="not-a-cursor")
    with pytest.raises(ValueError):
        feed.count_newer_than("yesterday|insight:a")


def test_reader_state_leaves_telegram_delivery_untouched(db):
    feed = _seed(db, _insight("a"))
    feed.mark_seen(["insight:a"])
    feed.set_saved("insight:a", True)
    feed.set_hidden("insight:a", True)

    store = InsightStore(db)
    assert store.get("insight:a").status == "new"
    # The pacer-driven outbox still queues it exactly as before.
    assert enqueue_new_insights(store, Outbox(db)) == 1
    assert Outbox(db).stats()["pending"] == 1
