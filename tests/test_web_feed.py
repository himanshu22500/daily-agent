"""Web feed store: publish-time ordering, keyset paging, filters, reader state."""

from __future__ import annotations

import random
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


def test_views_count_once_per_session_and_drive_unseen_count(db):
    feed = _seed(db, _insight("a", stored=2), _insight("b", stored=1))
    assert feed.unseen_count() == 2
    assert all(e.new for e in feed.page().entries)

    assert feed.record_views(["insight:a", "insight:a"], session_id="s1", now=T0) == 1
    assert feed.record_views(["insight:a"], session_id="s1", now=T0) == 0
    entries = {e.key: e for e in feed.page().entries}
    assert not entries["insight:a"].new and entries["insight:b"].new
    assert entries["insight:a"].view_count == 1
    assert feed.unseen_count() == 1
    # Unknown keys are ignored, not created.
    assert feed.record_views(["insight:missing"], session_id="s1") == 0

    feed.set_hidden("insight:b", True)
    assert feed.unseen_count() == 0


def test_each_view_schedules_a_growing_revisit_gap(db):
    feed = _seed(db, _insight("a"), _insight("s"))
    feed.set_saved("insight:s", True)
    for i, session in enumerate(("s1", "s2", "s3")):
        feed.record_views(
            ["insight:a", "insight:s"], session_id=session, now=T0 + timedelta(days=i)
        )
    # Third view: plain gap is 7 days; saved insights come back sooner (4 days).
    assert feed.next_revisit_at(now=T0) == T0 + timedelta(days=2 + 4)
    feed.set_hidden("insight:s", True)
    assert feed.next_revisit_at(now=T0) == T0 + timedelta(days=2 + 7)


def test_count_published_after(db):
    feed = _seed(db, _insight("a", stored=1))
    opened = T0 + timedelta(minutes=1, seconds=30)
    assert feed.count_published_after(opened) == 0
    InsightStore(db).add(_insight("b", stored=2))
    InsightStore(db).add(_insight("c", stored=3))
    assert feed.count_published_after(opened) == 2


def test_unknown_key_and_bad_inputs(db):
    feed = _seed(db, _insight("a"))
    assert feed.set_saved("insight:missing", True) is False
    assert feed.set_hidden("insight:missing", True) is False
    with pytest.raises(ValueError):
        feed.page(view="everything")
    with pytest.raises(ValueError):
        feed.page(cursor="not-a-cursor")
    with pytest.raises(ValueError):
        feed.page(view="for-you")  # editions, not keyset pages
    with pytest.raises(KeyError):
        feed.edition_page("no-such-edition")


def test_reader_state_leaves_telegram_delivery_untouched(db):
    feed = _seed(db, _insight("a"))
    feed.record_views(["insight:a"], session_id="s1")
    feed.set_saved("insight:a", True)
    feed.set_hidden("insight:a", True)

    store = InsightStore(db)
    assert store.get("insight:a").status == "new"
    # The pacer-driven outbox still queues it exactly as before.
    assert enqueue_new_insights(store, Outbox(db)) == 1
    assert Outbox(db).stats()["pending"] == 1


def test_pre_80_state_gains_view_columns_and_render_seen_marks_reset(db):
    # A web_feed_state from #77, where seen_at meant "was rendered".
    InsightStore(db).add(_insight("a"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE web_feed_state (key VARCHAR PRIMARY KEY, seen_at VARCHAR, "
            "saved_at VARCHAR, dismissed_at VARCHAR)"
        )
        conn.execute(
            "INSERT INTO web_feed_state VALUES ('insight:a', '2026-11-11T10:00:00', "
            "'2026-11-11T10:00:00', NULL)"
        )
    feed = WebFeed(db)
    entry = feed.page().entries[0]
    assert entry.new and entry.saved and entry.view_count == 0
    # Idempotent: reopening never resets real views again.
    feed.record_views(["insight:a"], session_id="s1")
    assert not WebFeed(db).page().entries[0].new


def _edition_keys(feed: WebFeed, edition: str, limit: int = 2) -> list[str]:
    keys, offset = [], 0
    while offset is not None:
        page = feed.edition_page(edition, offset=offset, limit=limit)
        keys += [e.key.removeprefix("insight:") for e in page.entries]
        offset = page.next_offset
    return keys


def test_edition_skips_seen_until_due_and_never_hidden(db):
    feed = _seed(db, *[_insight(k, stored=i) for i, k in enumerate("abcde")])
    feed.record_views(["insight:a", "insight:b"], session_id="s1", now=T0)
    feed.set_hidden("insight:c", True)

    edition = feed.new_edition(now=T0 + timedelta(hours=1), rng=random.Random(1))
    assert sorted(_edition_keys(feed, edition)) == ["d", "e"]

    # A day later the first-view gap (1 day) has elapsed: a and b come back.
    later = feed.new_edition(now=T0 + timedelta(days=1, hours=1), rng=random.Random(1))
    assert sorted(_edition_keys(feed, later)) == ["a", "b", "d", "e"]


def test_edition_order_is_frozen_while_state_changes(db):
    feed = _seed(db, *[_insight(k, stored=i) for i, k in enumerate("abcdef")])
    edition = feed.new_edition(rng=random.Random(7))
    first = feed.edition_page(edition, limit=2)
    # Viewing page 1 doesn't reshuffle or drop anything from later pages.
    feed.record_views([e.key for e in first.entries], session_id=edition)
    rest = _edition_keys(feed, edition)
    assert len(rest) == 6 and len(set(rest)) == 6
    # Hiding mid-scroll removes just that card from later pages.
    feed.set_hidden("insight:" + rest[-1], True)
    assert _edition_keys(feed, edition) == rest[:-1]


def test_each_open_is_a_different_order(db):
    feed = _seed(db, *[_insight(k, stored=i) for i, k in enumerate("abcdefghij")])
    orders = {
        tuple(_edition_keys(feed, feed.new_edition(rng=random.Random(seed))))
        for seed in range(8)
    }
    assert len(orders) > 1


def test_edition_type_filter_and_old_editions_are_pruned(db):
    feed = _seed(db, _insight("g", type="gotcha"), _insight("r", type="repo", stored=1))
    edition = feed.new_edition(type="repo", now=T0)
    assert _edition_keys(feed, edition) == ["r"]
    feed.new_edition(now=T0 + timedelta(days=4))
    with pytest.raises(KeyError):
        feed.edition_page(edition)


def _rel(key, tags, *, type="repo", session="", stored=0):
    return Insight(
        key=f"insight:{key}",
        text=f"Lesson {key}.",
        type=type,
        tags=tags,
        source_session=session or f"sess-{key}",
        captured_at=T0,
        stored_at=T0 + timedelta(minutes=stored),
    )


def test_related_ranks_by_shared_tags_then_project_then_type(db):
    store = InsightStore(db)
    for insight in (
        _rel("cur", ["postgres", "sql"], type="gotcha", session="s-cur"),
        _rel("both", ["postgres", "sql"]),  # 2 tags: 6 +1 unseen
        _rel("one", ["postgres"], type="gotcha"),  # 3 +1 type +1 unseen
        _rel("proj", ["zzz"], session="s-proj"),  # 1.5 project +1 unseen
        _rel("type", ["other"], type="gotcha"),  # 1 type +1 unseen
        _rel("none", ["zzz"], type="technique"),  # 1 unseen
        _rel("gone", ["postgres", "sql"]),  # hidden: never shown
    ):
        store.add(insight)
    # s-cur and s-proj are transcripts from the same project directory.
    for session in ("s-cur", "s-proj"):
        store.set_cursor(f"/x/projects/-nowhere-zz-repo-x/{session}.jsonl", 1)
    feed = WebFeed(db)
    feed.set_hidden("insight:gone", True)
    related = [e.key.removeprefix("insight:") for e in feed.related("insight:cur")]
    assert related == ["both", "one", "proj", "type"]
    assert "cur" not in related and "gone" not in related
    assert feed.related("insight:missing") == []


def test_related_prefers_unread_and_caps_one_session(db):
    store = InsightStore(db)
    store.add(_rel("cur", ["a", "b"], type="gotcha"))
    for i in range(3):  # near-duplicates from one session: only two may show
        store.add(_rel(f"dup{i}", ["a", "b"], session="same", stored=i))
    store.add(_rel("fresh", ["a"]))
    store.add(_rel("read-today", ["a"]))
    store.add(_rel("read-long-ago", ["a"]))
    feed = WebFeed(db)
    now = datetime.now(timezone.utc)
    feed.record_views(["insight:read-today"], session_id="v1", now=now)
    feed.record_views(
        ["insight:read-long-ago"], session_id="v2", now=now - timedelta(days=3)
    )
    keys = [
        e.key.removeprefix("insight:")
        for e in feed.related("insight:cur", limit=10, now=now)
    ]
    assert keys[:2] == ["dup2", "dup1"] and "dup0" not in keys
    assert keys.index("fresh") < keys.index("read-long-ago") < keys.index("read-today")
