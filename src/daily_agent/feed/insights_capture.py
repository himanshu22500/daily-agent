"""Capture insights from Claude Code transcripts.

The personal insight feed has two capture lanes over the same transcript reader:

- marker lane: a user message containing the configurable marker (default
  ``insight:``) is captured verbatim as a high-signal insight.
- extraction lane: an injected LLM extractor proposes structured candidates from
  the same new transcript messages.

Both lanes persist through ``InsightStore.add``, so exact-key dedup is shared.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..models import Insight, InsightCandidate
from .insights_store import InsightStore
from .transcripts import TranscriptMessage, parse_line, read_lines, session_files

InsightExtractor = Callable[
    [list[TranscriptMessage]], Awaitable[Iterable[InsightCandidate]]
]


@dataclass(frozen=True)
class CollectResult:
    new: int
    scanned: int
    marked: int
    extracted: int


def canonical_key(text: str) -> str:
    """Stable key from normalized text, so the same insight dedups exactly."""
    norm = " ".join(text.lower().split())
    return "insight:" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _candidate_key(value: str) -> str:
    raw = " ".join(value.split()).lower()
    if raw.startswith("insight:"):
        raw = raw.split(":", 1)[1]
    slug = _slug(raw)
    return f"insight:{slug}" if slug else ""


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _latest_ts(messages: list[TranscriptMessage]) -> datetime:
    for msg in reversed(messages):
        if msg.timestamp:
            return _parse_ts(msg.timestamp)
    return datetime.now(timezone.utc)


def _first_nonempty(values: Iterable[str]) -> str:
    return next((v for v in values if v), "")


def extract_marked(msg: TranscriptMessage, marker: str) -> Insight | None:
    """A verbatim insight if this user message carries the marker, else ``None``.

    Captures the text *after* the first marker occurrence (case-insensitive).
    Marker-lane insights are top-ranked (``score=1.0``) and tagged ``marked``.
    """
    if msg.role != "user" or not marker:
        return None
    idx = msg.text.lower().find(marker.lower())
    if idx < 0:
        return None
    captured = msg.text[idx + len(marker) :].strip()
    if not captured:
        return None
    return Insight(
        key=canonical_key(captured),
        text=captured,
        type="general",
        tags=["marked"],
        score=1.0,
        source_session=msg.session_id,
        git_branch=msg.git_branch,
        captured_at=_parse_ts(msg.timestamp),
        status="new",
    )


def insight_from_candidate(
    candidate: InsightCandidate, messages: list[TranscriptMessage]
) -> Insight | None:
    """Convert a structured LLM candidate into the persisted Insight shape."""
    text = " ".join(candidate.text.split())
    key = _candidate_key(candidate.canonical_key)
    if not text or not key:
        return None
    return Insight(
        key=key,
        text=text,
        type=_slug(candidate.type) or "general",
        tags=[t for t in dict.fromkeys(_slug(t) for t in candidate.tags) if t],
        score=max(0.0, min(1.0, float(candidate.score))),
        source_session=_first_nonempty(m.session_id for m in messages),
        git_branch=_first_nonempty(m.git_branch for m in messages),
        captured_at=_latest_ts(messages),
        status="new",
    )


def collect_marked(
    store: InsightStore, transcripts_dir: str | Path, marker: str
) -> tuple[int, int]:
    """Capture marked insights from new transcript records. Returns ``(new, scanned)``.

    ``new`` = insights actually stored (post-dedup); ``scanned`` = transcript lines
    read this run (the watermark only advances over what was read).
    """
    new = scanned = 0
    for path in session_files(transcripts_dir):
        key = str(path)
        start = store.cursor(key)
        lines = read_lines(path)
        for raw in lines[start:]:
            scanned += 1
            msg = parse_line(raw)
            if msg is None:
                continue
            insight = extract_marked(msg, marker)
            if insight is not None and store.add(insight):
                new += 1
        store.set_cursor(key, len(lines))
    return new, scanned


async def collect_insights(
    store: InsightStore,
    transcripts_dir: str | Path,
    marker: str,
    extractor: InsightExtractor | None,
) -> CollectResult:
    """Capture marker and extraction lanes from new transcript records.

    Returns counts for all newly stored insights plus per-lane breakdowns. The
    cursor advances only after both lanes succeed for a transcript file.
    """
    new = scanned = marked = extracted = 0
    for path in session_files(transcripts_dir):
        key = str(path)
        start = store.cursor(key)
        lines = read_lines(path)
        messages: list[TranscriptMessage] = []
        for raw in lines[start:]:
            scanned += 1
            msg = parse_line(raw)
            if msg is None:
                continue
            messages.append(msg)
            insight = extract_marked(msg, marker)
            if insight is not None and store.add(insight):
                new += 1
                marked += 1
        if extractor is not None and messages:
            for candidate in await extractor(messages):
                insight = insight_from_candidate(candidate, messages)
                if insight is not None and store.add(insight):
                    new += 1
                    extracted += 1
        store.set_cursor(key, len(lines))
    return CollectResult(new=new, scanned=scanned, marked=marked, extracted=extracted)
