"""Small, safe text helpers for the web feed templates."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from markupsafe import Markup, escape

_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_PARAGRAPH = re.compile(r"\n\s*\n")


def _inline(segment: str) -> str:
    """Escape one non-code segment and apply bold."""
    return _BOLD.sub(r"<strong>\1</strong>", str(escape(segment)))


def inline_markdown(text: str) -> Markup:
    """Render the tiny markdown subset insights use: paragraphs, `code`, **bold**.

    Everything is HTML-escaped first; only the tags generated here are emitted,
    so insight text (which is model output) can never inject markup.
    """
    paragraphs = []
    for block in _PARAGRAPH.split(text.strip()):
        parts = _CODE.split(block)
        # re.split with one group alternates: text, code, text, code, ...
        html = "".join(
            f"<code>{escape(part)}</code>" if i % 2 else _inline(part)
            for i, part in enumerate(parts)
        )
        paragraphs.append("<p>" + html.replace("\n", "<br>") + "</p>")
    return Markup("".join(paragraphs))


def label(value: str) -> str:
    """``"some-type"`` -> ``"Some type"`` for display."""
    words = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()
    return " ".join(words).capitalize() or "General"


def type_class(value: str) -> str:
    """CSS-safe class suffix for an insight type."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "general"


def ago(moment: datetime, now: datetime | None = None) -> str:
    """Compact relative time: ``just now``, ``5m``, ``3h``, ``yesterday``, ``Oct 30``."""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (current - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 2 * 86400:
        return "yesterday"
    local = moment.astimezone()
    if local.year == current.astimezone().year:
        return f"{local:%b} {local.day}"
    return f"{local:%b} {local.day}, {local.year}"


def until(moment: datetime, now: datetime | None = None) -> str:
    """Compact time-until: ``in 40m``, ``in 5h``, ``tomorrow``, ``in 3 days``."""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (moment - current).total_seconds()
    if seconds < 60:
        return "any moment"
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    if seconds < 86400:
        return f"in {int(seconds // 3600)}h"
    if seconds < 2 * 86400:
        return "tomorrow"
    return f"in {int(seconds // 86400)} days"


# Base hue per insight type for generated covers; other types hash to a hue.
_TYPE_HUES = {"gotcha": 18, "repo": 215, "architecture": 265, "technique": 158}


def _hash(value: str) -> int:
    return int(hashlib.sha1(value.encode()).hexdigest()[:12], 16)


def cover_style(key: str, type: str) -> str:
    """CSS custom properties for a deterministic, per-insight generated cover.

    Hue family comes from the type; the exact hues, angle, and highlight
    position vary by key, so neighbouring cards of one type still differ.
    """
    h = _hash(key)
    base = _TYPE_HUES.get(type_class(type), _hash(type or "general") % 360)
    hue1 = (base + h % 36 - 18) % 360
    hue2 = (hue1 + 20 + (h >> 6) % 45) % 360
    angle = (h >> 12) % 180
    x, y = 12 + (h >> 20) % 76, 10 + (h >> 28) % 60
    return f"--h1:{hue1};--h2:{hue2};--angle:{angle}deg;--x:{x}%;--y:{y}%"


def source_hue(source: str) -> int:
    """Stable hue for a project's little 'publisher' badge."""
    return _hash(source or "?") % 360


def longdate(moment: datetime) -> str:
    """``November 11, 2026, 5:38 PM`` in local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local:%B} {local.day}, {local.year}, {hour}:{local:%M} {meridiem}"


def ago_long(moment: datetime, now: datetime | None = None) -> str:
    """Spelled-out relative time: ``38 mins ago``, ``3 hours ago``, ``Oct 30``."""
    short = ago(moment, now)
    if short.endswith("m") and short[:-1].isdigit():
        n = int(short[:-1])
        return f"{n} min{'s' if n != 1 else ''} ago"
    if short.endswith("h") and short[:-1].isdigit():
        n = int(short[:-1])
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return short
