"""Small, safe text helpers for the web feed templates."""

from __future__ import annotations

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
