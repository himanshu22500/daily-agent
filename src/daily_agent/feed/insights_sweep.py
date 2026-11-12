"""Timed sweep over every project's transcripts (issue #91).

The ``SessionEnd`` hook flushes only the folder of a session that ended, and
sessions run inside an editor over ACP (e.g. Zed) almost never end — the editor
keeps each thread's Claude process alive. The sweep closes that gap: on a timer
it reads new lines from every ``<project>/<session>.jsonl`` that

- was written to since it was last read (the cursor records when reading
  started, so nothing written after that is skipped), and
- has been quiet for a settle window, so a live conversation is read once it
  pauses rather than mid-reply.

It shares the flush's per-file cursors, so a transcript is never read twice
whichever path gets to it first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .insights_capture import (
    CollectResult,
    InsightExtractor,
    collect_file,
    unread_lines,
)
from .insights_store import InsightStore
from .transcripts import TranscriptMessage, all_session_files, parse_line


@dataclass(frozen=True)
class Pending:
    """A transcript written to since it was last read."""

    path: Path
    mtime: float
    settled: bool


def pending_transcripts(
    store: InsightStore,
    projects_dir: str | Path,
    *,
    settle_seconds: float,
    now: float | None = None,
) -> list[Pending]:
    """Transcripts with possibly-unread lines, oldest write first."""
    clock = datetime.now(timezone.utc).timestamp() if now is None else now
    found: list[Pending] = []
    for path in all_session_files(projects_dir):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        read_at = store.last_read_at(str(path))
        if read_at is not None and mtime <= read_at.timestamp():
            continue
        found.append(Pending(path, mtime, settled=clock - mtime >= settle_seconds))
    return sorted(found, key=lambda p: (p.mtime, str(p.path)))


def unread_messages(store: InsightStore, path: Path) -> list[TranscriptMessage]:
    """Conversational messages in ``path`` that capture hasn't read yet."""
    lines, start = unread_lines(store, path)
    return [m for m in (parse_line(raw) for raw in lines[start:]) if m is not None]


@dataclass
class SweepResult:
    captured: CollectResult = field(
        default_factory=lambda: CollectResult(new=0, scanned=0, marked=0, extracted=0)
    )
    read: int = 0  # transcripts with new lines read this run
    waiting: int = 0  # written to within the settle window; read next time
    failed: dict[str, str] = field(default_factory=dict)
    stopped_early: bool = False


async def sweep_insights(
    store: InsightStore,
    projects_dir: str | Path,
    marker: str,
    extractor: InsightExtractor | None,
    *,
    settle_seconds: float,
    max_consecutive_failures: int = 3,
    on_progress: Callable[[], None] | None = None,
    now: float | None = None,
) -> SweepResult:
    """Capture insights from every settled transcript with unread lines.

    One transcript failing (its cursor stays put, so it's retried next sweep)
    never stops the rest, but ``max_consecutive_failures`` in a row ends the
    run early — that's a provider outage, not a bad transcript. ``on_progress``
    runs before each transcript (the CLI uses it to keep its lock fresh).
    """
    pending = pending_transcripts(
        store, projects_dir, settle_seconds=settle_seconds, now=now
    )
    result = SweepResult(waiting=sum(not item.settled for item in pending))
    streak = 0
    for item in pending:
        if not item.settled:
            continue
        if on_progress is not None:
            on_progress()
        try:
            captured = await collect_file(store, item.path, marker, extractor)
        except Exception as exc:  # noqa: BLE001 - one transcript never stops the sweep
            result.failed[str(item.path)] = (
                str(exc).splitlines() or [type(exc).__name__]
            )[0][:200]
            streak += 1
            if streak >= max(1, max_consecutive_failures):
                result.stopped_early = True
                break
            continue
        streak = 0
        if captured.scanned:
            result.read += 1
            result.captured = result.captured + captured
    return result
