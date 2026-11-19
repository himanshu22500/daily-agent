"""Questions asked under feed items and answered by Claude (issue #119).
Offline: a fake answerer and a fake Telegram sender."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_deep_dive import make_folder

from daily_agent.agents.question_answerer import AskRequest
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep import PrepSync, card_key
from daily_agent.feed.questions import (
    MAX_OPEN,
    MAX_QUESTION_CHARS,
    NOTIFY_RETRY,
    NOTIFY_WINDOW,
    QuestionError,
    QuestionStore,
    QuestionSync,
    telegram_text,
)
from daily_agent.feed.story_queue import MAX_ATTEMPTS, RETRY_BASE
from daily_agent.models import Insight

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
FEED = "http://daily-agent.example.ts.net"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Answerer:
    def __init__(self) -> None:
        self.requests: list[AskRequest] = []
        self.fail = False

    async def __call__(self, request: AskRequest) -> str:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("claude error 529: overloaded\nmore detail")
        return f"Answer to {request.question}\n\n**Short** version: yes."


class Telegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail = False

    def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("Too Many Requests")
        self.sent.append(text)


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ask.db")
    InsightStore(path).add(
        Insight(
            key="insight:retries",
            text="Retry with backoff, or a usage limit turns into a storm.",
            title="Back off before you retry",
            type="gotcha",
            tags=["retries"],
            source_session="s1",
            captured_at=T0,
        )
    )
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def answerer() -> Answerer:
    return Answerer()


@pytest.fixture
def telegram() -> Telegram:
    return Telegram()


@pytest.fixture
def sync(db, clock, answerer, telegram) -> QuestionSync:
    return QuestionSync(db, answerer, notify=telegram, feed_url=FEED, clock=clock)


# --- asking ------------------------------------------------------------------- #
def test_a_question_is_trimmed_and_kept_with_its_item(db):
    store = QuestionStore(db)
    asked = store.ask("insight:retries", "  Why   back off?  \n\n\n\nReally? ", now=T0)
    assert asked.question == "Why back off?\n\nReally?"
    assert asked.is_open and not asked.answered
    assert [q.id for q in store.thread("insight:retries")] == [asked.id]
    assert store.thread("insight:other") == []


@pytest.mark.parametrize("text", ["", "   \n ", "x" * (MAX_QUESTION_CHARS + 1)])
def test_empty_or_huge_questions_are_refused(db, text):
    with pytest.raises(QuestionError) as caught:
        QuestionStore(db).ask("insight:retries", text, now=T0)
    assert caught.value.status == 400


def test_too_many_waiting_questions_are_refused(db):
    store = QuestionStore(db)
    for n in range(MAX_OPEN):
        store.ask("insight:retries", f"Question {n}?", now=T0)
    with pytest.raises(QuestionError) as caught:
        store.ask("insight:retries", "One more?", now=T0)
    assert caught.value.status == 429


def test_questions_are_answered_in_the_order_asked(db):
    store = QuestionStore(db)
    first = store.ask("insight:retries", "First?", now=T0)
    second = store.ask("insight:retries", "Second?", now=T0 + timedelta(seconds=5))
    assert [r.id for r in store.ready(now=T0 + timedelta(minutes=1), limit=5)] == [
        first.id,
        second.id,
    ]
    assert [q.id for q in store.thread("insight:retries")] == [first.id, second.id]


# --- answering ---------------------------------------------------------------- #
async def test_an_answer_is_stored_and_telegram_links_to_it(sync, answerer, telegram):
    asked = sync.store.ask("insight:retries", "Why back off?", now=T0)
    report = await sync.run_once()
    assert (report.written, report.remaining) == (1, 0)

    (request,) = answerer.requests
    assert request.question == "Why back off?"
    assert request.headline == "Back off before you retry"
    assert request.text.startswith("Retry with backoff")
    assert request.kind == "an insight the reader learned during a coding session"
    assert request.tags == ["retries"] and request.notes == "" and request.earlier == []

    (answered,) = sync.store.thread("insight:retries")
    assert answered.answered and answered.answered_at == T0
    assert answered.answer.startswith("Answer to Why back off?")

    (message,) = telegram.sent
    assert message.startswith(
        "Claude answered your question about “Back off before you retry”\n\n"
    )
    assert "Q: Why back off?" in message
    assert "Short version: yes." in message  # markdown markers dropped
    assert message.endswith(
        f"Read it in your feed: {FEED}/i/insight%3Aretries#ask-{asked.id}"
    )

    await sync.run_once()  # nothing new: no second message
    assert len(telegram.sent) == 1 and len(answerer.requests) == 1


async def test_a_follow_up_sees_the_earlier_answer(sync, answerer, clock):
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    await sync.run_once()
    clock.advance(timedelta(minutes=2))
    sync.store.ask("insight:retries", "How long should I wait?", now=clock())
    await sync.run_once()
    follow_up = answerer.requests[-1]
    assert follow_up.question == "How long should I wait?"
    assert follow_up.earlier == [
        ("Why back off?", "Answer to Why back off?\n\n**Short** version: yes.")
    ]


async def test_interview_questions_come_with_the_resume_notes(
    db, tmp_path, clock, answerer, telegram
):
    async def no_explainers(request):
        raise AssertionError("not written here")

    PrepSync(db, no_explainers).import_folder(make_folder(tmp_path))
    sync = QuestionSync(db, answerer, notify=telegram, feed_url=FEED, clock=clock)
    sync.store.ask(card_key("T5", 4), "What would they ask next?", now=T0)
    await sync.run_once()
    (request,) = answerer.requests
    assert request.headline == "How did you make the SQL safe?"
    assert request.kind.startswith("an interview flashcard")
    assert request.source == "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7"
    assert "Led the production" in request.resume_line
    assert "## 4. Likely interview questions" in request.notes


async def test_a_failed_answer_is_retried_then_given_up(
    sync, answerer, clock, telegram
):
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    answerer.fail = True
    await sync.run_once()
    (q,) = sync.store.thread("insight:retries")
    assert q.retrying and q.error == "claude error 529: overloaded"
    assert q.retry_at == T0 + RETRY_BASE

    await sync.run_once()  # not before the retry time
    assert len(answerer.requests) == 1

    for attempt in range(1, MAX_ATTEMPTS):
        clock.advance(RETRY_BASE * 2 ** (attempt - 1))
        report = await sync.run_once()
    assert report.failed == 1
    (q,) = sync.store.thread("insight:retries")
    assert q.failed and not q.is_open
    assert telegram.sent == []


async def test_a_question_about_a_removed_item_is_given_up(sync, answerer, db):
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    InsightStore(db).delete(["insight:retries"])
    report = await sync.run_once()
    assert report.failed == 1 and answerer.requests == []
    (q,) = sync.store.thread("insight:retries")
    assert q.failed and q.error == "the item is no longer in the feed"


async def test_a_failed_telegram_message_is_sent_again_later(sync, telegram, clock):
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    telegram.fail = True
    await sync.run_once()
    assert telegram.sent == []
    assert sync.store.thread("insight:retries")[0].answered  # answered regardless

    telegram.fail = False
    await sync.run_once()  # too soon to try again
    assert telegram.sent == []

    clock.advance(NOTIFY_RETRY)
    await sync.run_once()
    assert len(telegram.sent) == 1
    clock.advance(NOTIFY_RETRY * 5)
    await sync.run_once()
    assert len(telegram.sent) == 1


async def test_old_unsent_messages_are_dropped(sync, telegram, clock):
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    telegram.fail = True
    await sync.run_once()
    telegram.fail = False
    clock.advance(NOTIFY_WINDOW + timedelta(minutes=1))
    await sync.run_once()
    assert telegram.sent == []


async def test_without_telegram_answers_still_land(db, answerer, clock):
    sync = QuestionSync(db, answerer, clock=clock)
    sync.store.ask("insight:retries", "Why back off?", now=T0)
    report = await sync.run_once()
    assert report.written == 1
    assert sync.store.thread("insight:retries")[0].answered


def test_telegram_text_keeps_list_lines(db):
    store = QuestionStore(db)
    asked = store.ask("insight:retries", "Why?", now=T0)
    store.record_answer(asked.id, "Two ways:\n• **one**\n• `two`\n\nDone.", now=T0)
    text = telegram_text(store.get(asked.id), "Is it “safe”?", "")
    assert text.startswith("Claude answered your question about “Is it “safe”?”\n\n")
    assert "\n\nTwo ways:\n• one\n• two\n\nDone.\n\n" in text


def test_telegram_text_without_a_feed_url_and_a_long_answer(db):
    store = QuestionStore(db)
    asked = store.ask("insight:retries", "Why?", now=T0)
    store.record_answer(asked.id, "word " * 600, now=T0)
    text = telegram_text(store.get(asked.id), "", "")
    assert text.startswith("Claude answered your question about an item in your feed.")
    assert text.endswith("It's in your feed under the item.")
    assert "…" in text and len(text) < 1500


def test_video_stories_are_described_as_such(db):
    """``describe`` names what the item is (here: a YouTube story)."""
    from daily_agent.feed.questions import describe
    from daily_agent.feed.web_feed import WebFeed

    InsightStore(db).add(
        Insight(
            key="video:dQw4w9WgXcQ",
            text="A story.",
            type="video",
            captured_at=T0,
        )
    )
    entry = WebFeed(db).entry("video:dQw4w9WgXcQ")
    assert describe(entry).startswith("a story Claude wrote from a YouTube video")
