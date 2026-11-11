"""Illustrated covers for the web feed (issue #87).

A cover is one SVG illustration per insight, stored in the ``insight_covers``
table of the main database (so database backups include them). Covers are
drawn outside capture: by illustrator subagents (imported from a directory of
``<slug>.svg`` files) or by ``covers-generate`` with the model. Either way the
SVG is untrusted: ``validate_cover`` only accepts a small, static,
self-contained subset of SVG, and nothing reaches the table without passing.
The web app additionally serves covers with a locked-down CSP and shows them
through ``<img>``, where SVG scripts never run.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from ..db import create_tables, make_engine, session_scope
from ..models import Insight
from .insights_store import InsightStore

SVG_NS = "http://www.w3.org/2000/svg"
VIEWBOX = "0 0 800 400"
MIN_BYTES = 600
MAX_BYTES = 12_000
ALLOWED_TAGS = {
    "svg", "g", "defs", "title", "desc", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "path", "text", "tspan", "linearGradient",
    "radialGradient", "stop", "clipPath", "mask", "pattern", "use",
}  # fmt: skip
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)


def cover_slug(key: str) -> str | None:
    """URL/file stem for an insight key, or ``None`` if it isn't path-safe."""
    slug = key.removeprefix("insight:")
    return slug if _SLUG.fullmatch(slug) else None


def validate_cover(data: bytes) -> list[str]:
    """Reasons ``data`` is not an acceptable cover (empty list = acceptable)."""
    if not MIN_BYTES <= len(data) <= MAX_BYTES:
        return [f"size {len(data)} bytes (allowed {MIN_BYTES}-{MAX_BYTES})"]
    lowered = data.lower()
    # Refuse DTDs/entities before parsing (entity-expansion and XXE tricks).
    for marker in (b"<!doctype", b"<!entity", b"<?xml-stylesheet"):
        if marker in lowered:
            return [f"contains {marker.decode()}"]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"]
    problems: list[str] = []
    if root.tag != f"{{{SVG_NS}}}svg":
        problems.append("root element is not <svg> in the SVG namespace")
    if " ".join((root.get("viewBox") or "").split()) != VIEWBOX:
        problems.append(f'viewBox must be "{VIEWBOX}"')
    for element in root.iter():
        namespace, _, tag = element.tag.rpartition("}")
        if namespace != "{" + SVG_NS or tag not in ALLOWED_TAGS:
            problems.append(f"element <{tag}> is not allowed")
            continue
        for name, value in element.attrib.items():
            attr = name.rpartition("}")[2].lower()
            if attr.startswith("on"):
                problems.append(f"event attribute {attr}")
            elif attr == "href" and not value.startswith("#"):
                problems.append("href must point inside the file (#id)")
            elif attr == "style" and ("@import" in value or "expression" in value):
                problems.append("unsafe style attribute")
            elif _EXTERNAL_URL.search(value):
                problems.append(f"external url() in {attr}")
    return list(dict.fromkeys(problems))


# --- storage --------------------------------------------------------------- #


class CoverRow(SQLModel, table=True):
    __tablename__ = "insight_covers"

    key: str = Field(primary_key=True)
    svg: str
    # Who drew it, e.g. "subagent:fable" or "model:google:gemini-3.8-flash".
    source: str = ""
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class CoverStore:
    """Validated cover SVGs, keyed by insight key, in the main database."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, CoverRow)

    def put(self, key: str, svg: str, *, source: str = "") -> list[str]:
        """Store ``svg`` for ``key`` if it validates; return the problems if not."""
        problems = validate_cover(svg.encode())
        if problems:
            return problems
        stamp = _now_iso()
        stmt = sqlite_insert(CoverRow).values(
            key=key, svg=svg, source=source, updated_at=stamp
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"svg": svg, "source": source, "updated_at": stamp},
        )
        with session_scope(self._engine) as session:
            session.execute(stmt)
        return []

    def get(self, key: str) -> str | None:
        with session_scope(self._engine) as session:
            row = session.get(CoverRow, key)
            return row.svg if row else None

    def version(self, key: str) -> str | None:
        """Changes whenever the cover does (for cache-busting URLs)."""
        with session_scope(self._engine) as session:
            stamp = session.exec(
                select(CoverRow.updated_at).where(CoverRow.key == key)
            ).first()
        return stamp

    def keys(self) -> set[str]:
        with session_scope(self._engine) as session:
            return set(session.exec(select(CoverRow.key)).all())


# --- bringing covers in ------------------------------------------------------ #


@dataclass
class CoverImport:
    imported: int = 0
    rejected: dict[str, list[str]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)


def import_covers(
    source_dir: str | Path,
    covers: CoverStore,
    known_keys: set[str],
    *,
    source: str = "import",
) -> CoverImport:
    """Store valid ``<slug>.svg`` files for known insights.

    Files for unknown insights are skipped; invalid ones are reported with
    their reasons. An existing cover is replaced by a valid file.
    """
    by_slug = {cover_slug(key): key for key in known_keys}
    by_slug.pop(None, None)
    result = CoverImport()
    for path in sorted(Path(source_dir).glob("*.svg")):
        key = by_slug.get(path.stem)
        if key is None:
            result.unknown.append(path.name)
            continue
        try:
            svg = path.read_bytes().decode()
        except UnicodeDecodeError:
            result.rejected[path.name] = ["not UTF-8 text"]
            continue
        problems = covers.put(key, svg, source=source)
        if problems:
            result.rejected[path.name] = problems
        else:
            result.imported += 1
    return result


# (insight record, validator feedback from a rejected try or None) -> SVG text
CoverDrawer = Callable[[dict, "list[str] | None"], Awaitable[str]]


@dataclass
class CoverGeneration:
    written: int = 0
    failed: dict[str, list[str]] = field(default_factory=dict)


async def generate_covers(
    store: InsightStore,
    covers: CoverStore,
    draw: CoverDrawer,
    *,
    source: str = "",
    limit: int | None = None,
    concurrency: int = 4,
    tries: int = 2,
    overwrite: bool = False,
) -> CoverGeneration:
    """Draw covers for insights without one; store only those that validate.

    Only insights with no cover are drawn unless ``overwrite``. A rejected
    drawing is retried with the validator's reasons as feedback, up to
    ``tries`` drawings per insight. A failing insight never stops the rest.
    """
    have = set() if overwrite else covers.keys()
    todo = [
        insight
        for insight in sorted(store.all(), key=lambda i: i.key)
        if cover_slug(insight.key) and insight.key not in have
    ]
    if limit is not None:
        todo = todo[:limit]
    result = CoverGeneration()
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(insight: Insight) -> None:
        record = {
            "key": insight.key,
            "title": insight.title,
            "text": insight.text,
            "type": insight.type,
            "tags": insight.tags,
        }
        feedback: list[str] | None = None
        async with gate:
            for _ in range(max(1, tries)):
                try:
                    svg = await draw(record, feedback)
                except Exception as exc:  # noqa: BLE001 - one failure never stops the batch
                    result.failed[insight.key] = [
                        (str(exc).splitlines() or [type(exc).__name__])[0][:200]
                    ]
                    return
                if not svg:
                    feedback = ["reply contained no <svg>"]
                    continue
                feedback = covers.put(insight.key, svg, source=source)
                if not feedback:
                    result.written += 1
                    result.failed.pop(insight.key, None)
                    return
            result.failed[insight.key] = feedback or ["no valid drawing"]

    await asyncio.gather(*(one(insight) for insight in todo))
    return result
