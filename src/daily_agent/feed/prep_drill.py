"""A Telegram drill every morning, and its answers every evening (#136).

At ``prep_drill_hour`` (in ``DAILY_AGENT_TIMEZONE``) the bot sends one DSA card
and one system design card, prompt only, each with a link to the card in the
feed. At ``prep_answer_hour`` it sends the answers to the same two cards: a DSA
card's Pattern and Key idea steps, a design card's 30-second recap (or its
first step).

**Picking.** Cards due for review or never seen come first, then cards on the
reader's weak tags, then ones not drilled this week, then must-do cards, then
DSA problems before pattern quizzes. A card drilled yesterday is never picked,
and a hidden one never is. Ties rotate by day.

**Once per slot.** Both the Mac and the feed server may run ``daily-agent
web``; whichever has Telegram sends. Each ``(day, slot)`` is claimed in
``prep_drills`` with an insert only one runner wins (``RETURNING`` says which).
A failed send is retried a few times, ten minutes apart; a runner that dies
mid-send is not retried, so a message never goes out twice.

**Catch-up.** A machine that slept through the morning hour sends the drill
when it wakes, until the evening hour. The answers go out only after a drill
did. Quiet hours hold both.

No model calls: it only picks and formats cards already in the feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import update
from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..sources.practice_pack import MUST_DO
from .headlines import fallback_headline, plain
from .insights_store import InsightRow
from .pacer import Pacer
from .prep_grades import GradeStore
from .prep_progress import day_label, weak_spots
from .prep_tracks import (
    DESIGN,
    DSA,
    PROBLEM_TYPE,
    TRACKS,
    PrepItems,
    Step,
    split_steps,
    track_of,
    types_for,
)
from .story_queue import first_line, iso, now_utc
from .web_feed import WebFeedStateRow

_LOG = logging.getLogger(__name__)

MORNING, EVENING = "morning", "evening"
PROMPT_CHARS = 600
STEP_CHARS = 400
RETRY_GAP = timedelta(minutes=10)
MAX_ATTEMPTS = 3
RECENT_DAYS = 7
CHECK_EVERY = 300.0  # seconds between the loop's looks at the clock

Notifier = Callable[[str], None]  # sends one message; raises if it can't


class PrepDrillRow(SQLModel, table=True):
    """One message per local day and slot; the key is what makes it once."""

    __tablename__ = "prep_drills"

    day: str = Field(primary_key=True)  # local date, "2026-11-25"
    slot: str = Field(primary_key=True)  # "morning" or "evening"
    keys: str = ""  # the cards, comma-joined (DSA first)
    claimed_at: str
    sent_at: str | None = None
    attempts: int = 0
    retry_at: str | None = None  # set after a failed send; cleared on retry
    error: str = ""


class DrillLog:
    """The ``prep_drills`` table."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, PrepDrillRow)

    def get(self, day: date, slot: str) -> PrepDrillRow | None:
        with session_scope(self._engine) as session:
            return session.get(PrepDrillRow, (day.isoformat(), slot))

    def claim(self, day: date, slot: str, keys: list[str], *, now: datetime) -> bool:
        """Take the slot; False if another runner already has it."""
        with session_scope(self._engine) as session:
            won = session.execute(
                conflict_insert(session, PrepDrillRow)
                .values(
                    day=day.isoformat(),
                    slot=slot,
                    keys=",".join(keys),
                    claimed_at=iso(now),
                    attempts=0,
                    error="",
                )
                .on_conflict_do_nothing(index_elements=["day", "slot"])
                .returning(PrepDrillRow.day)
            ).first()
        return won is not None

    def retry(self, day: date, slot: str, *, now: datetime) -> list[str] | None:
        """Take back a slot whose send failed, once it may be tried again;
        the cards it was for (``None`` if it isn't this runner's to retry)."""
        row = PrepDrillRow
        stamp = iso(now)
        with session_scope(self._engine) as session:
            taken = session.execute(
                update(row)
                .where(row.day == day.isoformat())
                .where(row.slot == slot)
                .where(row.sent_at.is_(None))
                .where(row.retry_at.is_not(None))
                .where(row.retry_at <= stamp)
                .values(retry_at=None, claimed_at=stamp)
                .returning(row.keys)
            ).first()
        return taken[0].split(",") if taken and taken[0] else None

    def sent(self, day: date, slot: str, *, now: datetime) -> None:
        self._set(day, slot, sent_at=iso(now), error="")

    def failed(self, day: date, slot: str, error: str, *, now: datetime) -> None:
        """Count a failed send; retry later, or give up after a few."""
        with session_scope(self._engine) as session:
            row = session.get(PrepDrillRow, (day.isoformat(), slot))
            if row is None:
                return
            row.attempts += 1
            row.error = error[:300]
            row.retry_at = iso(now + RETRY_GAP) if row.attempts < MAX_ATTEMPTS else None
            session.add(row)

    def drilled(self, since: date, until: date) -> dict[str, str]:
        """``{card key: last local day}`` for drills sent in [since, until)."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(PrepDrillRow.day, PrepDrillRow.keys)
                .where(PrepDrillRow.slot == MORNING)
                .where(PrepDrillRow.day >= since.isoformat())
                .where(PrepDrillRow.day < until.isoformat())
                .order_by(PrepDrillRow.day)
            ).all()
        return {key: day for day, keys in rows for key in keys.split(",") if key}

    def _set(self, day: date, slot: str, **values) -> None:
        with session_scope(self._engine) as session:
            session.execute(
                update(PrepDrillRow)
                .where(PrepDrillRow.day == day.isoformat())
                .where(PrepDrillRow.slot == slot)
                .values(**values)
            )


@dataclass(frozen=True)
class DrillCard:
    key: str
    type: str
    title: str
    text: str
    tags: tuple[str, ...] = ()
    source: str = ""  # its source's label, e.g. "Blinkit DSA pack"
    seen: bool = False
    due: bool = False

    @property
    def track(self) -> str:
        return track_of(self.type) or ""

    @property
    def headline(self) -> str:
        return self.title or fallback_headline(self.text)


# --- formatting ------------------------------------------------------------ #

_BLANK_RUNS = re.compile(r"\n{3,}")
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def flatten(text: str, limit: int) -> str:
    """Card markdown as Telegram plain text: no markers or code fences,
    one blank line at most, clipped at a word."""
    lines = [
        plain(line) for line in text.splitlines() if not line.lstrip().startswith("```")
    ]
    flat = _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def _norm(title: str) -> str:
    return _NOT_WORD.sub(" ", title.lower()).strip()


def answer_steps(card: DrillCard) -> list[Step]:
    """What the evening message shows of a card: a DSA card's Pattern and Key
    idea (else its Answer, else its first step), a design card's 30-second
    recap (else its first step)."""
    _, steps = split_steps(card.text)
    if card.track == DSA:
        picked = [
            s for s in steps if _norm(s.title).startswith(("pattern", "key idea"))
        ]
        picked = picked or [s for s in steps if _norm(s.title) == "answer"]
    else:
        picked = [s for s in steps if "recap" in _norm(s.title)][:1]
    return picked or steps[:1]


def card_link(feed_url: str, key: str) -> str:
    """The card in the feed ("" without ``DAILY_AGENT_FEED_URL``)."""
    return f"{feed_url.rstrip('/')}/i/{quote(key, safe='')}" if feed_url else ""


def hour_label(hour: int) -> str:
    """``20`` -> ``"8 pm"``."""
    return f"{hour % 12 or 12} {'am' if hour % 24 < 12 else 'pm'}"


def morning_text(
    cards: list[DrillCard], *, day: date, feed_url: str, answer_hour: int
) -> str:
    what = {1: "One card", 2: "Two cards"}.get(len(cards), f"{len(cards)} cards")
    parts = [
        f"Morning drill · {day_label(day)}",
        f"{what} to answer in your head; the answers come at "
        f"{hour_label(answer_hour)}.",
    ]
    for card in cards:
        prompt, _ = split_steps(card.text)
        lines = [
            " · ".join(filter(None, (TRACKS.get(card.track, ""), card.source))),
            card.headline,
            flatten(prompt, PROMPT_CHARS),
            card_link(feed_url, card.key),
        ]
        parts.append("\n".join(line for line in lines if line))
    return "\n\n".join(parts)


def evening_text(cards: list[DrillCard], *, day: date, feed_url: str) -> str:
    parts = [f"Answers · {day_label(day)}"]
    for card in cards:
        lines = [f"{TRACKS.get(card.track, 'Prep')}: {card.headline}"]
        for step in answer_steps(card):
            body = flatten(step.body, STEP_CHARS)
            lines.append(f"{step.title}: {body}" if body else step.title)
        lines.append(card_link(feed_url, card.key))
        parts.append("\n".join(line for line in lines if line))
    parts.append("Grade them in the feed: Got it, Shaky or Missed.")
    return "\n\n".join(parts)


# --- the drill ------------------------------------------------------------- #


class PrepDrill:
    """Picks, claims and sends the morning drill and the evening answers."""

    def __init__(
        self,
        db_path: str | Path,
        notify: Notifier,
        *,
        zone: tzinfo,
        feed_url: str = "",
        drill_hour: int = 9,
        answer_hour: int = 20,
        quiet_start: int = 22,
        quiet_end: int = 8,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._engine = make_engine(str(db_path))
        self.items = PrepItems(db_path)
        self.grades = GradeStore(db_path)
        self.log = DrillLog(db_path)
        self.notify = notify
        self.zone = zone
        self.feed_url = feed_url
        self.drill_hour = drill_hour
        self.answer_hour = answer_hour
        self.quiet = Pacer(0, quiet_start, quiet_end)
        self.clock = clock

    def slot_at(self, local: datetime) -> str | None:
        """The slot open at this local time: the drill from its hour until the
        answers' hour, the answers after that; none in quiet hours."""
        if self.quiet.in_quiet_hours(local):
            return None
        if self.drill_hour <= local.hour < self.answer_hour:
            return MORNING
        if local.hour >= self.answer_hour:
            return EVENING
        return None

    async def run_once(self) -> str | None:
        """Send the slot that's due, if any; return it once sent."""
        now = self.clock()
        local = now.astimezone(self.zone)
        slot = self.slot_at(local)
        if slot is None:
            return None
        day = local.date()
        if self.log.get(day, slot) is not None:
            keys = self.log.retry(day, slot, now=now)
            if keys is None:
                return None  # sent, being sent, or given up
            text = self.message(slot, self.cards(keys), day)
            if not text:
                self.log.failed(day, slot, "its cards are gone", now=now)
                return None
        else:
            if slot == MORNING:
                cards = self.pick(day, now=now)
            else:
                morning = self.log.get(day, MORNING)
                if morning is None or morning.sent_at is None:
                    return None  # answers only follow a drill
                cards = self.cards(morning.keys.split(","))
            text = self.message(slot, cards, day)
            if not text or not self.log.claim(
                day, slot, [c.key for c in cards], now=now
            ):
                return None
        try:
            await asyncio.to_thread(self.notify, text)
        except Exception as exc:  # noqa: BLE001 - retried on a later pass
            self.log.failed(day, slot, first_line(exc), now=self.clock())
            _LOG.warning("Prep drill (%s %s): %s", day, slot, exc)
            return None
        self.log.sent(day, slot, now=self.clock())
        return slot

    def message(self, slot: str, cards: list[DrillCard], day: date) -> str:
        if not cards:
            return ""
        if slot == MORNING:
            return morning_text(
                cards, day=day, feed_url=self.feed_url, answer_hour=self.answer_hour
            )
        return evening_text(cards, day=day, feed_url=self.feed_url)

    # --- cards ----------------------------------------------------------- #

    def _load(self, *where, now: datetime | None = None) -> list[DrillCard]:
        state = WebFeedStateRow
        stmt = (
            select(
                InsightRow.key,
                InsightRow.type,
                InsightRow.title,
                InsightRow.text,
                InsightRow.tags,
                InsightRow.source_session,
                state.seen_at,
                state.due_at,
            )
            .join(state, state.key == InsightRow.key, isouter=True)
            .where(*where)
            .order_by(InsightRow.key)
        )
        with session_scope(self._engine) as session:
            rows = session.exec(stmt).all()
        labels = self.items.labels()
        stamp = iso(now or self.clock())
        return [
            DrillCard(
                key=key,
                type=type_,
                title=title or "",
                text=text,
                tags=tuple(filter(None, (tags or "").split(","))),
                source=labels.get(source, ""),
                seen=seen_at is not None,
                due=due_at is not None and due_at <= stamp,
            )
            for key, type_, title, text, tags, source, seen_at, due_at in rows
        ]

    def cards(self, keys: list[str]) -> list[DrillCard]:
        """These cards, in this order (gone ones left out)."""
        found = {c.key: c for c in self._load(InsightRow.key.in_(keys))}
        return [found[key] for key in keys if key in found]

    def pick(self, day: date, *, now: datetime) -> list[DrillCard]:
        """Today's DSA card and design card (either may be missing)."""
        yesterday = (day - timedelta(days=1)).isoformat()
        recent = self.log.drilled(day - timedelta(days=RECENT_DAYS), day)
        pool = self._load(
            InsightRow.type.in_(types_for(DSA) + types_for(DESIGN)),
            WebFeedStateRow.dismissed_at.is_(None),
            now=now,
        )
        chosen = []
        for track in (DSA, DESIGN):
            weak = weak_spots(self.grades, track)
            options = [
                card
                for card in pool
                if card.track == track and recent.get(card.key) != yesterday
            ]
            if options:
                chosen.append(min(options, key=lambda c: _rank(c, weak, recent, day)))
        return chosen


def _rank(card: DrillCard, weak: list[str], recent: dict[str, str], day: date):
    """Lower is better: due or unseen, on a weak tag (the weakest first), not
    drilled this week, must-do, a problem rather than a quiz; ties rotate."""
    weak_rank = min((weak.index(t) for t in card.tags if t in weak), default=len(weak))
    return (
        not (card.due or not card.seen),
        weak_rank,
        card.key in recent,
        MUST_DO not in card.tags,
        card.type != PROBLEM_TYPE,
        hashlib.sha1(f"{day}|{card.key}".encode()).hexdigest(),
    )


async def drill_forever(drill: PrepDrill, *, every: float = CHECK_EVERY) -> None:
    """Look at the clock every few minutes and send what's due (the web
    server's background loop). Never raises; one bad pass is logged."""
    while True:
        try:
            await drill.run_once()
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            _LOG.exception("Prep drill pass failed")
        await asyncio.sleep(every)
