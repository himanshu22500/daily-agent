"""Capture insights from Claude Code transcripts.

Lane 1 — the **marker lane** (this issue): a user message containing a configurable
marker (default ``insight:``) is captured **verbatim** as a high-signal insight.
The LLM **extraction lane** (#61) plugs in alongside ``extract_marked`` without
touching the reader or the store.

``collect_marked`` ties the reader + store together: per transcript file it reads
only the lines after the stored watermark, captures any marked insights, and
advances the watermark — so re-runs are cheap and exact-key dedup collapses repeats.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..models import Insight
from .insights_store import InsightStore
from .transcripts import TranscriptMessage, parse_line, read_lines, session_files


def canonical_key(text: str) -> str:
    """Stable key from normalized text, so the same insight dedups exactly."""
    norm = " ".join(text.lower().split())
    return "insight:" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


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
