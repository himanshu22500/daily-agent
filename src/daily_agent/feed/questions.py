"""Questions about feed items, answered by Claude (issue #119).

The reader asks a question under any article or interview card. The phone
usually reads the always-on feed server, which has no ``claude`` CLI (#111,
#114), so asking only stores the question in ``feed_questions``. The Mac's
``daily-agent web`` (or ``daily-agent questions answer``) picks it up through
the shared ``StoryQueue`` (claim, retry with backoff, give up), has Claude
answer it, stores the answer, and sends a Telegram message with a link that
opens the item in the feed at the answer. While the Mac sleeps, questions
wait.

Questions are answered in the order they were asked, so a follow-up on the
same item is answered with the earlier answer in view. A Telegram message that
fails to send is tried again a few times; the answer is in the feed either way.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from sqlmodel import Field, SQLModel, func, select

from ..agents.question_answerer import AskRequest
from ..db import create_tables, make_engine, session_scope
from .headlines import plain
from .insights_store import InsightStore
from .prep import CARD_TYPE, CONCEPT_TYPE, KEY_PREFIX as PREP_PREFIX, PrepStore
from .story_queue import (
    DONE,
    FAILED,
    PENDING,
    WRITING,
    StoryQueue,
    SyncReport,
    aware,
    first_line,
    iso,
    now_utc,
)
from .web_feed import FeedEntry, WebFeed

_LOG = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_OPEN = 20  # unanswered questions across the feed
MAX_PER_ITEM = 50
PER_RUN = 3
NOTIFY_ATTEMPTS = 3
NOTIFY_RETRY = timedelta(minutes=5)
# A message that couldn't go out within a day isn't news any more.
NOTIFY_WINDOW = timedelta(days=1)
PREVIEW_CHARS = 1200


class QuestionError(ValueError):
    """A question that can't be asked; the message is shown to the reader."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status  # HTTP status for the web form


class QuestionRow(SQLModel, table=True):
    __tablename__ = "feed_questions"

    id: str = Field(primary_key=True)  # "q3f9a1c20b7"
    key: str = Field(index=True)  # the insight it's about
    question: str
    answer: str = ""
    published_at: str  # when it was asked; orders the queue
    answered_at: str | None = None
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""
    notified_at: str | None = None
    notify_attempts: int = 0
    notify_after: str | None = None
    notify_error: str = ""


@dataclass(frozen=True)
class Question:
    id: str
    key: str
    question: str
    answer: str
    state: str
    asked_at: datetime
    answered_at: datetime | None
    attempts: int
    error: str
    retry_at: datetime | None

    @property
    def answered(self) -> bool:
        return self.state == DONE

    @property
    def failed(self) -> bool:
        return self.state == FAILED

    @property
    def writing(self) -> bool:
        return self.state == WRITING

    @property
    def retrying(self) -> bool:
        """Waiting to be tried again after a failed attempt."""
        return self.state == PENDING and self.attempts > 0

    @property
    def is_open(self) -> bool:
        return self.state in (PENDING, WRITING)


def _question(row: QuestionRow) -> Question:
    return Question(
        id=row.id,
        key=row.key,
        question=row.question,
        answer=row.answer,
        state=row.state,
        asked_at=aware(row.published_at),
        answered_at=aware(row.answered_at) if row.answered_at else None,
        attempts=row.attempts,
        error=row.error,
        retry_at=aware(row.not_before) if row.not_before else None,
    )


def clean_question(text: str) -> str:
    """The question as typed, trimmed; raises ``QuestionError`` if unusable."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    question = "\n".join(lines).strip()
    while "\n\n\n" in question:
        question = question.replace("\n\n\n", "\n\n")
    if not question:
        raise QuestionError("Type a question first.")
    if len(question) > MAX_QUESTION_CHARS:
        raise QuestionError(
            f"That's too long: keep a question under {MAX_QUESTION_CHARS} characters."
        )
    return question


class QuestionStore(StoryQueue):
    """The ``feed_questions`` table: each question, its answer, and the queue
    the Mac answers from."""

    item_row = QuestionRow
    item_id = "id"
    newest_first = False

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, QuestionRow)

    def ask(self, key: str, text: str, *, now: datetime) -> Question:
        question = clean_question(text)
        row = QuestionRow(
            id="q" + secrets.token_hex(5),
            key=key,
            question=question,
            published_at=iso(now),
        )
        with session_scope(self._engine) as session:
            waiting = session.scalar(
                select(func.count())
                .select_from(QuestionRow)
                .where(QuestionRow.state.in_((PENDING, WRITING)))
            )
            if (waiting or 0) >= MAX_OPEN:
                raise QuestionError(
                    f"{waiting} questions are still waiting for Claude. Ask this "
                    "one once some are answered.",
                    status=429,
                )
            on_item = session.scalar(
                select(func.count())
                .select_from(QuestionRow)
                .where(QuestionRow.key == key)
            )
            if (on_item or 0) >= MAX_PER_ITEM:
                raise QuestionError("This item has had enough questions.", status=429)
            session.add(row)
        return _question(row)

    def get(self, question_id: str) -> Question | None:
        with session_scope(self._engine) as session:
            row = session.get(QuestionRow, question_id)
            return _question(row) if row else None

    def thread(self, key: str) -> list[Question]:
        """Every question about ``key``, in the order asked."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(QuestionRow)
                .where(QuestionRow.key == key)
                .order_by(QuestionRow.published_at, QuestionRow.id)
            ).all()
        return [_question(row) for row in rows]

    def recent(self, limit: int = 20) -> list[Question]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(QuestionRow)
                .order_by(QuestionRow.published_at.desc(), QuestionRow.id)
                .limit(limit)
            ).all()
        return [_question(row) for row in rows]

    def record_answer(self, question_id: str, answer: str, *, now: datetime) -> None:
        self._set(
            question_id,
            state=DONE,
            answer=answer,
            answered_at=iso(now),
            error="",
            not_before=None,
            claimed_at=None,
        )

    def give_up(self, question_id: str, error: str) -> None:
        self._set(
            question_id,
            state=FAILED,
            error=error[:300],
            not_before=None,
            claimed_at=None,
        )

    # --- Telegram ------------------------------------------------------------ #
    def to_notify(self, *, now: datetime, limit: int = 5) -> list[Question]:
        """Answered questions whose message hasn't gone out (and may now)."""
        row = QuestionRow
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(row)
                .where(row.state == DONE)
                .where(row.notified_at.is_(None))
                .where(row.notify_attempts < NOTIFY_ATTEMPTS)
                .where(row.answered_at >= iso(now - NOTIFY_WINDOW))
                .where(row.notify_after.is_(None) | (row.notify_after <= iso(now)))
                .order_by(row.answered_at, row.id)
                .limit(limit)
            ).all()
        return [_question(r) for r in rows]

    def mark_notified(self, question_id: str, *, now: datetime) -> None:
        self._set(question_id, notified_at=iso(now), notify_error="")

    def notify_failed(self, question_id: str, error: str, *, now: datetime) -> None:
        with session_scope(self._engine) as session:
            row = session.get(QuestionRow, question_id)
            if row is None:
                return
            row.notify_attempts += 1
            row.notify_after = iso(now + NOTIFY_RETRY * row.notify_attempts)
            row.notify_error = error[:300]
            session.add(row)


def describe(entry: FeedEntry) -> str:
    """What the item is, for Claude."""
    if entry.video_id:
        return "a story Claude wrote from a YouTube video the reader follows"
    if entry.post_id:
        return "a story Claude wrote from an Instagram post the reader follows"
    if entry.type == CARD_TYPE:
        return (
            "an interview flashcard: the headline is a question the reader may "
            "be asked about their resume, and the text is their prepared answer"
        )
    if entry.type == CONCEPT_TYPE:
        return "a concept explainer Claude wrote for the reader's interview prep"
    return "an insight the reader learned during a coding session"


_BLANK_RUNS = re.compile(r"\n{3,}")


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def telegram_text(question: Question, headline: str, link: str) -> str:
    """The Telegram message for an answered question (plain text)."""
    # Markdown markers dropped; paragraphs and "• " lines kept.
    lines = [plain(line) for line in question.answer.splitlines()]
    answer = clip(_BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip(), PREVIEW_CHARS)
    parts = [
        f"Claude answered your question about “{headline}”"
        if headline
        else "Claude answered your question about an item in your feed.",
        f"Q: {clip(question.question, 300)}",
        answer,
        f"Read it in your feed: {link}"
        if link
        else "It's in your feed under the item.",
    ]
    return "\n\n".join(parts)


def item_link(feed_url: str, question: Question) -> str:
    """The feed URL that opens the item at this answer ("" if unconfigured)."""
    if not feed_url:
        return ""
    return f"{feed_url.rstrip('/')}/i/{quote(question.key, safe='')}#ask-{question.id}"


Answerer = Callable[[AskRequest], Awaitable[str]]
Notifier = Callable[[str], None]  # sends one message; raises if it can't


class QuestionSync:
    """Answer queued questions with Claude and send the Telegram messages."""

    def __init__(
        self,
        db_path: str | Path,
        answer: Answerer,
        *,
        notify: Notifier | None = None,
        feed_url: str = "",
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = QuestionStore(db_path)
        self.feed = WebFeed(db_path)
        self.insights = InsightStore(db_path)
        self.prep = PrepStore(db_path)
        self.answer = answer
        self.notify = notify
        self.feed_url = feed_url
        self.per_run = max(1, per_run)
        self.clock = clock

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Send messages still owed, then answer up to ``limit`` questions."""
        report = SyncReport()
        if self.notify is not None:
            for question in self.store.to_notify(now=self.clock()):
                await self._send(question)
        for row in self.store.ready(now=self.clock(), limit=limit or self.per_run):
            await self._answer(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    def request_for(self, question: Question) -> AskRequest | None:
        """What Claude is given; ``None`` if the item is gone."""
        entry = self.feed.entry(question.key)
        if entry is None:
            return None
        earlier = [
            (q.question, q.answer)
            for q in self.store.thread(question.key)
            if q.answered and (q.asked_at, q.id) < (question.asked_at, question.id)
        ]
        resume_line = notes = ""
        if entry.type in (CARD_TYPE, CONCEPT_TYPE):
            insight = self.insights.get(question.key)
            source = insight.source_session if insight else ""
            point = (
                self.prep.point(source.removeprefix(PREP_PREFIX))
                if source.startswith(PREP_PREFIX)
                else None
            )
            if point is not None:
                resume_line, notes = point.resume_line, point.notes
        return AskRequest(
            question=question.question,
            kind=describe(entry),
            headline=entry.headline,
            text=entry.text,
            source=entry.source,
            tags=list(entry.tags),
            resume_line=resume_line,
            notes=notes,
            earlier=earlier,
        )

    async def _answer(self, row: QuestionRow, report: SyncReport) -> None:
        if not self.store.claim(row.id, now=self.clock()):
            return
        question = _question(row)
        request = self.request_for(question)
        if request is None:
            self.store.give_up(row.id, "the item is no longer in the feed")
            report.failed += 1
            return
        try:
            answer = await self.answer(request)
        except Exception as exc:  # noqa: BLE001 - one question never stops the rest
            error = first_line(exc)
            if self.store.fail_attempt(row.id, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"{question.question[:60]}: {error}")
            _LOG.warning("Question %s: %s", row.id, error)
            return
        self.store.record_answer(row.id, answer, now=self.clock())
        report.written += 1
        if self.notify is not None:
            answered = self.store.get(row.id)
            if answered is not None:
                await self._send(answered)

    async def _send(self, question: Question) -> None:
        entry = self.feed.entry(question.key)
        text = telegram_text(
            question,
            entry.headline if entry else "",
            item_link(self.feed_url, question),
        )
        try:
            await asyncio.to_thread(self.notify, text)
        except Exception as exc:  # noqa: BLE001 - the answer is in the feed anyway
            self.store.notify_failed(question.id, first_line(exc), now=self.clock())
            _LOG.warning("Telegram message for question %s: %s", question.id, exc)
            return
        self.store.mark_notified(question.id, now=self.clock())
