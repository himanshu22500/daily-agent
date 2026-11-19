"""Interview prep from a resume deep dive, in the feed (issue #108).

``daily-agent prep import <folder>`` reads the folder once
(``sources.deep_dive``). Running it again after editing the notes updates the
feed to match; nothing watches the folder.

- Each answered interview question becomes a flashcard: an ``insights`` row
  with key ``prep:<point>-q<n>``, type ``interview``, the question as its
  headline, and the answer as its text. The reader hides the answer until it's
  tapped. Tags are the point id and the level. A re-import updates changed
  answers in place, keeping what the reader has seen or saved, and removes
  cards whose question is gone.
- Each "concept to master" is queued in ``prep_concepts``. The web server's
  background loop (or ``daily-agent prep write``) has Claude write an
  explainer for it: key ``prep:<point>-c<n>``, type ``concept``. A concept
  whose text changes is written again.

Everything is ``web-only`` (never Telegram). The source label is the resume
point, e.g. "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7".
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Field, SQLModel, select

from ..agents.concept_writer import ConceptRequest
from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..models import Insight, VideoStory
from ..sources.deep_dive import DeepDive, Flashcard, read_deep_dive
from .insights_store import InsightRow, InsightStore
from .story_queue import (
    DONE,
    FAILED,
    PENDING,
    WRITING,
    StoryQueue,
    SyncReport,
    first_line,
    iso,
    now_utc,
)

_LOG = logging.getLogger(__name__)

KEY_PREFIX = "prep:"
CARD_TYPE = "interview"
CONCEPT_TYPE = "concept"
STATUS = "web-only"
SCORE = 0.5
PER_RUN = 5
LEVELS = {"surface": "Surface", "deep-dive": "Deep dive", "curveball": "Curveball"}


def card_key(point_id: str, number: int) -> str:
    return f"{KEY_PREFIX}{point_id.lower()}-q{number}"


def concept_id(point_id: str, number: int) -> str:
    return f"{point_id.lower()}-c{number}"


def concept_key(row_id: str) -> str:
    return KEY_PREFIX + row_id


def source_of(point_id: str) -> str:
    return KEY_PREFIX + point_id.lower()


def level_label(tags: list[str]) -> str:
    """ "Deep dive" for a flashcard tagged ``deep-dive`` ("" if none)."""
    return next((LEVELS[t] for t in tags if t in LEVELS), "")


def card_text(card: Flashcard) -> str:
    if not card.still_open:
        return card.answer
    return f"{card.answer}\n\n**Still open:** {card.still_open}."


class PrepPointRow(SQLModel, table=True):
    __tablename__ = "prep_points"

    id: str = Field(primary_key=True)  # "t5"
    label_id: str  # "T5"
    job: str = ""
    name: str = ""
    title: str = ""
    resume_line: str = ""
    path: str = ""
    notes: str = ""
    imported_at: str


class PrepConceptRow(SQLModel, table=True):
    __tablename__ = "prep_concepts"

    id: str = Field(primary_key=True)  # "t5-c3"
    point_id: str  # "t5"
    concept: str
    published_at: str  # when it was (re)queued; orders the queue
    found_at: str
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""


@dataclass
class ImportReport:
    points: int = 0
    cards_added: int = 0
    cards_updated: int = 0
    cards_removed: int = 0
    unanswered: int = 0
    concepts_queued: int = 0
    concepts_removed: int = 0


@dataclass(frozen=True)
class PrepProgress:
    cards: int
    explainers: int  # written
    queued: int
    writing: int
    failed: list[tuple[str, str]]  # (concept, error)


class PrepStore(StoryQueue):
    """The ``prep_points`` and ``prep_concepts`` tables, and the prep entries
    in ``insights``. Explainers are queued through the shared ``StoryQueue``
    (ready, claim, finish, fail_attempt)."""

    item_row = PrepConceptRow
    item_id = "id"

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        self.insights = InsightStore(db_path)
        create_tables(self._engine, PrepPointRow, PrepConceptRow)

    # --- import ------------------------------------------------------------ #
    def import_deep_dive(self, deep: DeepDive, *, now: datetime) -> ImportReport:
        report = ImportReport(points=len(deep.points), unanswered=deep.unanswered)
        sources = [source_of(p.id) for p in deep.points]
        with session_scope(self._engine) as session:
            for point in deep.points:
                values = {
                    "label_id": point.id,
                    "job": point.group,
                    "name": point.name,
                    "title": point.title,
                    "resume_line": point.resume_line,
                    "path": point.path,
                    "notes": point.notes,
                    "imported_at": iso(now),
                }
                stmt = conflict_insert(session, PrepPointRow).values(
                    id=point.id.lower(), **values
                )
                session.execute(
                    stmt.on_conflict_do_update(index_elements=["id"], set_=values)
                )

        keys = set()
        for card in deep.cards:
            key = card_key(card.point_id, card.number)
            keys.add(key)
            tags = [card.point_id.lower(), card.level]
            if self.insights.get(key) is None:
                self.insights.add(
                    Insight(
                        key=key,
                        text=card_text(card),
                        title=card.question,
                        type=CARD_TYPE,
                        tags=tags,
                        score=SCORE,
                        source_session=source_of(card.point_id),
                        captured_at=now,
                        status=STATUS,
                    )
                )
                report.cards_added += 1
            elif self.insights.update_content(
                key, text=card_text(card), title=card.question, tags=tags
            ):
                report.cards_updated += 1
        with session_scope(self._engine) as session:
            stale = [
                key
                for key in session.exec(
                    select(InsightRow.key)
                    .where(InsightRow.type == CARD_TYPE)
                    .where(InsightRow.source_session.in_(sources))
                ).all()
                if key not in keys
            ]
        report.cards_removed = self.insights.delete(stale)

        wanted = {concept_id(c.point_id, c.number): c for c in deep.concepts}
        point_ids = [p.id.lower() for p in deep.points]
        with session_scope(self._engine) as session:
            known = {
                row.id: row
                for row in session.exec(
                    select(PrepConceptRow).where(PrepConceptRow.point_id.in_(point_ids))
                ).all()
            }
            for row_id, concept in wanted.items():
                row = known.get(row_id)
                if row is None:
                    session.add(
                        PrepConceptRow(
                            id=row_id,
                            point_id=concept.point_id.lower(),
                            concept=concept.text,
                            published_at=iso(now),
                            found_at=iso(now),
                        )
                    )
                    report.concepts_queued += 1
                elif row.concept != concept.text or row.state == FAILED:
                    # Changed (write it again) or given up on (try again).
                    row.concept, row.state, row.attempts = concept.text, PENDING, 0
                    row.error, row.not_before, row.claimed_at = "", None, None
                    row.published_at = iso(now)
                    session.add(row)
                    report.concepts_queued += 1
            gone = [row_id for row_id in known if row_id not in wanted]
            if gone:
                session.execute(
                    delete(PrepConceptRow).where(PrepConceptRow.id.in_(gone))
                )
        report.concepts_removed = len(gone)
        self.insights.delete([concept_key(row_id) for row_id in gone])
        return report

    # --- reading ----------------------------------------------------------- #
    def point(self, point_id: str) -> PrepPointRow | None:
        with session_scope(self._engine) as session:
            return session.get(PrepPointRow, point_id.lower())

    def points(self) -> list[PrepPointRow]:
        """Resume points in the tracker's order (their files sort that way:
        ``01-tranzact/05-...`` comes before ``02-edra-labs/...``)."""
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(PrepPointRow).order_by(PrepPointRow.path, PrepPointRow.id)
                ).all()
            )

    def labels(self) -> dict[str, str]:
        """``{"prep:t5": "T5 · <point name>"}`` for every entry's source label."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(PrepPointRow.id, PrepPointRow.label_id, PrepPointRow.name)
            ).all()
        return {KEY_PREFIX + pid: f"{label} · {name}" for pid, label, name in rows}

    def progress(self) -> PrepProgress:
        with session_scope(self._engine) as session:
            concepts = session.exec(select(PrepConceptRow)).all()
            cards = session.exec(
                select(InsightRow.key).where(InsightRow.type == CARD_TYPE)
            ).all()
        states = Counter(c.state for c in concepts)
        return PrepProgress(
            cards=len(cards),
            explainers=states[DONE],
            queued=states[PENDING],
            writing=states[WRITING],
            failed=[(c.concept, c.error) for c in concepts if c.state == FAILED],
        )


ExplainerWriter = Callable[[ConceptRequest], Awaitable[VideoStory]]


class PrepSync:
    """Import a deep-dive folder and write its concept explainers."""

    def __init__(
        self,
        db_path: str | Path,
        write_explainer: ExplainerWriter,
        *,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = PrepStore(db_path)
        self.write_explainer = write_explainer
        self.per_run = max(1, per_run)
        self.clock = clock

    def import_folder(
        self, root: str | Path, *, jobs_only: bool = True
    ) -> ImportReport:
        """Read the folder and bring the feed in line with it. Raises
        ``DeepDiveError`` if it isn't a deep-dive folder."""
        deep = read_deep_dive(root, jobs_only=jobs_only)
        return self.store.import_deep_dive(deep, now=self.clock())

    async def run_once(self, *, limit: int | None = None) -> SyncReport:
        """Write up to ``limit`` queued explainers."""
        report = SyncReport()
        for row in self.store.ready(now=self.clock(), limit=limit or self.per_run):
            await self._write(row, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _write(self, row: PrepConceptRow, report: SyncReport) -> None:
        if not self.store.claim(row.id, now=self.clock()):
            return
        point = self.store.point(row.point_id)
        if point is None:  # its point was removed; nothing to write
            self.store.finish(row.id)
            return
        request = ConceptRequest(
            concept=row.concept,
            point_id=point.label_id,
            point_name=point.name,
            group=point.job,
            resume_line=point.resume_line,
            notes=point.notes,
        )
        try:
            story = await self.write_explainer(request)
        except Exception as exc:  # noqa: BLE001 - one concept never stops the rest
            error = f"writing: {first_line(exc)}"
            if self.store.fail_attempt(row.id, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"{row.concept[:60]}: {error}")
            _LOG.warning("Concept %s: %s", row.id, error)
            return
        key = concept_key(row.id)
        tags = list(dict.fromkeys([*story.tags, point.id]))
        insights = self.store.insights
        if insights.get(key) is None:
            insights.add(
                Insight(
                    key=key,
                    text=story.text,
                    title=story.headline,
                    type=CONCEPT_TYPE,
                    tags=tags,
                    score=SCORE,
                    source_session=source_of(point.id),
                    captured_at=self.clock(),
                    status=STATUS,
                )
            )
        else:
            insights.update_content(
                key, text=story.text, title=story.headline, tags=tags
            )
        self.store.finish(row.id)
        report.written += 1
