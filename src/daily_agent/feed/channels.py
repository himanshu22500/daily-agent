"""Delivery channels.

A channel turns a queued :class:`~daily_agent.feed.outbox.OutboxItem` into an
actual delivery. It must raise on failure (so the outbox retries) and return
normally on success (so the outbox commits the delivery).

Phase 1 ships two channel-agnostic channels — a console printer and a file
appender — so the whole outbox/dedup pipeline is exercised end-to-end before any
Slack credentials exist. Slack lands in Phase 2 as just another ``Channel``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .outbox import OutboxItem


class ConsoleChannel:
    """Prints each bite as a panel — useful for local runs and demos."""

    name = "console"

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, item: OutboxItem) -> None:
        self._console.print(
            Panel(item.content, title=item.subject, subtitle=item.kind, expand=False)
        )


class FileChannel:
    """Appends each bite to a file as a timestamped block.

    A durable, inspectable transcript of the feed with no external dependency —
    handy for verifying dedup across runs.
    """

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, item: OutboxItem) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        block = (
            f"\n--- {stamp} | {item.subject} | {item.kind} ---\n{item.content}\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)
