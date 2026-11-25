"""Mock interviews on interview-prep cards, run by Claude (issue #133).

"Interview me" in the Ask box of a prep card opens a short mock interview
about it. Claude asks one question at a time, gives a line or two of feedback
on each answer, and after the last question (or when the reader taps "End &
grade") grades the whole interview against the mid-level bar.

It works like Ask (#119): the phone usually reads the feed server, which has no
``claude`` CLI, so each step only stores a *turn* in ``feed_interview_turns``.
The Mac's ``daily-agent web`` (or ``daily-agent questions answer``) claims it
through ``StoryQueue``, has Claude write the interviewer's reply with the
transcript so far, and stores it. One turn is open at a time per interview.
"Start over" drops a turn still queued for the Mac (it would be wasted), but
waits while Claude is writing one or grading.

Turns of one interview share its id and are numbered: the opening (``n`` 0)
has no answer from the reader, and every later turn carries the reader's
answer plus the interviewer's feedback and next question, or the verdict.
Telegram stays quiet while the reader is there: it pings only for the verdict,
or when a reply took more than ``SLOW_TURN`` (they probably left).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import UniqueConstraint, update
from sqlmodel import Field, SQLModel, func, select

from ..agents.mock_interviewer import (
    GRADES,
    NEXT,
    OPEN as OPEN_TASK,
    VERDICT,
    InterviewReply,
    InterviewRequest,
)
from ..db import conflict_insert, create_tables, make_engine, session_scope
from .headlines import plain
from .insights_store import InsightStore
from .prep import PrepStore
from .prep_tracks import (
    CARD_TYPE,
    CONCEPT_TYPE,
    KEY_PREFIX as PREP_PREFIX,
    TRACKS,
    track_of,
)
from .questions import clip, describe
from .story_queue import (
    DONE,
    FAILED,
    PENDING,
    SKIPPED,
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

# Turn kinds: the opening question, an answer (feedback + next question), and
# an answer that ends the interview (the verdict).
OPEN, REPLY, GRADE = "open", "reply", "grade"
INTERVIEWER, CANDIDATE = "interviewer", "candidate"

DEFAULT_QUESTIONS = 4
MAX_QUESTIONS = 10
MAX_ANSWER_CHARS = 2000
MAX_OPEN = 20  # turns waiting for Claude across the feed
MAX_PER_ITEM = 20  # interviews on one card
MAX_TURNS = 2 * MAX_QUESTIONS + 4  # per interview, with room for failed turns
PER_RUN = 3
# A reply that took longer than this pings Telegram: the reader probably left.
SLOW_TURN = timedelta(minutes=2)
NOTIFY_ATTEMPTS = 3
NOTIFY_RETRY = timedelta(minutes=5)
NOTIFY_WINDOW = timedelta(days=1)
PREVIEW_CHARS = 700


class InterviewError(ValueError):
    """A step that can't be taken; the message is shown to the reader."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status  # HTTP status for the web form


class InterviewTurnRow(SQLModel, table=True):
    __tablename__ = "feed_interview_turns"
    # Two taps on "send" can't both become the next turn.
    __table_args__ = (
        UniqueConstraint("interview", "n", name="uq_feed_interview_turns_n"),
    )

    id: str = Field(primary_key=True)  # "t3f9a1c20b7"
    interview: str = Field(index=True)  # "m81c0d5e3aa": groups one interview
    key: str = Field(index=True)  # the card it's about
    n: int  # 0 = the opening; one more per turn
    kind: str  # OPEN, REPLY or GRADE
    said: str = ""  # the reader's answer ("" for the opening)
    feedback: str = ""  # the interviewer's feedback on it
    reply: str = ""  # the next question, or the verdict
    grade: str = ""  # the verdict's suggested self-grade
    questions: int = DEFAULT_QUESTIONS  # asked before the verdict
    published_at: str  # when the reader sent it; orders the queue
    answered_at: str | None = None
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""
    ping: bool = False  # owes a Telegram message
    notified_at: str | None = None
    notify_attempts: int = 0
    notify_after: str | None = None
    notify_error: str = ""


@dataclass(frozen=True)
class Turn:
    id: str
    interview: str
    key: str
    n: int
    kind: str
    said: str
    feedback: str
    reply: str
    grade: str
    state: str
    asked_at: datetime
    answered_at: datetime | None
    attempts: int
    error: str
    retry_at: datetime | None

    @property
    def done(self) -> bool:
        return self.state == DONE

    @property
    def failed(self) -> bool:
        return self.state == FAILED

    @property
    def writing(self) -> bool:
        return self.state == WRITING

    @property
    def retrying(self) -> bool:
        return self.state == PENDING and self.attempts > 0

    @property
    def skipped(self) -> bool:
        """Dropped unanswered when the reader started over."""
        return self.state == SKIPPED

    @property
    def is_open(self) -> bool:
        return self.state in (PENDING, WRITING)

    @property
    def grade_label(self) -> str:
        return GRADES.get(self.grade, "")


def _turn(row: InterviewTurnRow) -> Turn:
    return Turn(
        id=row.id,
        interview=row.interview,
        key=row.key,
        n=row.n,
        kind=row.kind,
        said=row.said,
        feedback=row.feedback,
        reply=row.reply,
        grade=row.grade,
        state=row.state,
        asked_at=aware(row.published_at),
        answered_at=aware(row.answered_at) if row.answered_at else None,
        attempts=row.attempts,
        error=row.error,
        retry_at=aware(row.not_before) if row.not_before else None,
    )


@dataclass(frozen=True)
class Interview:
    """One mock interview: its turns in order. Only the newest on a card
    (``latest``) can go on; an older one that was never graded was stopped."""

    id: str
    key: str
    questions: int
    turns: tuple[Turn, ...]
    latest: bool = True

    @property
    def started_at(self) -> datetime:
        return self.turns[0].asked_at

    @property
    def asked(self) -> int:
        """Questions the interviewer has asked."""
        return sum(1 for t in self.turns if t.done and t.kind != GRADE)

    @property
    def answered(self) -> int:
        """Answers the reader has given (not counting failed or dropped ones)."""
        return sum(1 for t in self.turns if t.said and t.state not in (FAILED, SKIPPED))

    @property
    def verdict(self) -> Turn | None:
        return next((t for t in self.turns if t.done and t.kind == GRADE), None)

    @property
    def graded(self) -> bool:
        return self.verdict is not None

    @property
    def waiting(self) -> bool:
        """A turn is with Claude (or waiting for the Mac)."""
        return self.turns[-1].is_open

    @property
    def grading(self) -> bool:
        return self.waiting and self.turns[-1].kind == GRADE

    @property
    def dead(self) -> bool:
        """The opening question never came."""
        return self.asked == 0 and self.turns[-1].failed

    @property
    def active(self) -> bool:
        """The reader is in it: answering, or waiting for the next question."""
        return self.latest and not (self.graded or self.dead or self.grading)

    @property
    def can_answer(self) -> bool:
        return self.active and not self.waiting

    @property
    def number(self) -> int:
        """The question the reader is on (1-based)."""
        coming = self.waiting and self.turns[-1].kind != GRADE
        return min(max(self.asked + coming, 1), self.questions)

    @property
    def progress(self) -> str:
        """The header's turn count."""
        if self.graded:
            return f"Graded · {self.asked} question{'s' if self.asked != 1 else ''}"
        if self.grading:
            return "Grading…"
        if self.dead:
            return "Couldn't start"
        if not self.latest:
            return f"Stopped after {self.answered} of {self.questions}"
        return f"Question {self.number} of {self.questions}"


def _interviews(rows: list[InterviewTurnRow]) -> list[Interview]:
    """Rows grouped into interviews, oldest first (by when each started)."""
    grouped: dict[str, list[InterviewTurnRow]] = {}
    for row in rows:
        grouped.setdefault(row.interview, []).append(row)
    interviews = sorted(
        (
            Interview(
                id=interview_id,
                key=group[0].key,
                questions=group[0].questions,
                turns=tuple(_turn(r) for r in sorted(group, key=lambda r: r.n)),
            )
            for interview_id, group in grouped.items()
        ),
        key=lambda iv: (iv.started_at, iv.id),
    )
    return [replace(iv, latest=False) for iv in interviews[:-1]] + interviews[-1:]


def clean_answer(text: str, *, required: bool = True) -> str:
    """The answer as typed, trimmed; raises ``InterviewError`` if unusable."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    answer = "\n".join(lines).strip()
    while "\n\n\n" in answer:
        answer = answer.replace("\n\n\n", "\n\n")
    if required and not answer:
        raise InterviewError("Type your answer first.")
    if len(answer) > MAX_ANSWER_CHARS:
        raise InterviewError(
            f"That's too long: keep an answer under {MAX_ANSWER_CHARS} characters."
        )
    return answer


def clamp_questions(value: int) -> int:
    return min(max(int(value), 1), MAX_QUESTIONS)


class InterviewStore(StoryQueue):
    """The ``feed_interview_turns`` table: every turn of every mock interview,
    and the queue the Mac works through."""

    item_row = InterviewTurnRow
    item_id = "id"
    newest_first = False  # in the order sent

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, InterviewTurnRow)

    # --- reading ------------------------------------------------------------ #
    def for_item(self, key: str) -> list[Interview]:
        """Every mock interview on ``key``, oldest first."""
        row = InterviewTurnRow
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(row)
                .where(row.key == key)
                .order_by(row.published_at, row.n, row.id)
            ).all()
        return _interviews(list(rows))

    def interview(self, interview_id: str) -> Interview | None:
        """One interview (``latest`` is not worked out here)."""
        row = InterviewTurnRow
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(row).where(row.interview == interview_id).order_by(row.n)
            ).all()
        found = _interviews(list(rows))
        return found[0] if found else None

    def turn(self, turn_id: str) -> Turn | None:
        with session_scope(self._engine) as session:
            row = session.get(InterviewTurnRow, turn_id)
            return _turn(row) if row else None

    def _open_turns(self, session, *conditions) -> int:
        row = InterviewTurnRow
        stmt = (
            select(func.count())
            .select_from(row)
            .where(row.state.in_((PENDING, WRITING)), *conditions)
        )
        return session.scalar(stmt) or 0

    # --- the reader's steps ------------------------------------------------- #
    def start(
        self, key: str, *, questions: int = DEFAULT_QUESTIONS, now: datetime
    ) -> Interview:
        """A new interview on ``key`` (an older one there stops). A turn of the
        old one still queued for the Mac is dropped; one Claude is writing, or
        a verdict on its way, makes this wait."""
        row = InterviewTurnRow
        interview_id = "m" + secrets.token_hex(5)
        with session_scope(self._engine) as session:
            # Only a still-queued turn: a claim that won the race stays, and
            # is caught just below. Undone if this start is refused.
            session.execute(
                update(row)
                .where(row.key == key, row.state == PENDING, row.kind != GRADE)
                .values(state=SKIPPED, not_before=None)
            )
            if self._open_turns(session, row.key == key):
                raise InterviewError(
                    "The interviewer is still on your last turn. Wait for the "
                    "reply, then start over.",
                    status=409,
                )
            waiting = self._open_turns(session)
            if waiting >= MAX_OPEN:
                raise InterviewError(
                    f"{waiting} turns are still waiting for Claude. Try again "
                    "once some are answered.",
                    status=429,
                )
            started = session.scalar(
                select(func.count()).select_from(row).where(row.key == key, row.n == 0)
            )
            if (started or 0) >= MAX_PER_ITEM:
                raise InterviewError(
                    "This card has had enough mock interviews.", status=429
                )
            session.add(
                row(
                    id="t" + secrets.token_hex(5),
                    interview=interview_id,
                    key=key,
                    n=0,
                    kind=OPEN,
                    questions=clamp_questions(questions),
                    published_at=iso(now),
                )
            )
        return self.for_item(key)[-1]

    def answer(
        self,
        key: str,
        interview_id: str,
        text: str,
        *,
        end: bool = False,
        now: datetime,
    ) -> Interview:
        """The reader's answer to the current question. It ends the interview
        (Claude grades it) after the last question, or with ``end``."""
        interviews = self.for_item(key)
        current = next((iv for iv in interviews if iv.id == interview_id), None)
        if current is None:
            raise InterviewError("That interview isn't on this card.", status=409)
        if not current.latest:
            raise InterviewError("That interview was stopped.", status=409)
        if current.graded:
            raise InterviewError("That interview is already graded.", status=409)
        if current.dead:
            raise InterviewError(
                "That interview never started. Start a new one.", status=409
            )
        if current.waiting:
            raise InterviewError(
                "The interviewer is still on your last answer.", status=409
            )
        answer = clean_answer(text, required=not end)
        if end and not answer and not current.answered:
            raise InterviewError("Answer at least one question first, or start over.")
        if len(current.turns) >= MAX_TURNS:
            raise InterviewError("This interview has run long enough.", status=429)
        kind = GRADE if end or current.asked >= current.questions else REPLY
        row = InterviewTurnRow
        with session_scope(self._engine) as session:
            waiting = self._open_turns(session)
            if waiting >= MAX_OPEN:
                raise InterviewError(
                    f"{waiting} turns are still waiting for Claude. Try again "
                    "once some are answered.",
                    status=429,
                )
            added = session.execute(
                conflict_insert(session, row)
                .values(
                    id="t" + secrets.token_hex(5),
                    interview=interview_id,
                    key=key,
                    n=current.turns[-1].n + 1,
                    kind=kind,
                    said=answer,
                    questions=current.questions,
                    published_at=iso(now),
                    state=PENDING,
                    attempts=0,
                    error="",
                    ping=False,
                    notify_attempts=0,
                    notify_error="",
                    feedback="",
                    reply="",
                    grade="",
                )
                .on_conflict_do_nothing(index_elements=["interview", "n"])
                .returning(row.id)
            ).first()
        if added is None:
            raise InterviewError("That answer was already sent.", status=409)
        return self.for_item(key)[-1]

    # --- the Mac's side ----------------------------------------------------- #
    def record_reply(
        self, turn_id: str, reply: InterviewReply, *, now: datetime, ping: bool
    ) -> None:
        self._set(
            turn_id,
            state=DONE,
            reply=reply.question,
            feedback=reply.feedback,
            grade=reply.grade,
            answered_at=iso(now),
            ping=ping,
            error="",
            not_before=None,
            claimed_at=None,
        )

    def give_up(self, turn_id: str, error: str) -> None:
        self._set(
            turn_id,
            state=FAILED,
            error=error[:300],
            not_before=None,
            claimed_at=None,
        )

    # --- Telegram ----------------------------------------------------------- #
    def to_notify(self, *, now: datetime, limit: int = 5) -> list[Turn]:
        """Replies that owe a Telegram message (and may send it now)."""
        row = InterviewTurnRow
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(row)
                .where(row.state == DONE)
                .where(row.ping.is_(True))
                .where(row.notified_at.is_(None))
                .where(row.notify_attempts < NOTIFY_ATTEMPTS)
                .where(row.answered_at >= iso(now - NOTIFY_WINDOW))
                .where(row.notify_after.is_(None) | (row.notify_after <= iso(now)))
                .order_by(row.answered_at, row.id)
                .limit(limit)
            ).all()
        return [_turn(r) for r in rows]

    def mark_notified(self, turn_id: str, *, now: datetime) -> None:
        self._set(turn_id, notified_at=iso(now), notify_error="")

    def notify_failed(self, turn_id: str, error: str, *, now: datetime) -> None:
        with session_scope(self._engine) as session:
            row = session.get(InterviewTurnRow, turn_id)
            if row is None:
                return
            row.notify_attempts += 1
            row.notify_after = iso(now + NOTIFY_RETRY * row.notify_attempts)
            row.notify_error = error[:300]
            session.add(row)


def card_kind(entry: FeedEntry) -> str:
    """What the card is, for Claude."""
    if entry.type in (CARD_TYPE, CONCEPT_TYPE):
        return describe(entry)
    track = TRACKS.get(track_of(entry.type) or "", "")
    return f"an interview-prep card from the reader's {track or 'practice'} track"


def turn_link(feed_url: str, turn: Turn) -> str:
    """The feed URL that opens the card at this turn ("" if unconfigured)."""
    if not feed_url:
        return ""
    return f"{feed_url.rstrip('/')}/i/{quote(turn.key, safe='')}#turn-{turn.id}"


def telegram_text(turn: Turn, interview: Interview, headline: str, link: str) -> str:
    """The Telegram message for a verdict or a slow reply (plain text)."""
    about = f"“{headline}”" if headline else "a card in your feed"
    if turn.kind == GRADE:
        summary = plain(turn.reply.split("\n\n", 1)[0])
        parts = [
            f"Your mock interview on {about} is graded. Suggested self-grade: "
            f"{turn.grade_label or 'see the verdict'}.",
            clip(summary, PREVIEW_CHARS),
            f"Read the verdict: {link}"
            if link
            else "The verdict is in your feed under the card.",
        ]
    else:
        number = sum(
            1 for t in interview.turns if t.done and t.kind != GRADE and t.n <= turn.n
        )
        parts = [
            f"The interviewer asked question {number} of {interview.questions} "
            f"in your mock interview on {about}.",
            clip(plain(turn.reply), PREVIEW_CHARS),
            f"Answer it in your feed: {link}"
            if link
            else "Answer it in your feed under the card.",
        ]
    return "\n\n".join(parts)


Interviewer = Callable[[InterviewRequest], Awaitable[InterviewReply]]
Notifier = Callable[[str], None]  # sends one message; raises if it can't


class InterviewSync:
    """Write the interviewer's replies with Claude; send the few messages owed."""

    def __init__(
        self,
        db_path: str | Path,
        interviewer: Interviewer,
        *,
        notify: Notifier | None = None,
        feed_url: str = "",
        questions: int = DEFAULT_QUESTIONS,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = InterviewStore(db_path)
        self.feed = WebFeed(db_path)
        self.insights = InsightStore(db_path)
        self.prep = PrepStore(db_path)
        self.interviewer = interviewer
        self.notify = notify
        self.feed_url = feed_url
        self.questions = clamp_questions(questions)
        self.per_run = max(1, per_run)
        self.clock = clock

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Send messages still owed, then work through up to ``limit`` turns."""
        report = SyncReport()
        if self.notify is not None:
            for turn in self.store.to_notify(now=self.clock()):
                await self._send(turn)
        for row in self.store.ready(now=self.clock(), limit=limit or self.per_run):
            await self._work(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    def request_for(self, turn: Turn) -> InterviewRequest | None:
        """What Claude is given; ``None`` if the card (or interview) is gone."""
        entry = self.feed.entry(turn.key)
        interview = self.store.interview(turn.interview)
        if entry is None or interview is None:
            return None
        earlier = [t for t in interview.turns if t.done and t.n < turn.n]
        transcript: list[tuple[str, str]] = []
        for t in earlier:
            if t.said:
                transcript.append((CANDIDATE, t.said))
            said = "\n\n".join(part for part in (t.feedback, t.reply) if part)
            transcript.append((INTERVIEWER, said))
        if turn.said:
            transcript.append((CANDIDATE, turn.said))
        task = {OPEN: OPEN_TASK, GRADE: VERDICT}.get(turn.kind, NEXT)
        answers = sum(1 for who, _ in transcript if who == CANDIDATE)
        return InterviewRequest(
            task=task,
            type=entry.type,
            kind=card_kind(entry),
            headline=entry.headline,
            text=entry.text,
            source=entry.source,
            tags=list(entry.tags),
            resume_line=self._resume_line(entry),
            questions=interview.questions,
            asked=sum(1 for t in earlier if t.kind != GRADE),
            transcript=transcript,
            ended_early=task == VERDICT and answers < interview.questions,
        )

    def _resume_line(self, entry: FeedEntry) -> str:
        """The resume bullet a deep-dive card is about ("" for other cards)."""
        insight = self.insights.get(entry.key)
        source = insight.source_session if insight else ""
        if not source.startswith(PREP_PREFIX):
            return ""
        point = self.prep.point(source.removeprefix(PREP_PREFIX))
        return point.resume_line if point is not None else ""

    async def _work(self, row: InterviewTurnRow, report: SyncReport) -> None:
        if not self.store.claim(row.id, now=self.clock()):
            return
        turn = _turn(row)
        request = self.request_for(turn)
        if request is None:
            self.store.give_up(row.id, "the card is no longer in the feed")
            report.failed += 1
            return
        try:
            reply = await self.interviewer(request)
        except Exception as exc:  # noqa: BLE001 - one turn never stops the rest
            error = first_line(exc)
            if self.store.fail_attempt(row.id, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"Mock interview turn {row.id}: {error}")
            _LOG.warning("Mock interview turn %s: %s", row.id, error)
            return
        now = self.clock()
        ping = turn.kind == GRADE or now - turn.asked_at > SLOW_TURN
        self.store.record_reply(row.id, reply, now=now, ping=ping)
        report.written += 1
        if ping and self.notify is not None:
            done = self.store.turn(row.id)
            if done is not None:
                await self._send(done)

    async def _send(self, turn: Turn) -> None:
        entry = self.feed.entry(turn.key)
        interview = self.store.interview(turn.interview)
        if interview is None:
            return
        text = telegram_text(
            turn,
            interview,
            entry.headline if entry else "",
            turn_link(self.feed_url, turn),
        )
        try:
            await asyncio.to_thread(self.notify, text)
        except Exception as exc:  # noqa: BLE001 - the reply is in the feed anyway
            self.store.notify_failed(turn.id, first_line(exc), now=self.clock())
            _LOG.warning("Telegram message for interview turn %s: %s", turn.id, exc)
            return
        self.store.mark_notified(turn.id, now=self.clock())
