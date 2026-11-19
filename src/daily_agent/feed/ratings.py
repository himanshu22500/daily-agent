"""Ratings: the reader's 👍 / 👎 on feed items, and why (issue #126).

A 👎 removes the item from the feed (it is hidden, like "Hide this") and may
carry reasons from a short list. A 👍 says "more like this". One rating per
item; the latest wins, and Undo clears it.

The ratings also teach the insight extractor (``agents.insight_extractor``):
``extraction_feedback`` turns recent ratings of coding insights into a short
note that goes into each extraction prompt, so Claude writes fewer items like
the 👎 ones and more like the 👍 ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from .web_feed import WebFeed

UP, DOWN = "up", "down"
# Reason code -> what the sheet's chip says. "source" and "type" read
# "Fewer from <source>" / "Fewer <type>s" on the chip.
REASONS = {
    "basic": "Too basic",
    "irrelevant": "Not relevant to me",
    "wrong": "Wrong or outdated",
    "repeat": "Repeats another",
    "source": "Fewer from this source",
    "type": "Fewer of this type",
}
# Only coding insights come from the extractor; videos, posts and prep cards
# are written elsewhere, so their ratings don't steer it.
INSIGHT_PREFIX = "insight:"
FEEDBACK_DOWN = 20
FEEDBACK_UP = 10
FEEDBACK_HEADLINE_CHARS = 140


class RatingRow(SQLModel, table=True):
    __tablename__ = "feed_ratings"

    key: str = Field(primary_key=True)
    rating: str  # "up" | "down"
    reasons: str = ""  # comma-separated codes from REASONS
    rated_at: str


@dataclass(frozen=True)
class Rating:
    key: str
    rating: str
    reasons: tuple[str, ...]
    rated_at: datetime

    @property
    def up(self) -> bool:
        return self.rating == UP

    @property
    def down(self) -> bool:
        return self.rating == DOWN


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def clean_reasons(raw: list[str]) -> list[str]:
    """Known reason codes, deduplicated, in the sheet's order."""
    chosen = {r.strip() for r in raw}
    return [code for code in REASONS if code in chosen]


def _rating(row: RatingRow) -> Rating:
    return Rating(
        key=row.key,
        rating=row.rating,
        reasons=tuple(r for r in row.reasons.split(",") if r),
        rated_at=datetime.fromisoformat(row.rated_at),
    )


class Ratings:
    """The ``feed_ratings`` table."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, RatingRow)

    def rate(self, key: str, rating: str, *, now: datetime | None = None) -> None:
        """Rate an item; a new rating replaces the old one and its reasons."""
        if rating not in (UP, DOWN):
            raise ValueError(f"not a rating: {rating!r}")
        stamp = _iso(now or datetime.now(timezone.utc))
        stmt = conflict_insert(self._engine, RatingRow).values(
            key=key, rating=rating, reasons="", rated_at=stamp
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"rating": rating, "reasons": "", "rated_at": stamp},
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)

    def set_reasons(self, key: str, reasons: list[str]) -> bool:
        """Say why a 👎; False if the item has no 👎 to explain."""
        with session_scope(self._engine) as session:
            row = session.get(RatingRow, key)
            if row is None or row.rating != DOWN:
                return False
            row.reasons = ",".join(clean_reasons(reasons))
            session.add(row)
            return True

    def clear(self, key: str) -> Rating | None:
        """Forget an item's rating (Undo); return what it was."""
        with session_scope(self._engine) as session:
            row = session.get(RatingRow, key)
            if row is None:
                return None
            gone = _rating(row)
            session.delete(row)
            return gone

    def get(self, key: str) -> Rating | None:
        with session_scope(self._engine) as session:
            row = session.get(RatingRow, key)
            return _rating(row) if row else None

    def recent(self, rating: str, *, limit: int, prefix: str = "") -> list[Rating]:
        """The latest ratings of one kind, newest first."""
        stmt = select(RatingRow).where(RatingRow.rating == rating)
        if prefix:
            stmt = stmt.where(RatingRow.key.startswith(prefix))
        stmt = stmt.order_by(RatingRow.rated_at.desc(), RatingRow.key).limit(limit)
        with session_scope(self._engine) as session:
            return [_rating(row) for row in session.exec(stmt).all()]


def _clip(text: str, limit: int = FEEDBACK_HEADLINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def extraction_feedback(db_path: str | Path) -> str:
    """The reader's ratings of coding insights, as a note for the extractor.

    "" when nothing has been rated. Lists the latest 👎 items (with reasons)
    and 👍 items by headline, type and project, and which projects and types
    the reader asked to see fewer of.
    """
    ratings = Ratings(db_path)
    down = ratings.recent(DOWN, limit=FEEDBACK_DOWN, prefix=INSIGHT_PREFIX)
    up = ratings.recent(UP, limit=FEEDBACK_UP, prefix=INSIGHT_PREFIX)
    if not down and not up:
        return ""
    feed = WebFeed(db_path)

    def line(rating: Rating) -> tuple[str, str, str] | None:
        entry = feed.entry(rating.key)
        if entry is None:
            return None
        label = " · ".join(part for part in (entry.type, entry.source) if part)
        why = ", ".join(
            REASONS[r].lower()
            for r in rating.reasons
            if r in REASONS and r not in ("source", "type")
        )
        text = f"- [{label}] {_clip(entry.headline)}" + (f" ({why})" if why else "")
        return text, entry.source, entry.type

    lines: list[str] = []
    fewer_sources: list[str] = []
    fewer_types: list[str] = []
    down_lines = []
    for rating in down:
        found = line(rating)
        if found is None:
            continue
        down_lines.append(found[0])
        if "source" in rating.reasons and found[1] and found[1] not in fewer_sources:
            fewer_sources.append(found[1])
        if "type" in rating.reasons and found[2] and found[2] not in fewer_types:
            fewer_types.append(found[2])
    up_lines = [found[0] for found in map(line, up) if found is not None]
    if down_lines:
        lines.append("Not useful (the reader removed these from the feed):")
        lines.extend(down_lines)
    if up_lines:
        lines.append("Useful (the reader wants more like these):")
        lines.extend(up_lines)
    if fewer_sources:
        lines.append(
            "The reader wants fewer insights from these projects: "
            + ", ".join(fewer_sources)
            + ". For a session in one of them, keep only clearly durable, "
            "non-obvious insights."
        )
    if fewer_types:
        lines.append(
            "The reader wants fewer insights of these types: "
            + ", ".join(fewer_types)
            + "."
        )
    return "\n".join(lines)
