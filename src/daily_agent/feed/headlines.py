"""Headlines for the Discover-style feed (issue #82).

Headlines are written outside capture (no model call while a session is being
read): the timed sweep or ``titles-generate`` sends untitled insights in batches
to Claude Code (issue #95), and ``titles-export``/``titles-import`` still let a
batch of Claude subagents write them by hand. Everything is stored through
``apply_titles``, which cleans and validates it. Until an insight has a
headline, the feed shows its first sentence.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .insights_store import InsightStore

MAX_HEADLINE = 90
FALLBACK_HEADLINE = 80

# Only unambiguous markdown: backticks and **bold**. Single `_`/`*` are left
# alone, since they are part of identifiers (snake_case, QA_*, __init__).
_MARKUP = re.compile(r"`+|\*\*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(])")


def plain(text: str) -> str:
    """Drop inline markdown markers and collapse whitespace."""
    return " ".join(_MARKUP.sub("", text or "").split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


_QUOTES = "\"'“”‘’"


def clean_headline(raw: str) -> str:
    """Normalize a written headline (quotes, markup, trailing period, length).

    Quotes are removed only when they wrap the whole headline, so one that
    ends with a quoted phrase (``posts a "Power user PSA"``) keeps it.
    """
    title = plain(raw).strip()
    inner = title[1:-1]
    if (
        len(title) > 2
        and title[0] in _QUOTES
        and title[-1] in _QUOTES
        and not any(q in inner for q in _QUOTES if q != "'")
    ):
        title = inner.strip()
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


# batch of {key, text, type, tags} records -> {key: headline}
HeadlineWriter = Callable[[list[dict]], Awaitable[dict[str, str]]]


@dataclass
class HeadlineRun:
    written: int = 0
    failed: list[str] = field(default_factory=list)


async def generate_headlines(
    store: InsightStore,
    write: HeadlineWriter,
    *,
    limit: int | None = None,
    batch_size: int = 25,
    on_progress: Callable[[], None] | None = None,
) -> HeadlineRun:
    """Write headlines for untitled insights, newest first, in batches.

    A failed batch is reported and left untitled for the next run; it never
    stops the other batches. Existing headlines are never replaced.
    """
    records = [
        {"key": i.key, "text": i.text, "type": i.type, "tags": i.tags}
        for i in store.missing_titles(limit)
    ]
    run = HeadlineRun()
    for start in range(0, len(records), max(1, batch_size)):
        if on_progress is not None:
            on_progress()
        try:
            titles = await write(records[start : start + batch_size])
        except Exception as exc:  # noqa: BLE001 - one batch never stops the rest
            run.failed.append((str(exc).splitlines() or [type(exc).__name__])[0][:200])
            continue
        run.written += apply_titles(store, titles).applied
    return run
