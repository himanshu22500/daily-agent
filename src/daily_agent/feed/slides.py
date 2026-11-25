"""Slide stories for the web feed (issue #158).

About one in four feed items is told as a short, swipeable story in its card:
3-5 slides, each a bold caption over its own illustration, like a news
carousel. Tapping opens the full story as before. Slides are written outside
capture, a few per sweep (``insights sweep --max-slides``) or in bulk
(``insights slides-generate``), by Claude through ``agents.slide_writer``.

The model's SVG is untrusted, exactly like a cover's: every slide must pass
``covers.validate_cover`` (portrait viewBox) before anything is stored, and the
web app serves slides with the covers' locked-down headers through ``<img>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..models import Insight
from .covers import validate_cover
from .headlines import plain
from .insights_store import InsightStore, is_work_insight
from .prep_tracks import CONCEPT_TYPE, DESIGN_TYPE
from .videos import video_id_of

VIEWBOX = "0 0 800 1000"  # 4:5, the card's slide frame
MIN_SLIDES, MAX_SLIDES = 3, 5
MAX_CAPTION = 80
# One in SHARE eligible items gets slides (a stable pick by key), so the feed
# is a mix: mostly regular cards, some slide stories.
SHARE = 4
# Shorter than this there's no story to tell in slides.
MIN_TEXT = 200

# Kinds told as slides. "story": the whole thing (session insights, videos,
# concept explainers). "practice": set the problem up and end on the question,
# never the answer (system design cards reveal their answer in steps).
# Flashcards, DSA problems and drills never get slides: their answers stay
# hidden. Instagram carousels already have their own slides (#157).
_STORY_TYPES = {"video", CONCEPT_TYPE}
_PRACTICE_TYPES = {DESIGN_TYPE}


def slide_mode(insight: Insight) -> str | None:
    """ "story", "practice", or None when ``insight`` never gets slides."""
    if insight.type in _PRACTICE_TYPES:
        return "practice"
    if insight.type in _STORY_TYPES or video_id_of(insight.key):
        return "story"
    if is_work_insight(insight.key):
        return "story"
    return None


def picked(key: str) -> bool:
    """The stable ~1-in-``SHARE`` choice of which items become slide stories."""
    digest = hashlib.sha1(key.encode()).digest()
    return int.from_bytes(digest[:4], "big") % SHARE == 0


def wants_slides(insight: Insight) -> bool:
    return (
        slide_mode(insight) is not None
        and len(plain(insight.text)) >= MIN_TEXT
        and picked(insight.key)
    )


def clean_caption(raw: object) -> str:
    """A slide caption as plain, single-line text ("" if there is none)."""
    return plain(raw if isinstance(raw, str) else "").strip().strip("\"'“”")


def validate_slides(slides: object) -> list[str]:
    """Reasons ``slides`` (``[{caption, svg}]``) can't be stored (empty = fine)."""
    if not isinstance(slides, list) or not MIN_SLIDES <= len(slides) <= MAX_SLIDES:
        count = len(slides) if isinstance(slides, list) else 0
        return [f"need {MIN_SLIDES}-{MAX_SLIDES} slides, got {count}"]
    problems: list[str] = []
    for n, slide in enumerate(slides, 1):
        if not isinstance(slide, dict):
            problems.append(f"slide {n}: not an object")
            continue
        caption = clean_caption(slide.get("caption"))
        if not caption:
            problems.append(f"slide {n}: empty caption")
        elif len(caption) > MAX_CAPTION:
            problems.append(f"slide {n}: caption over {MAX_CAPTION} characters")
        svg = slide.get("svg")
        if not isinstance(svg, str):
            problems.append(f"slide {n}: no svg")
            continue
        problems += [
            f"slide {n}: {problem}"
            for problem in validate_cover(svg.encode(), viewbox=VIEWBOX)
        ]
    return problems


# --- storage --------------------------------------------------------------- #


class SlidesRow(SQLModel, table=True):
    __tablename__ = "insight_slides"

    key: str = Field(primary_key=True)
    # JSON list of {"caption": str, "svg": str}
    slides: str
    # Who wrote them, e.g. "model:claude-code:claude-opus-5-5".
    source: str = ""
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Slide:
    caption: str
    svg: str


class SlideStore:
    """Validated slide stories, keyed by insight key, in the main database."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, SlidesRow)

    def put(self, key: str, slides: object, *, source: str = "") -> list[str]:
        """Store ``slides`` for ``key`` if they validate; else return why not."""
        problems = validate_slides(slides)
        if problems:
            return problems
        body = json.dumps(
            [
                {"caption": clean_caption(s["caption"]), "svg": s["svg"]}
                for s in slides  # type: ignore[union-attr]
            ]
        )
        stamp = _now_iso()
        stmt = conflict_insert(self._engine, SlidesRow).values(
            key=key, slides=body, source=source, updated_at=stamp
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"slides": body, "source": source, "updated_at": stamp},
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)
        return []

    def get(self, key: str) -> list[Slide]:
        """The stored slides for ``key`` ([] if it has none)."""
        with session_scope(self._engine) as session:
            row = session.get(SlidesRow, key)
            if row is None:
                return []
            return [Slide(s["caption"], s["svg"]) for s in json.loads(row.slides)]

    def summary(self, key: str) -> tuple[str, list[str]] | None:
        """``(version, captions)`` without the SVGs (what a card needs)."""
        with session_scope(self._engine) as session:
            row = session.get(SlidesRow, key)
            if row is None:
                return None
            return row.updated_at, [s["caption"] for s in json.loads(row.slides)]

    def keys(self) -> set[str]:
        with session_scope(self._engine) as session:
            return set(session.exec(select(SlidesRow.key)).all())


# --- writing them ---------------------------------------------------------- #

# (record {key, title, text, type, tags, mode}, validator feedback or None)
#   -> [{caption, svg}]
SlideWriter = Callable[[dict, "list[str] | None"], Awaitable[list]]


@dataclass
class SlideGeneration:
    written: int = 0
    failed: dict[str, list[str]] = field(default_factory=dict)


def _newest_first(insight: Insight) -> tuple:
    stamp = insight.stored_at or insight.captured_at
    return (-stamp.timestamp(), insight.key)


async def generate_slides(
    store: InsightStore,
    slides: SlideStore,
    write: SlideWriter,
    *,
    source: str = "",
    limit: int | None = None,
    concurrency: int = 2,
    tries: int = 2,
    on_progress: Callable[[], None] | None = None,
    work: bool = True,
) -> SlideGeneration:
    """Write slide stories for picked items without one, newest first.

    Only what validates is stored. A rejected reply is retried with the
    validator's reasons, up to ``tries`` replies per item; a failing item never
    stops the rest and is tried again on a later run. ``work=False`` (#162):
    work insights are hidden, so they get none.
    """
    have = slides.keys()
    todo = sorted(
        (
            i
            for i in store.all()
            if i.key not in have
            and wants_slides(i)
            and (work or not is_work_insight(i.key))
        ),
        key=_newest_first,
    )
    if limit is not None:
        todo = todo[:limit]
    result = SlideGeneration()
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(insight: Insight) -> None:
        record = {
            "key": insight.key,
            "title": insight.title,
            "text": insight.text,
            "type": insight.type,
            "tags": insight.tags,
            "mode": slide_mode(insight),
        }
        feedback: list[str] | None = None
        async with gate:
            for _ in range(max(1, tries)):
                if on_progress is not None:
                    on_progress()
                try:
                    reply = await write(record, feedback)
                except Exception as exc:  # noqa: BLE001 - one failure never stops the batch
                    result.failed[insight.key] = [
                        (str(exc).splitlines() or [type(exc).__name__])[0][:200]
                    ]
                    return
                feedback = slides.put(insight.key, reply, source=source)
                if not feedback:
                    result.written += 1
                    result.failed.pop(insight.key, None)
                    return
            result.failed[insight.key] = feedback or ["no valid slides"]

    await asyncio.gather(*(one(insight) for insight in todo))
    return result
