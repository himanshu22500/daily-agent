"""Read Claude Code session transcripts (``~/.claude/projects/<proj>/*.jsonl``).

Each line is one JSON record. We surface the conversational text — user-typed
messages and assistant replies — as lightweight ``TranscriptMessage`` objects for
the insight-capture lanes. Everything else (tool calls, tool results, metadata
records like ``ai-title``/``file-history-snapshot``) is skipped.

A *user-typed* message is ``type == "user"`` with a **string** ``message.content``;
tool-result records are also ``type == "user"`` but carry an **array** content, so
the string check cleanly excludes them. Assistant text is the concatenation of the
``text`` blocks in its content array (``thinking``/``tool_use`` blocks dropped).

Pure functions over strings/paths so they're trivially testable with fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptMessage:
    session_id: str
    git_branch: str
    timestamp: str
    role: str  # "user" | "assistant"
    text: str


def _record_text(record: dict) -> tuple[str, str] | None:
    """Return ``(role, text)`` for a conversational record, else ``None``."""
    kind = record.get("type")
    content = (record.get("message") or {}).get("content")
    if kind == "user":
        # Typed prompt = plain string. Array content = tool results -> skip.
        text = content if isinstance(content, str) else None
    elif kind == "assistant":
        blocks = content if isinstance(content, list) else []
        text = " ".join(
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ).strip()
    else:
        text = None
    if not text:
        return None
    return kind, text


def parse_line(line: str) -> TranscriptMessage | None:
    """Parse one JSONL line into a ``TranscriptMessage`` (None if not conversational)."""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    parsed = _record_text(record)
    if parsed is None:
        return None
    role, text = parsed
    return TranscriptMessage(
        session_id=record.get("sessionId", ""),
        git_branch=record.get("gitBranch", ""),
        timestamp=record.get("timestamp", ""),
        role=role,
        text=text,
    )


def session_files(transcripts_dir: str | Path) -> list[Path]:
    """All transcript files in a project's dir, oldest-named first."""
    d = Path(transcripts_dir)
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def read_lines(path: Path) -> list[str]:
    """All lines of a transcript file (tolerant of undecodable bytes)."""
    return path.read_text(errors="replace").splitlines()
