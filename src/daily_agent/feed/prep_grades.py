"""Self-grading for interview prep cards (#130).

After answering a card in their head, the reader grades it: **Got it**,
**Shaky** or **Missed**. Each grade is kept (``prep_grades``), and the card's
next review moves with it, replacing the view-count schedule for that card:

- missed: back tomorrow;
- shaky: in 3 days;
- got it: in 7 days, then 21, then 60, as got-it grades run on in a row.

Grades also tell the daily writers and the progress page where the reader is
weak: ``tag_stats`` and ``weak_tags`` read each card's latest grade per tag
(a DSA card's tags are its patterns, a design card's its topics).

``web_feed`` must not import this module (it imports ``web_feed``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from .insights_store import InsightRow
from .prep_tracks import PREP_TYPES
from .web_feed import WebFeedStateRow

GOT, SHAKY, MISSED = "got", "shaky", "missed"
GRADES: dict[str, str] = {GOT: "Got it", SHAKY: "Shaky", MISSED: "Missed"}
MISSED_GAP = timedelta(days=1)
SHAKY_GAP = timedelta(days=3)
GOT_GAPS = (timedelta(days=7), timedelta(days=21), timedelta(days=60))


class PrepGradeRow(SQLModel, table=True):
    __tablename__ = "prep_grades"

    key: str = Field(primary_key=True)
    graded_at: str = Field(primary_key=True)
    grade: str


@dataclass(frozen=True)
class Grade:
    key: str
    grade: str
    graded_at: datetime


@dataclass(frozen=True)
class TagStat:
    """One tag across the prep cards that carry it (latest grade per card)."""

    tag: str
    cards: int
    graded: int = 0
    got: int = 0
    shaky: int = 0
    missed: int = 0

    @property
    def weakness(self) -> float:
        """0 (all got) .. 1 (all missed); shaky counts half."""
        return (self.missed + self.shaky / 2) / self.graded if self.graded else 0.0


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def next_gap(grade: str, got_streak: int) -> timedelta:
    """When a card comes back after ``grade``; ``got_streak`` counts this
    grade too when it is a got-it."""
    if grade == MISSED:
        return MISSED_GAP
    if grade == SHAKY:
        return SHAKY_GAP
    return GOT_GAPS[min(max(got_streak, 1), len(GOT_GAPS)) - 1]


class GradeStore:
    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, PrepGradeRow, WebFeedStateRow)

    def grade(self, key: str, grade: str, *, now: datetime | None = None) -> datetime:
        """Record a grade and reschedule the card; return when it's due."""
        if grade not in GRADES:
            raise ValueError(f"unknown grade: {grade!r}")
        moment = now or datetime.now(timezone.utc)
        with session_scope(self._engine) as session:
            row = session.get(InsightRow, key)
            if row is None or row.type not in PREP_TYPES:
                raise KeyError(key)
            earlier = session.exec(
                select(PrepGradeRow.grade)
                .where(PrepGradeRow.key == key)
                .order_by(PrepGradeRow.graded_at.desc())
            ).all()
            streak = 0
            if grade == GOT:
                streak = 1
                for previous in earlier:
                    if previous != GOT:
                        break
                    streak += 1
            due = moment + next_gap(grade, streak)
            stamp = _iso(moment)
            session.execute(
                conflict_insert(session, PrepGradeRow)
                .values(key=key, graded_at=stamp, grade=grade)
                .on_conflict_do_nothing(index_elements=["key", "graded_at"])
            )
            state = session.get(WebFeedStateRow, key) or WebFeedStateRow(key=key)
            state.seen_at = state.seen_at or stamp
            state.last_seen_at = stamp
            state.due_at = _iso(due)
            session.add(state)
        return due

    def latest(self, keys: Iterable[str] | None = None) -> dict[str, Grade]:
        """Each card's latest grade (cards never graded are absent)."""
        stmt = select(PrepGradeRow).order_by(PrepGradeRow.key, PrepGradeRow.graded_at)
        if keys is not None:
            stmt = stmt.where(PrepGradeRow.key.in_(list(keys)))
        with session_scope(self._engine) as session:
            rows = session.exec(stmt).all()
        return {
            r.key: Grade(r.key, r.grade, _aware(r.graded_at)) for r in rows
        }  # later rows overwrite earlier ones

    def history(self, *, since: datetime | None = None) -> list[Grade]:
        """Every grade, oldest first (for streaks and activity)."""
        stmt = select(PrepGradeRow).order_by(PrepGradeRow.graded_at, PrepGradeRow.key)
        if since is not None:
            stmt = stmt.where(PrepGradeRow.graded_at >= _iso(since))
        with session_scope(self._engine) as session:
            return [
                Grade(r.key, r.grade, _aware(r.graded_at))
                for r in session.exec(stmt).all()
            ]

    def tag_stats(self, types: Iterable[str] = PREP_TYPES) -> list[TagStat]:
        """Per tag over the prep cards of ``types``: how many cards carry it
        and how their latest grades split. Most cards first, then by tag."""
        with session_scope(self._engine) as session:
            cards = session.exec(
                select(InsightRow.key, InsightRow.tags).where(
                    InsightRow.type.in_(list(types))
                )
            ).all()
        latest = self.latest()
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for key, tags in cards:
            grade = latest.get(key)
            for tag in filter(None, (tags or "").split(",")):
                counts[tag]["cards"] += 1
                if grade:
                    counts[tag]["graded"] += 1
                    counts[tag][grade.grade] += 1
        stats = [
            TagStat(
                tag=tag,
                cards=c["cards"],
                graded=c["graded"],
                got=c[GOT],
                shaky=c[SHAKY],
                missed=c[MISSED],
            )
            for tag, c in counts.items()
        ]
        return sorted(stats, key=lambda s: (-s.cards, s.tag))

    def weak_tags(
        self,
        types: Iterable[str] = PREP_TYPES,
        *,
        limit: int = 5,
        ignore: Iterable[str] = (),
    ) -> list[str]:
        """Tags the reader misses most (weakest first); ``ignore`` drops
        tags that name a source rather than a skill (e.g. ``blinkit``)."""
        skip = set(ignore)
        weak = [
            s for s in self.tag_stats(types) if s.weakness > 0 and s.tag not in skip
        ]
        weak.sort(key=lambda s: (-s.weakness, -s.missed, s.tag))
        return [s.tag for s in weak[:limit]]
