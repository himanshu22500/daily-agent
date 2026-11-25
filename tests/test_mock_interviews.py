"""Mock interviews on prep cards (issue #133): the turn store and the Mac's
side. Offline: a fake interviewer and a fake Telegram sender."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_deep_dive import make_folder

from daily_agent.agents.mock_interviewer import (
    NEXT,
    OPEN,
    VERDICT,
    InterviewReply,
    InterviewRequest,
)
from daily_agent.feed import mock_interviews
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.mock_interviews import (
    GRADE,
    MAX_QUESTIONS,
    NOTIFY_RETRY,
    REPLY,
    SLOW_TURN,
    InterviewError,
    InterviewStore,
    InterviewSync,
)
from daily_agent.feed.prep import PrepSync, card_key
from daily_agent.feed.questions import QuestionSync
from daily_agent.feed.story_queue import MAX_ATTEMPTS, RETRY_BASE
from daily_agent.models import Insight

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
FEED = "http://daily-agent.example.ts.net"
KEY = "prep:sd-cache"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta = timedelta(seconds=5)) -> datetime:
        self.now += delta
        return self.now


class Interviewer:
    def __init__(self) -> None:
        self.requests: list[InterviewRequest] = []
        self.fail = False

    async def __call__(self, request: InterviewRequest) -> InterviewReply:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("claude error 529: overloaded\nmore detail")
        if request.task == VERDICT:
            return InterviewReply(
                question="Solid on requirements, thin on failure.\n\n"
                "**What was missing**\n• Cache node failure",
                grade="shaky",
            )
        number = request.asked + 1
        return InterviewReply(
            question=f"Question {number}?",
            feedback=""
            if request.task == OPEN
            else f"Feedback on answer {number - 1}.",
        )


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
    path = str(tmp_path / "mock.db")
    InsightStore(path).add(
        Insight(
            key=KEY,
            text="Design a read-through cache for a product catalogue.",
            title="Design a product-catalogue cache",
            type="concept",
            tags=["caching", "system-design"],
            captured_at=T0,
        )
    )
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def interviewer() -> Interviewer:
    return Interviewer()


@pytest.fixture
def telegram() -> Telegram:
    return Telegram()


@pytest.fixture
def sync(db, clock, interviewer, telegram) -> InterviewSync:
    return InterviewSync(db, interviewer, notify=telegram, feed_url=FEED, clock=clock)


async def _start(sync: InterviewSync, clock: Clock):
    interview = sync.store.start(KEY, questions=sync.questions, now=clock())
    await sync.run_once()
    return interview


async def _answer(
    sync: InterviewSync, clock: Clock, interview_id: str, text: str, **kw
):
    turn = sync.store.answer(KEY, interview_id, text, now=clock.advance(), **kw)
    await sync.run_once()
    return turn


# --- starting ---------------------------------------------------------------- #
def test_starting_queues_the_opening_question(db):
    store = InterviewStore(db)
    interview = store.start(KEY, now=T0)
    assert interview.key == KEY and interview.questions == 4
    (opening,) = interview.turns
    assert opening.kind == "open" and opening.said == "" and opening.is_open
    assert interview.waiting and interview.active and not interview.can_answer
    assert interview.progress == "Question 1 of 4"
    assert [r.id for r in store.ready(now=T0, limit=5)] == [opening.id]


def test_starting_over_drops_a_turn_still_queued_for_the_mac(db):
    store = InterviewStore(db)
    first = store.start(KEY, now=T0)
    second = store.start(KEY, now=T0 + timedelta(seconds=1))
    old, new = store.for_item(KEY)
    assert (old.id, new.id) == (first.id, second.id)
    assert old.turns[0].skipped and not old.waiting and not old.active
    assert old.progress == "Stopped after 0 of 4"
    later = T0 + timedelta(seconds=2)
    assert [r.id for r in store.ready(now=later, limit=5)] == [new.turns[0].id]


def test_starting_over_waits_while_claude_writes_a_turn(db):
    store = InterviewStore(db)
    first = store.start(KEY, now=T0)
    assert store.claim(first.turns[0].id, now=T0)  # the Mac is on it
    with pytest.raises(InterviewError) as caught:
        store.start(KEY, now=T0 + timedelta(seconds=1))
    assert caught.value.status == 409
    (only,) = store.for_item(KEY)
    assert only.id == first.id and only.turns[0].writing


async def test_starting_over_waits_for_a_verdict_on_its_way(sync, clock):
    started = await _start(sync, clock)
    sync.store.answer(KEY, started.id, "Answer 1", end=True, now=clock.advance())
    with pytest.raises(InterviewError) as caught:
        sync.store.start(KEY, now=clock.advance())
    assert caught.value.status == 409
    (interview,) = sync.store.for_item(KEY)
    assert interview.grading and interview.turns[-1].state == "pending"


async def test_starting_over_drops_a_reply_waiting_to_be_retried(
    sync, clock, interviewer
):
    started = await _start(sync, clock)
    interviewer.fail = True
    await _answer(sync, clock, started.id, "Answer 1")
    retrying = sync.store.for_item(KEY)[0].turns[-1]
    assert retrying.retrying  # back in an hour: the reader needn't wait for it
    interviewer.fail = False
    second = sync.store.start(KEY, now=clock.advance())
    clock.advance(RETRY_BASE)
    await sync.run_once()
    old, new = sync.store.for_item(KEY)
    assert old.turns[-1].skipped and old.progress == "Stopped after 0 of 4"
    assert new.id == second.id and new.can_answer
    assert [r.task for r in interviewer.requests] == [OPEN, NEXT, OPEN]


@pytest.mark.parametrize("asked, kept", [(0, 1), (3, 3), (99, MAX_QUESTIONS)])
def test_the_question_count_is_kept_in_range(db, asked, kept):
    assert InterviewStore(db).start(KEY, questions=asked, now=T0).questions == kept


def test_too_many_waiting_turns_are_refused(db, monkeypatch):
    monkeypatch.setattr(mock_interviews, "MAX_OPEN", 1)
    store = InterviewStore(db)
    store.start(KEY, now=T0)
    with pytest.raises(InterviewError) as caught:
        store.start("prep:other", now=T0)
    assert caught.value.status == 429


# --- a whole interview -------------------------------------------------------- #
async def test_an_interview_runs_question_by_question_to_a_verdict(
    sync, clock, interviewer, telegram
):
    started = await _start(sync, clock)
    (interview,) = sync.store.for_item(KEY)
    assert interview.can_answer and interview.asked == 1
    assert interview.turns[0].reply == "Question 1?"

    for n in range(1, 4):
        await _answer(sync, clock, started.id, f"Answer {n}")
    interview = sync.store.for_item(KEY)[0]
    assert interview.asked == 4 and interview.progress == "Question 4 of 4"
    assert [t.kind for t in interview.turns] == ["open", REPLY, REPLY, REPLY]
    assert interview.turns[2].feedback == "Feedback on answer 2."

    # The answer to the last question ends it: Claude grades.
    after = sync.store.answer(KEY, started.id, "Answer 4", now=clock.advance())
    assert after.grading and after.turns[-1].kind == GRADE
    assert after.progress == "Grading…"
    await sync.run_once()

    interview = sync.store.for_item(KEY)[0]
    assert interview.graded and not interview.active
    assert interview.verdict.grade == "shaky"
    assert interview.verdict.grade_label == "Shaky"
    assert interview.progress == "Graded · 4 questions"

    tasks = [r.task for r in interviewer.requests]
    assert tasks == [OPEN, NEXT, NEXT, NEXT, VERDICT]
    last = interviewer.requests[-1]
    assert last.asked == 4 and last.questions == 4 and not last.ended_early
    assert last.transcript[0] == ("interviewer", "Question 1?")
    assert last.transcript[1] == ("candidate", "Answer 1")
    assert last.transcript[2] == (
        "interviewer",
        "Feedback on answer 1.\n\nQuestion 2?",
    )
    assert last.transcript[-1] == ("candidate", "Answer 4")
    assert last.headline == "Design a product-catalogue cache"
    assert last.tags == ["caching", "system-design"] and last.type == "concept"

    # Quick turns stay quiet; the verdict pings, with a link to it.
    (message,) = telegram.sent
    assert message.startswith(
        "Your mock interview on “Design a product-catalogue cache” is graded. "
        "Suggested self-grade: Shaky.\n\nSolid on requirements, thin on failure."
    )
    verdict = interview.turns[-1]
    assert message.endswith(
        f"Read the verdict: {FEED}/i/prep%3Asd-cache#turn-{verdict.id}"
    )

    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, "More?", now=clock.advance())
    assert caught.value.status == 409  # already graded


async def test_end_and_grade_early_with_or_without_an_answer(sync, clock, interviewer):
    started = await _start(sync, clock)
    await _answer(sync, clock, started.id, "Answer 1")
    await _answer(sync, clock, started.id, "Half an answer", end=True)
    request = interviewer.requests[-1]
    assert request.task == VERDICT and request.ended_early
    assert request.transcript[-1] == ("candidate", "Half an answer")
    assert sync.store.for_item(KEY)[0].graded

    clock.advance()
    second = await _start(sync, clock)
    await _answer(sync, clock, second.id, "Answer 1")
    await _answer(sync, clock, second.id, "  ", end=True)  # nothing typed
    request = interviewer.requests[-1]
    assert request.ended_early and request.transcript[-1][0] == "interviewer"
    ended = sync.store.for_item(KEY)[-1].turns[-1]
    assert ended.kind == GRADE and ended.said == "" and ended.done


async def test_ending_needs_at_least_one_answer(sync, clock):
    started = await _start(sync, clock)
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, "", end=True, now=clock.advance())
    assert caught.value.status == 400
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, " \n ", now=clock.advance())
    assert str(caught.value) == "Type your answer first."
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, "x" * 2001, now=clock.advance())
    assert "too long" in str(caught.value)


async def test_answers_wait_for_the_interviewer(sync, clock):
    started = await _start(sync, clock)
    sync.store.answer(KEY, started.id, "Answer 1", now=clock.advance())
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, "Answer 1 again", now=clock.advance())
    assert caught.value.status == 409
    assert "still on your last answer" in str(caught.value)


async def test_a_second_send_of_the_same_turn_is_refused(sync, clock, monkeypatch):
    """Two taps racing past the "still waiting" check: only one becomes the
    turn (the unique turn number, through RETURNING)."""
    started = await _start(sync, clock)
    stale = sync.store.for_item(KEY)
    sync.store.answer(KEY, started.id, "Answer 1", now=clock.advance())
    monkeypatch.setattr(sync.store, "for_item", lambda key: stale)
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, started.id, "Answer 1", now=clock.advance())
    assert caught.value.status == 409 and "already sent" in str(caught.value)
    monkeypatch.undo()
    assert len(sync.store.for_item(KEY)[0].turns) == 2


async def test_starting_over_stops_the_old_interview(sync, clock):
    first = await _start(sync, clock)
    await _answer(sync, clock, first.id, "Answer 1")
    clock.advance()
    second = await _start(sync, clock)
    old, new = sync.store.for_item(KEY)
    assert (old.id, new.id) == (first.id, second.id)
    assert not old.latest and not old.active
    assert old.progress == "Stopped after 1 of 4"
    assert new.active and new.can_answer
    with pytest.raises(InterviewError) as caught:
        sync.store.answer(KEY, first.id, "Answer 2", now=clock.advance())
    assert caught.value.status == 409
    with pytest.raises(InterviewError):
        sync.store.answer("prep:other", second.id, "Answer", now=clock.advance())


async def test_the_question_count_setting_decides_when_it_ends(db, clock, interviewer):
    sync = InterviewSync(db, interviewer, questions=2, clock=clock)
    started = await _start(sync, clock)
    await _answer(sync, clock, started.id, "Answer 1")
    after = sync.store.answer(KEY, started.id, "Answer 2", now=clock.advance())
    assert after.turns[-1].kind == GRADE
    await sync.run_once()
    assert interviewer.requests[1].task == NEXT
    assert interviewer.requests[-1].task == VERDICT
    assert not interviewer.requests[-1].ended_early


# --- Telegram ----------------------------------------------------------------- #
async def test_a_slow_reply_pings_telegram(sync, clock, telegram):
    started = sync.store.start(KEY, now=clock())
    clock.advance(SLOW_TURN + timedelta(seconds=1))  # the Mac was asleep
    await sync.run_once()
    (message,) = telegram.sent
    assert message.startswith(
        "The interviewer asked question 1 of 4 in your mock interview on "
        "“Design a product-catalogue cache”.\n\nQuestion 1?"
    )
    opening = sync.store.for_item(KEY)[0].turns[0]
    assert message.endswith(
        f"Answer it in your feed: {FEED}/i/prep%3Asd-cache#turn-{opening.id}"
    )
    await _answer(sync, clock, started.id, "Answer 1")  # quick: no ping
    assert len(telegram.sent) == 1


async def test_a_failed_telegram_message_is_sent_again_later(sync, clock, telegram):
    started = await _start(sync, clock)
    telegram.fail = True
    await _answer(sync, clock, started.id, "Answer 1", end=True)
    assert telegram.sent == []
    telegram.fail = False
    await sync.run_once()  # too soon
    assert telegram.sent == []
    clock.advance(NOTIFY_RETRY)
    await sync.run_once()
    assert len(telegram.sent) == 1 and "is graded" in telegram.sent[0]
    clock.advance(NOTIFY_RETRY * 5)
    await sync.run_once()
    assert len(telegram.sent) == 1


# --- failures ----------------------------------------------------------------- #
async def test_a_failed_opening_is_retried_then_the_interview_is_dead(
    sync, clock, interviewer, telegram
):
    sync.store.start(KEY, now=clock())
    interviewer.fail = True
    await sync.run_once()
    (interview,) = sync.store.for_item(KEY)
    turn = interview.turns[0]
    assert turn.retrying and turn.error == "claude error 529: overloaded"
    assert turn.retry_at == T0 + RETRY_BASE
    for attempt in range(1, MAX_ATTEMPTS):
        clock.advance(RETRY_BASE * 2 ** (attempt - 1))
        report = await sync.run_once()
    assert report.failed == 1
    (interview,) = sync.store.for_item(KEY)
    assert interview.dead and not interview.active and not interview.waiting
    assert interview.progress == "Couldn't start"
    assert telegram.sent == []
    interviewer.fail = False
    sync.store.start(KEY, now=clock.advance())  # a new one may start


async def test_a_failed_answer_can_be_sent_again(sync, clock, interviewer):
    started = await _start(sync, clock)
    sync.store.answer(KEY, started.id, "Lost answer", now=clock.advance())
    claimed = sync.store.ready(now=clock(), limit=1)[0]
    sync.store.claim(claimed.id, now=clock())
    sync.store.give_up(claimed.id, "claude error: refused")
    interview = sync.store.for_item(KEY)[0]
    assert interview.can_answer and interview.answered == 0
    await _answer(sync, clock, started.id, "Answer 1")
    request = interviewer.requests[-1]
    assert request.task == NEXT and request.asked == 1
    assert ("candidate", "Lost answer") not in request.transcript
    assert sync.store.for_item(KEY)[0].asked == 2


async def test_a_turn_on_a_removed_card_is_given_up(sync, clock, db, interviewer):
    sync.store.start(KEY, now=clock())
    InsightStore(db).delete([KEY])
    report = await sync.run_once()
    assert report.failed == 1 and interviewer.requests == []
    turn = sync.store.for_item(KEY)[0].turns[0]
    assert turn.failed and turn.error == "the card is no longer in the feed"


# --- context ------------------------------------------------------------------ #
async def test_a_resume_card_comes_with_its_resume_line(db, tmp_path, clock):
    async def no_explainers(request):
        raise AssertionError("not written here")

    PrepSync(db, no_explainers).import_folder(make_folder(tmp_path))
    interviewer = Interviewer()
    sync = InterviewSync(db, interviewer, clock=clock)
    key = card_key("T5", 4)
    sync.store.start(key, now=clock())
    await sync.run_once()
    (request,) = interviewer.requests
    assert request.task == OPEN and request.type == "interview"
    assert request.headline == "How did you make the SQL safe?"
    assert request.kind.startswith("an interview flashcard")
    assert request.resume_line.startswith("Led the production")
    assert request.transcript == []


async def test_other_prep_cards_are_described_as_prep(db, clock, interviewer):
    InsightStore(db).add(
        Insight(
            key="prep:dsa:top-k",
            text="Find the k most frequent items.",
            title="Top-K frequent",
            type="dsa",
            captured_at=T0,
        )
    )
    sync = InterviewSync(db, interviewer, clock=clock)
    sync.store.start("prep:dsa:top-k", now=clock())
    await sync.run_once()
    (request,) = interviewer.requests
    assert request.type == "dsa" and request.resume_line == ""
    assert request.kind == "an interview-prep card from the reader's DSA track"


async def test_the_ask_loop_runs_interview_turns_too(db, clock, interviewer):
    async def answer(request):
        return "An answer."

    interviews = InterviewSync(db, interviewer, clock=clock)
    questions = QuestionSync(db, answer, clock=clock, interviews=interviews)
    questions.store.ask(KEY, "Why a read-through cache?", now=clock())
    interviews.store.start(KEY, now=clock())
    report = await questions.run_once()
    assert (report.written, report.remaining) == (2, 0)
    assert questions.store.thread(KEY)[0].answered
    assert interviews.store.for_item(KEY)[0].can_answer
