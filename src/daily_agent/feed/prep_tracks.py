"""Interview prep tracks and the shared way to put prep cards in the feed (#130).

The maintainer prepares for interviews with three tracks, each a deck in the
Interview tab:

- **Resume** (``interview`` flashcards and ``concept`` explainers from the
  resume deep dive, #108);
- **DSA** (``dsa`` problems and ``drill`` "which pattern?" quizzes);
- **System design** (``design`` walkthroughs, building blocks and recall
  cards).

Every prep card is an ``insights`` row whose key starts with ``prep:``, status
``web-only`` (never Telegram). Features that add cards (pack imports, daily
writers, cards from videos or study sessions) go through ``PrepItems.put``
under a labelled source, so the feed can name where a card came from and a
re-import can replace its own cards without touching anyone else's.

A card's text can reveal in steps: everything before the first line starting
with ``### `` is the prompt, and each ``### <title>`` section after it is one
tap-to-reveal step ("Pattern", "Key idea", "Complexity", "Solution", or
"Answer"). Resume flashcards keep their single hidden answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..models import Insight
from .insights_store import InsightRow, InsightStore

KEY_PREFIX = "prep:"
STATUS = "web-only"
SCORE = 0.5

# Tracks, in the order the Interview tab shows them.
RESUME, DSA, DESIGN = "resume", "dsa", "design"
TRACKS: dict[str, str] = {RESUME: "Resume", DSA: "DSA", DESIGN: "System design"}

# Prep types (``insights.type``).
CARD_TYPE = "interview"  # resume flashcard: question + hidden answer
CONCEPT_TYPE = "concept"  # resume concept explainer
PROBLEM_TYPE = "dsa"  # a DSA problem whose solution reveals in steps
DRILL_TYPE = "drill"  # "which pattern?" quiz: scenario, options, answer step
DESIGN_TYPE = "design"  # system design walkthrough, building block, recall card

TRACK_TYPES: dict[str, tuple[str, ...]] = {
    RESUME: (CARD_TYPE, CONCEPT_TYPE),
    DSA: (PROBLEM_TYPE, DRILL_TYPE),
    DESIGN: (DESIGN_TYPE,),
}
PREP_TYPES: tuple[str, ...] = tuple(t for ts in TRACK_TYPES.values() for t in ts)

STEP_MARK = "### "
_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def track_of(type_: str) -> str | None:
    """The track a prep type belongs to (``None`` for anything else)."""
    return next((t for t, types in TRACK_TYPES.items() if type_ in types), None)


def types_for(track: str) -> tuple[str, ...]:
    return TRACK_TYPES.get(track, ())


def slug(text: str, *, limit: int = 60) -> str:
    """``"Top-K Trending SKUs!"`` -> ``"top-k-trending-skus"``."""
    return _SLUG_BAD.sub("-", text.lower()).strip("-")[:limit].strip("-")


def item_key(source_id: str, name: str) -> str:
    """Key for a card from ``source_id``, e.g. ``prep:dsa-blinkit-r1:10-top-k``."""
    return f"{KEY_PREFIX}{slug(source_id)}:{slug(name)}"


def source_session(source_id: str) -> str:
    return KEY_PREFIX + slug(source_id)


# --- steps --------------------------------------------------------------- #


@dataclass(frozen=True)
class Step:
    title: str
    body: str


def split_steps(text: str) -> tuple[str, list[Step]]:
    """``(prompt, steps)``: the text before the first ``### `` line, then one
    step per ``### <title>`` section. A text without steps is all prompt."""
    prompt: list[str] = []
    steps: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(STEP_MARK) and line[4:].strip():
            steps.append((line[4:].strip(), []))
        elif steps:
            steps[-1][1].append(line)
        else:
            prompt.append(line)
    return (
        "\n".join(prompt).strip(),
        [Step(title, "\n".join(body).strip()) for title, body in steps],
    )


def join_steps(prompt: str, steps: Iterable[tuple[str, str] | Step]) -> str:
    """The inverse of ``split_steps``, for writers building a card's text."""
    parts = [prompt.strip()]
    for step in steps:
        title, body = (step.title, step.body) if isinstance(step, Step) else step
        parts.append(f"{STEP_MARK}{title.strip()}\n{body.strip()}")
    return "\n\n".join(p for p in parts if p)


# --- storage --------------------------------------------------------------- #


class PrepSourceRow(SQLModel, table=True):
    """Where prep cards came from: a pack, a daily writer, a video channel."""

    __tablename__ = "prep_sources"

    id: str = Field(primary_key=True)  # "dsa-blinkit-r1"
    label: str  # "Blinkit DSA pack", shown as the card's source
    track: str = ""
    updated_at: str


@dataclass
class PrepItem:
    """One card to put in the feed. ``text`` may hold ``### `` steps."""

    key: str
    type: str
    title: str
    text: str
    tags: list[str] = field(default_factory=list)
    captured_at: datetime | None = None
    score: float = SCORE


@dataclass
class PutReport:
    added: int = 0
    updated: int = 0
    removed: int = 0


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _clean_tags(tags: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(slug(t, limit=40) for t in tags if slug(t, limit=40)))


class PrepItems:
    """Prep cards in ``insights`` grouped by labelled source (``prep_sources``)."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        self.insights = InsightStore(db_path)
        create_tables(self._engine, PrepSourceRow)

    def put(
        self,
        source_id: str,
        label: str,
        items: Iterable[PrepItem],
        *,
        track: str = "",
        now: datetime | None = None,
        replace: bool = False,
    ) -> PutReport:
        """Add new cards and update changed ones in place (keeping what the
        reader has seen, saved or graded). With ``replace``, this source's
        cards that aren't in ``items`` are removed (a re-import)."""
        moment = now or datetime.now(timezone.utc)
        session_id = source_session(source_id)
        items = list(items)
        for item in items:
            if not item.key.startswith(KEY_PREFIX):
                raise ValueError(f"prep keys start with {KEY_PREFIX!r}: {item.key!r}")
            if item.type not in PREP_TYPES:
                raise ValueError(f"not a prep type: {item.type!r}")
        values = {"label": label, "track": track, "updated_at": _iso(moment)}
        with session_scope(self._engine) as session:
            stmt = conflict_insert(session, PrepSourceRow).values(
                id=slug(source_id), **values
            )
            session.execute(
                stmt.on_conflict_do_update(index_elements=["id"], set_=values)
            )

        report = PutReport()
        for item in items:
            tags = _clean_tags(item.tags)
            added = self.insights.add(
                Insight(
                    key=item.key,
                    text=item.text,
                    title=item.title,
                    type=item.type,
                    tags=tags,
                    score=item.score,
                    source_session=session_id,
                    captured_at=item.captured_at or moment,
                    status=STATUS,
                )
            )
            if added:
                report.added += 1
            elif self._update(item, tags, session_id):
                report.updated += 1
        if replace:
            keep = {item.key for item in items}
            with session_scope(self._engine) as session:
                stale = [
                    key
                    for key in session.exec(
                        select(InsightRow.key).where(
                            InsightRow.source_session == session_id
                        )
                    ).all()
                    if key not in keep
                ]
            report.removed = self.insights.delete(stale)
        return report

    def _update(self, item: PrepItem, tags: list[str], session_id: str) -> bool:
        with session_scope(self._engine) as session:
            row = session.get(InsightRow, item.key)
            if row is None:
                return False
            new = (item.text, item.title or None, ",".join(tags), item.type)
            if (row.text, row.title, row.tags, row.type) == new:
                return False
            row.text, row.title, row.tags, row.type = new
            row.source_session = session_id
            session.add(row)
            return True

    def remove_source(self, source_id: str) -> int:
        """Drop a source and every card it put in the feed."""
        session_id = source_session(source_id)
        with session_scope(self._engine) as session:
            keys = list(
                session.exec(
                    select(InsightRow.key).where(
                        InsightRow.source_session == session_id
                    )
                ).all()
            )
            session.execute(
                delete(PrepSourceRow).where(PrepSourceRow.id == slug(source_id))
            )
        return self.insights.delete(keys)

    def sources(self) -> list[PrepSourceRow]:
        with session_scope(self._engine) as session:
            return list(
                session.exec(select(PrepSourceRow).order_by(PrepSourceRow.id)).all()
            )

    def labels(self) -> dict[str, str]:
        """``{"prep:dsa-blinkit-r1": "Blinkit DSA pack"}`` for source labels."""
        return {KEY_PREFIX + row.id: row.label for row in self.sources()}
