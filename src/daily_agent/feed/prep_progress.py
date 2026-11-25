"""How prepared the reader is: what the ``/progress`` page shows (#136).

It only reads the shared database (prep cards, ``web_feed_state`` and
``prep_grades``), so the always-on feed server shows it as well as the Mac.
Days are counted in the reader's time zone (``DAILY_AGENT_TIMEZONE``).

- a countdown to the interview (``DAILY_AGENT_INTERVIEW_DATE``, optional);
- the streak: consecutive days with at least one grade, and today's grades;
- per track: cards, how many were seen and graded, and the got-it rate;
- a tile per canonical DSA pattern (a pattern without cards is a gap) and per
  design topic, coloured by how weak the reader is on it;
- the weakest tags, and the last 7 days of grading.

Everything goes by each card's latest grade, like ``GradeStore.tag_stats``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import select

from ..db import create_tables, make_engine, session_scope
from ..sources.practice_pack import DSA_PATTERNS, MUST_DO
from .insights_store import InsightRow
from .prep_grades import GOT, MISSED, SHAKY, GradeStore, TagStat
from .prep_tracks import (
    DESIGN,
    DSA,
    PREP_TYPES,
    TRACKS,
    PrepItems,
    track_of,
    types_for,
)
from .web_feed import WebFeedStateRow

_LOG = logging.getLogger(__name__)

# The pattern grid shows the canonical DSA patterns (``DSA_PATTERNS``, shared
# with the practice packs, #140) in their order. These tags name where a card
# came from or its priority rather than a skill.
NOT_SKILLS = ("blinkit", MUST_DO)
WEAK_SPOTS = 5
TOPICS = 24
WEEK = 7
# Weakness (0 all got .. 1 all missed) from which a tag counts as mixed / weak.
MIXED_FROM, WEAK_FROM = 0.25, 0.6
LEVELS = {
    "gap": "No cards yet",
    "new": "Not graded",
    "strong": "Strong",
    "mixed": "Mixed",
    "weak": "Weak",
}


def local_zone(name: str) -> tzinfo:
    """``"Asia/Kolkata"`` -> its zone. An unknown or empty name means UTC,
    with a warning: a typo in ``DAILY_AGENT_TIMEZONE`` must not stop the
    feed server from starting."""
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        _LOG.warning("Unknown time zone %r; using UTC", name)
        return timezone.utc


def interview_day(value: str) -> date | None:
    """``DAILY_AGENT_INTERVIEW_DATE`` as a date (``None`` if unset or bad)."""
    try:
        return date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def day_label(day: date) -> str:
    """``"Wed 25 Nov"``."""
    return f"{day:%a} {day.day} {day:%b}"


def tag_label(tag: str) -> str:
    """``"rate-limiting"`` -> ``"Rate limiting"``; patterns by their name."""
    if tag in DSA_PATTERNS:
        return DSA_PATTERNS[tag]
    words = tag.replace("-", " ").split()
    return " ".join(words).capitalize() if words else tag


def weak_spots(grades: GradeStore, track: str, *, limit: int = WEAK_SPOTS) -> list[str]:
    """The reader's weakest tags in a track (weakest first). DSA keeps only
    the canonical patterns; tags naming a source never count."""
    tags = grades.weak_tags(types_for(track), limit=50, ignore=NOT_SKILLS)
    if track == DSA:
        tags = [t for t in tags if t in DSA_PATTERNS]
    return tags[:limit]


@dataclass(frozen=True)
class Tile:
    """One pattern or topic: its cards and how their latest grades split."""

    tag: str
    track: str
    cards: int = 0
    got: int = 0
    shaky: int = 0
    missed: int = 0

    @property
    def label(self) -> str:
        return tag_label(self.tag)

    @property
    def track_label(self) -> str:
        return TRACKS[self.track]

    @property
    def graded(self) -> int:
        return self.got + self.shaky + self.missed

    @property
    def ungraded(self) -> int:
        return max(0, self.cards - self.graded)

    @property
    def weakness(self) -> float:
        graded = self.graded
        return (self.missed + self.shaky / 2) / graded if graded else 0.0

    @property
    def level(self) -> str:
        if not self.cards:
            return "gap"
        if not self.graded:
            return "new"
        if self.weakness >= WEAK_FROM:
            return "weak"
        return "mixed" if self.weakness >= MIXED_FROM else "strong"

    @property
    def level_label(self) -> str:
        return LEVELS[self.level]


def _tile(stat: TagStat | None, tag: str, track: str) -> Tile:
    if stat is None:
        return Tile(tag, track)
    return Tile(tag, track, stat.cards, stat.got, stat.shaky, stat.missed)


@dataclass(frozen=True)
class TrackProgress:
    track: str
    cards: int = 0
    seen: int = 0
    got: int = 0
    shaky: int = 0
    missed: int = 0

    @property
    def label(self) -> str:
        return TRACKS[self.track]

    @property
    def graded(self) -> int:
        return self.got + self.shaky + self.missed

    @property
    def ungraded(self) -> int:
        return max(0, self.cards - self.graded)

    @property
    def got_rate(self) -> int | None:
        """Got-it share of the graded cards, in percent."""
        return round(100 * self.got / self.graded) if self.graded else None


@dataclass(frozen=True)
class DayTally:
    day: date
    got: int = 0
    shaky: int = 0
    missed: int = 0

    @property
    def total(self) -> int:
        return self.got + self.shaky + self.missed

    @property
    def label(self) -> str:
        return day_label(self.day)


@dataclass(frozen=True)
class Progress:
    today: date
    interview_date: date | None
    streak: int
    tracks: list[TrackProgress]
    patterns: list[Tile]
    topics: list[Tile]
    weak: list[Tile]
    week: list[DayTally]  # oldest first; the last one is today

    @property
    def days_left(self) -> int | None:
        """Days to the interview (``None`` when unset or past)."""
        if self.interview_date is None or self.interview_date < self.today:
            return None
        return (self.interview_date - self.today).days

    @property
    def today_tally(self) -> DayTally:
        return self.week[-1]

    @property
    def week_max(self) -> int:
        return max((d.total for d in self.week), default=0)

    @property
    def graded_ever(self) -> bool:
        return any(t.graded for t in self.tracks)


def streak(days: set[date], today: date) -> int:
    """Consecutive days with a grade, ending today, or yesterday while today
    has none yet (a streak isn't broken until the day is over)."""
    day = today if today in days else today - timedelta(days=1)
    count = 0
    while day in days:
        count += 1
        day -= timedelta(days=1)
    return count


class PrepProgress:
    """Builds the page's numbers from the shared database."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        PrepItems(db_path)  # the card tables, on a fresh database
        self.grades = GradeStore(db_path)
        create_tables(self._engine, WebFeedStateRow)

    def tracks(self) -> list[TrackProgress]:
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(InsightRow.key, InsightRow.type, WebFeedStateRow.seen_at)
                .join(
                    WebFeedStateRow,
                    WebFeedStateRow.key == InsightRow.key,
                    isouter=True,
                )
                .where(InsightRow.type.in_(PREP_TYPES))
            ).all()
        latest = self.grades.latest()
        counts = {
            track: {"cards": 0, "seen": 0, GOT: 0, SHAKY: 0, MISSED: 0}
            for track in TRACKS
        }
        for key, type_, seen_at in rows:
            count = counts[track_of(type_)]
            count["cards"] += 1
            count["seen"] += seen_at is not None
            if key in latest:
                count[latest[key].grade] += 1
        return [
            TrackProgress(track, c["cards"], c["seen"], c[GOT], c[SHAKY], c[MISSED])
            for track, c in counts.items()
        ]

    def build(
        self, *, now: datetime, zone: tzinfo, interview_date: date | None = None
    ) -> Progress:
        today = now.astimezone(zone).date()
        dsa = {s.tag: s for s in self.grades.tag_stats(types_for(DSA))}
        design = {s.tag: s for s in self.grades.tag_stats(types_for(DESIGN))}
        patterns = [_tile(dsa.get(tag), tag, DSA) for tag in DSA_PATTERNS]
        topics = [
            _tile(stat, tag, DESIGN)
            for tag, stat in design.items()
            if tag not in NOT_SKILLS
        ][:TOPICS]

        rounds = types_for(DSA) + types_for(DESIGN)
        both = {s.tag: s for s in self.grades.tag_stats(rounds)}
        weak_tags = self.grades.weak_tags(rounds, limit=50, ignore=NOT_SKILLS)
        weak = [
            _tile(both.get(tag), tag, DSA if tag in dsa else DESIGN)
            for tag in weak_tags
            if tag in DSA_PATTERNS or tag not in dsa
        ][:WEAK_SPOTS]

        history = self.grades.history()
        days = {g.graded_at.astimezone(zone).date() for g in history}
        first = today - timedelta(days=WEEK - 1)
        tally = {
            first + timedelta(days=n): dict.fromkeys((GOT, SHAKY, MISSED), 0)
            for n in range(WEEK)
        }
        since = datetime.combine(first, time(), zone)
        for grade in history:
            if grade.graded_at >= since:
                day = grade.graded_at.astimezone(zone).date()
                if day in tally:
                    tally[day][grade.grade] += 1
        week = [DayTally(day, c[GOT], c[SHAKY], c[MISSED]) for day, c in tally.items()]
        return Progress(
            today=today,
            interview_date=interview_date,
            streak=streak(days, today),
            tracks=self.tracks(),
            patterns=patterns,
            topics=topics,
            weak=weak,
            week=week,
        )
