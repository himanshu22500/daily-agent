"""Illustrated covers for the web feed (issue #87).

A cover is one SVG file per insight, named after the insight key
(``insight:foo-bar`` -> ``foo-bar.svg``), kept in a local, gitignored
directory. The files are written outside the pipeline (by illustrator
subagents), so nothing is trusted: ``validate_cover`` only accepts a small,
static, self-contained subset of SVG, and ``import_covers`` copies in only
files that pass. The web app additionally serves them with a locked-down CSP
and shows them through ``<img>``, where SVG scripts never run.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

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
    """File stem for an insight key, or ``None`` if it isn't path-safe."""
    slug = key.removeprefix("insight:")
    return slug if _SLUG.fullmatch(slug) else None


def cover_path(covers_dir: str | Path, key: str) -> Path | None:
    slug = cover_slug(key)
    return Path(covers_dir) / f"{slug}.svg" if slug else None


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


@dataclass
class CoverImport:
    imported: int = 0
    rejected: dict[str, list[str]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)


def import_covers(
    source_dir: str | Path, covers_dir: str | Path, known_keys: set[str]
) -> CoverImport:
    """Copy valid ``<slug>.svg`` files for known insights into ``covers_dir``.

    Files for unknown insights are skipped; invalid ones are reported with
    their reasons. Existing covers are overwritten by a valid replacement.
    """
    slugs = {cover_slug(key) for key in known_keys} - {None}
    destination = Path(covers_dir)
    destination.mkdir(parents=True, exist_ok=True)
    result = CoverImport()
    for path in sorted(Path(source_dir).glob("*.svg")):
        if path.stem not in slugs:
            result.unknown.append(path.name)
            continue
        data = path.read_bytes()
        problems = validate_cover(data)
        if problems:
            result.rejected[path.name] = problems
            continue
        (destination / path.name).write_bytes(data)
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
    covers_dir: str | Path,
    draw: CoverDrawer,
    *,
    limit: int | None = None,
    concurrency: int = 4,
    tries: int = 2,
    overwrite: bool = False,
) -> CoverGeneration:
    """Draw covers for insights without one; store only those that validate.

    A rejected drawing is retried with the validator's reasons as feedback, up
    to ``tries`` drawings per insight. A failing insight never stops the rest.
    """
    destination = Path(covers_dir)
    destination.mkdir(parents=True, exist_ok=True)
    todo = [
        insight
        for insight in sorted(store.all(), key=lambda i: i.key)
        if (path := cover_path(destination, insight.key)) is not None
        and (overwrite or not path.exists())
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
                data = svg.encode()
                feedback = validate_cover(data) if svg else ["reply contained no <svg>"]
                if not feedback:
                    cover_path(destination, insight.key).write_bytes(data)
                    result.written += 1
                    result.failed.pop(insight.key, None)
                    return
            result.failed[insight.key] = feedback

    await asyncio.gather(*(one(insight) for insight in todo))
    return result
