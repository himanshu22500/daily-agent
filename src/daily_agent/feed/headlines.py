"""Headlines for the Discover-style feed (issue #82).

Headlines are written outside the capture pipeline (no model call at capture
time): ``titles-export`` hands insights without one to a writer (e.g. a batch
of Claude subagents), and ``titles-import`` stores the results through
``apply_titles``, which cleans and validates them. Until an insight has a
headline, the feed shows its first sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .insights_store import InsightStore

MAX_HEADLINE = 90
FALLBACK_HEADLINE = 80

_MARKUP = re.compile(r"[`*_]{1,3}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(])")


def plain(text: str) -> str:
    """Drop inline markdown markers and collapse whitespace."""
    return " ".join(_MARKUP.sub("", text or "").split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def clean_headline(raw: str) -> str:
    """Normalize a written headline (quotes, markup, trailing period, length)."""
    title = plain(raw).strip().strip("\"'“”‘’").strip()
    title = title.rstrip(".:;").strip()
    return _clip(title, MAX_HEADLINE)


def split_first_sentence(text: str) -> tuple[str, str]:
    """``(first sentence, the rest)`` of an insight's plain text."""
    flat = plain(text)
    parts = _SENTENCE_END.split(flat, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def fallback_headline(text: str) -> str:
    first, _ = split_first_sentence(text)
    return _clip(first.rstrip("."), FALLBACK_HEADLINE)


def export_for_titling(
    store: InsightStore, *, include_titled: bool = False
) -> list[dict]:
    """Insights to write headlines for, as ``{key, text, type, tags}`` records."""
    insights = store.all() if include_titled else store.missing_titles()
    return [
        {"key": i.key, "text": i.text, "type": i.type, "tags": i.tags}
        for i in sorted(insights, key=lambda i: i.key)
    ]


@dataclass
class TitleImport:
    applied: int = 0
    kept_existing: int = 0
    unknown: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)


def apply_titles(
    store: InsightStore, titles: dict[str, str], *, overwrite: bool = False
) -> TitleImport:
    """Store cleaned ``{key: headline}`` pairs.

    Unknown keys and headlines that clean to nothing are reported, not stored.
    Existing headlines are kept unless ``overwrite`` is set.
    """
    result = TitleImport()
    for key, raw in titles.items():
        insight = store.get(key)
        if insight is None:
            result.unknown.append(key)
            continue
        title = clean_headline(raw if isinstance(raw, str) else "")
        if not title:
            result.empty.append(key)
        elif insight.title and not overwrite:
            result.kept_existing += 1
        elif store.set_title(key, title):
            result.applied += 1
    return result
