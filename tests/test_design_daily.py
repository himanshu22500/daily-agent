"""The daily system design queue (issue #134). Offline: a fake writer stands
in for Claude, and a fake clock runs the days."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import design_writer
from daily_agent.agents.claude_code import ClaudeCodeError
from daily_agent.agents.design_writer import DesignCard
from daily_agent.feed.design_daily import (
    SOURCE_LABEL,
    DesignSync,
    WithBackground,
    card_key,
    order_topics,
)
from daily_agent.feed.design_topics import BLOCK, BY_SLUG, CASE, TOPICS
from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.prep_grades import MISSED, GradeStore
from daily_agent.feed.prep_tracks import PrepItems, split_steps
from daily_agent.feed.story_queue import FAILED, MAX_ATTEMPTS, SKIPPED, SyncReport

IST = timezone(timedelta(hours=5, minutes=30))
# 10:00 in India.
NOW = datetime(2026, 11, 25, 4, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(tmp_path):
    db = str(tmp_path / "design.db")
    calls: list[str] = []  # every writer call, failed ones too
    failing: set[str] = set()
    clock = Clock()

    async def writer(topic):
        calls.append(topic.slug)
        if topic.slug in failing:
            raise ClaudeCodeError("claude exited 1: usage limit\nmore detail")
        return DesignCard(
            topic.title,
            f"Prompt for {topic.slug}, take {calls.count(topic.slug)}.",
            (("Requirements", "- Users can order"), ("30-second recap", "Done.")),
        )

    def make(**kwargs) -> DesignSync:
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("tz", IST)
        return DesignSync(db, writer, **kwargs)

    return SimpleNamespace(db=db, calls=calls, failing=failing, clock=clock, make=make)


def _slugs(topics) -> list[str]:
    return [t.slug for t in topics]


def _written(db: str) -> set[str]:
    return {
        row.key.removeprefix(card_key(""))
        for row in InsightStore(db).all()
        if row.key.startswith(card_key(""))
    }


# --- order ------------------------------------------------------------------- #


def test_blinkit_first_then_classics_then_the_rest():
    cases = [t for t in TOPICS if t.kind == CASE]
    ordered = order_topics(cases)
    groups = [0 if t.blinkit else 1 if t.classic else 2 for t in ordered]
    assert groups == sorted(groups) and set(groups) == {0, 1, 2}
    # The list's own order within a group.
    assert _slugs(ordered)[:3] == [
        "dark-store-inventory",
        "rider-dispatch",
        "delivery-eta",
    ]
    assert _slugs(ordered)[groups.index(1)] == "url-shortener"


def test_weak_tags_come_first_weakest_first():
    cases = [t for t in TOPICS if t.kind == CASE]
    ordered = _slugs(order_topics(cases, ["search", "geospatial"]))
    assert ordered[:9] == [
        # touch "search", the weakest, in list order (whatever their group)
        "grocery-autocomplete",
        "ticket-booking",
        "search-typeahead",
        "hotel-booking",
        # then "geospatial"
        "rider-dispatch",
        "delivery-eta",
        "order-tracking",
        "ride-hailing",
        # then the other Blinkit ones
        "dark-store-inventory",
    ]
    blocks = [t for t in TOPICS if t.kind == BLOCK]
    # A block matches by its own slug or the blocks it builds on.
    assert _slugs(order_topics(blocks, ["sharding"]))[:3] == [
        "sharding",
        "consistent-hashing",
        "caching",
    ]


# --- daily pacing -------------------------------------------------------------- #


async def test_one_case_study_and_one_building_block_a_day(setup):
    sync = setup.make()
    report = await sync.run_once()
    assert (report.queued, report.written, report.remaining) == (2, 2, 0)
    assert setup.calls == ["caching", "dark-store-inventory"]

    store = InsightStore(setup.db)
    card = store.get(card_key("dark-store-inventory"))
    assert card.type == "design" and card.status == "web-only"
    assert card.title == "Design dark-store inventory with stock reservations"
    assert card.tags == [
        "dark-store-inventory",
        "idempotency",
        "distributed-locks",
        "caching",
        "sharding",
        "blinkit",
    ]
    prompt, steps = split_steps(card.text)
    assert prompt.startswith("Prompt for dark-store-inventory")
    assert [s.title for s in steps] == ["Requirements", "30-second recap"]
    assert store.get(card_key("caching")).tags == ["caching"]
    assert PrepItems(setup.db).labels()[card.source_session] == SOURCE_LABEL
    # Web only: nothing goes to Telegram.
    assert enqueue_new_insights(store, Outbox(setup.db)) == 0

    # The cheat sheet goes in too, without a model call.
    sheet = store.get(card_key("latency-numbers"))
    assert sheet.type == "design" and sheet.status == "web-only"
    assert sheet.tags == ["latency-numbers", "estimation"]
    assert [s.title for s in split_steps(sheet.text)[1]][0] == "Latency"

    # The rest of the day brings nothing new.
    setup.clock.now += timedelta(hours=10)
    again = await sync.run_once()
    assert (again.queued, again.written) == (0, 0)
    assert len(setup.calls) == 2 and not sync.put_cheat_sheet()
    assert sync.progress().today == Counter({CASE: 1, BLOCK: 1})


async def test_days_start_at_local_midnight_and_sleep_is_not_made_up(setup):
    setup.clock.now = datetime(2026, 11, 25, 17, 0, tzinfo=timezone.utc)  # 22:30
    sync = setup.make()
    assert (await sync.run_once()).written == 2
    setup.clock.now += timedelta(hours=1)  # 23:30, the same day in India
    assert (await sync.run_once()).written == 0
    setup.clock.now += timedelta(hours=1)  # 00:30, the next day
    assert (await sync.run_once()).written == 2
    assert setup.calls[2:] == ["rider-dispatch", "sql-vs-nosql"]
    # The Mac sleeps three days: on waking, one day's cards, not four.
    setup.clock.now += timedelta(days=3, hours=8)
    report = await sync.run_once()
    assert (report.queued, report.written) == (2, 2)
    progress = sync.progress()
    assert (progress.written, progress.queued, progress.topics) == (6, 0, len(TOPICS))


async def test_cards_per_day_come_from_settings(setup):
    sync = setup.make(cases_per_day=2, blocks_per_day=0)
    assert (await sync.run_once()).written == 2
    assert sorted(setup.calls) == ["dark-store-inventory", "rider-dispatch"]
    assert _slugs(sync.schedule(3)) == ["delivery-eta", "flash-sale", "cart-checkout"]
    both = setup.make(cases_per_day=1, blocks_per_day=2)
    assert [t.kind for t in both.schedule(6)] == [CASE, BLOCK, BLOCK] * 2


async def test_a_written_topic_is_never_picked_again(setup):
    topics = [BY_SLUG["url-shortener"], BY_SLUG["rate-limiter"], BY_SLUG["caching"]]
    sync = setup.make(topics=topics)
    for _ in range(4):
        await sync.run_once()
        setup.clock.now += timedelta(days=1)
    assert setup.calls == ["caching", "url-shortener", "rate-limiter"]
    assert sync.schedule(5) == [] and sync.queue_today() == 0
    progress = sync.progress()
    assert (progress.written, progress.next_up) == (3, [])
    assert _written(setup.db) == {
        "url-shortener",
        "rate-limiter",
        "caching",
        "latency-numbers",
    }


# --- retries ------------------------------------------------------------------- #


async def test_failures_back_off_then_wait_behind_untried_topics(setup):
    topics = [
        BY_SLUG["url-shortener"],
        BY_SLUG["rate-limiter"],
        BY_SLUG["news-feed"],
        BY_SLUG["caching"],
    ]
    setup.clock.now = datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)  # 00:30
    setup.failing.add("url-shortener")
    sync = setup.make(topics=topics)
    report = await sync.run_once()
    # One topic failing never stops the other.
    assert (report.written, report.failed, report.remaining) == (1, 0, 0)
    assert report.errors == ["url-shortener: writing: claude exited 1: usage limit"]
    setup.clock.now += timedelta(minutes=30)  # not yet: retries wait an hour
    assert (await sync.run_once()).errors == []
    for wait in (1, 2, 4):  # hours, as StoryQueue backs off
        setup.clock.now += timedelta(hours=wait) - (
            timedelta(minutes=30) if wait == 1 else timedelta(0)
        )
        report = await sync.run_once()
    assert setup.calls.count("url-shortener") == MAX_ATTEMPTS
    assert report.failed == 1
    assert sync.store.row("url-shortener").state == FAILED
    assert sync.progress().failed == [
        ("url-shortener", "writing: claude exited 1: usage limit")
    ]
    # Untried topics go first on the next days; then the failed one comes back.
    setup.failing.clear()
    for _ in range(3):
        setup.clock.now += timedelta(days=1)
        await sync.run_once()
    assert [c for c in setup.calls if c != "url-shortener"] == [
        "caching",
        "rate-limiter",
        "news-feed",
    ]
    assert setup.calls[-1] == "url-shortener"
    assert sync.progress().failed == [] and sync.progress().written == 4


async def test_a_topic_dropped_from_the_list_is_skipped(setup):
    first = setup.make(topics=[BY_SLUG["url-shortener"]])
    assert first.queue_today() == 1
    later = setup.make(topics=[BY_SLUG["rate-limiter"]])
    report = await later.run_once()
    assert report.written == 0 and setup.calls == []
    assert later.store.row("url-shortener").state == SKIPPED


# --- by hand ------------------------------------------------------------------- #


async def test_write_a_topic_again_by_hand_in_place(setup):
    sync = setup.make()
    await sync.run_once()
    key = card_key("dark-store-inventory")
    store = InsightStore(setup.db)
    before = store.get(key)
    GradeStore(setup.db).grade(key, MISSED, now=setup.clock.now)

    setup.clock.now += timedelta(hours=1)
    report = await sync.write_topic("dark-store-inventory")
    assert (report.written, report.queued) == (1, 1)
    after = store.get(key)
    assert "take 2." in after.text and after.captured_at == before.captured_at
    assert GradeStore(setup.db).latest([key])[key].grade == MISSED

    # An unwritten topic by hand is an extra: today's pace is unchanged, and
    # the days never bring it again.
    assert (await sync.write_topic("url-shortener")).written == 1
    assert sync.progress().today == Counter({CASE: 1, BLOCK: 1})
    assert "url-shortener" not in _slugs(sync.schedule(len(TOPICS)))
    with pytest.raises(KeyError):
        await sync.write_topic("no-such-topic")


async def test_a_limit_writes_ahead_of_the_daily_pace(setup):
    sync = setup.make()
    report = await sync.run_once(limit=4, ahead=True)
    assert (report.queued, report.written, report.remaining) == (4, 4, 0)
    assert sorted(setup.calls) == [
        "caching",
        "dark-store-inventory",
        "rider-dispatch",
        "sql-vs-nosql",
    ]
    # Extras don't use up a day: tomorrow still brings its own two.
    assert sync.progress().today == Counter({CASE: 1, BLOCK: 1})
    setup.clock.now += timedelta(days=1)
    await sync.run_once()
    assert setup.calls[4:] == ["delivery-eta", "indexes"]


# --- weak spots -------------------------------------------------------------- #


async def test_missed_design_cards_steer_the_next_picks(setup):
    sync = setup.make()
    await sync.run_once()
    grades = GradeStore(setup.db)
    for slug in ("caching", "dark-store-inventory"):
        grades.grade(card_key(slug), MISSED, now=setup.clock.now)
    # "blinkit" names where a card is from, not a skill.
    weak = sync.weak_tags()
    assert weak[0] == "caching" and "blinkit" not in weak
    assert "dark-store-inventory" in weak
    # Without grades the next day brings rider-dispatch and sql-vs-nosql.
    setup.clock.now += timedelta(days=1)
    await sync.run_once()
    assert setup.calls[2:] == ["consistent-hashing", "delivery-eta"]
    assert sync.progress().weak == weak


async def test_grades_unavailable_never_block_the_day(setup):
    def broken():
        raise RuntimeError("grades table is locked")

    sync = setup.make(weak_tags=broken)
    assert sync.weak_tags() == []
    assert (await sync.run_once()).written == 2


# --- the background loop ------------------------------------------------------- #


class Passes:
    def __init__(self) -> None:
        self.count = 0
        self.ran = threading.Event()
        self.task: asyncio.Task | None = None

    async def run_once(self) -> SyncReport:
        self.count += 1
        self.task = asyncio.current_task()
        self.ran.set()
        return SyncReport()


def test_the_loop_runs_while_the_feed_is_up():
    def home(_request):
        return PlainTextResponse("feed")

    passes = Passes()
    app = WithBackground(Starlette(routes=[Route("/", home)]), passes, idle=3600)
    with TestClient(app) as client:
        assert client.get("/").text == "feed"
        assert passes.ran.wait(5)
    assert passes.count == 1  # then it idles until the next look
    assert passes.task is not None and passes.task.done()


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Run `daily-agent web` with create_app and uvicorn faked; return the app
    it would serve."""
    feed = object()
    seen: dict = {}

    import uvicorn

    import daily_agent.web.app as web_app

    monkeypatch.setattr(web_app, "create_app", lambda db, **kwargs: feed)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "w.db"))

    def run():
        cli.get_settings.cache_clear()
        try:
            result = CliRunner().invoke(cli.app, ["web"])
        finally:
            cli.get_settings.cache_clear()
        assert result.exit_code == 0, result.output
        return seen["app"]

    return run, feed


def test_web_writes_design_cards_only_with_claude_and_the_setting(
    served, tmp_path, monkeypatch
):
    run, feed = served
    # conftest points DAILY_AGENT_CLAUDE_BIN at a path that doesn't exist (the
    # feed server has no claude CLI either).
    assert run() is feed
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("DAILY_AGENT_PREP_DESIGN_PER_DAY", "2")
    app = run()
    assert isinstance(app, WithBackground) and app.app is feed
    assert app.sync.per_day == {CASE: 2, BLOCK: 1}
    monkeypatch.setenv("DAILY_AGENT_WEB_PREP_DESIGN_SYNC", "false")
    assert run() is feed


# --- CLI ------------------------------------------------------------------------- #


@pytest.fixture
def prep_design(tmp_path, monkeypatch):
    db = tmp_path / "cli.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(db))
    models: list[str] = []

    async def fake_write_card(model, topic):
        models.append(model)
        return DesignCard(topic.title, f"About {topic.slug}.", (("Answer", "A."),))

    monkeypatch.setattr(design_writer, "write_card", fake_write_card)
    cli.get_settings.cache_clear()

    def invoke(*args: str):
        result = CliRunner().invoke(cli.app, ["prep", "design", *args])
        result.text = " ".join(result.output.split())
        return result

    yield invoke, str(db), models
    cli.get_settings.cache_clear()


def test_cli_status_and_write(prep_design):
    invoke, db, models = prep_design
    status = invoke("status")
    assert status.exit_code == 0, status.output
    assert (
        f"System design: 0 of {len(TOPICS)} topic(s) written, 0 queued, 0 failed. "
        "Today: 0 of 1 case study(ies), 0 of 1 building block(s)." in status.text
    )
    assert "Next up: dark-store-inventory Design dark-store inventory" in status.text

    wrote = invoke("write")
    assert wrote.exit_code == 0, wrote.output
    assert "Wrote 2 card(s)" in wrote.text
    assert models == ["claude-code:claude-opus-5-5"] * 2

    ahead = invoke("write", "--limit", "2", "--model", "claude-code:other")
    assert "Wrote 2 card(s)" in ahead.text and models[-1] == "claude-code:other"
    one = invoke("write", "--topic", "url-shortener")
    assert "Wrote 1 card(s)" in one.text
    card = InsightStore(db).get(card_key("url-shortener"))
    assert card.text.startswith("About url-shortener.\n\n### Answer")

    listed = invoke("status", "--all")
    assert "5 of " in listed.text and "Today: 1 of 1 case study(ies)" in listed.text
    assert "done case url-shortener Design a URL shortener" in listed.text
    assert "- case news-feed Design a news feed" in listed.text

    missing = invoke("write", "--topic", "nope")
    assert missing.exit_code == 1 and "No topic 'nope'" in missing.text
