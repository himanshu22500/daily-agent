"""Resurface captured insights through the existing outbox."""

from __future__ import annotations

import re

from ..models import Bite, Insight
from .channels import stream_for
from .insights_store import InsightStore
from .outbox import Outbox, OutboxItem

INSIGHT_KIND = "insight"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _label(value: str) -> str:
    return " ".join(part.capitalize() for part in _slug(value).split("-") if part)


def insight_subject(key: str) -> str:
    return key if key.startswith("insight:") else f"insight:{key}"


def render_insight(insight: Insight) -> str:
    lines = [insight.text]
    meta: list[str] = []
    if insight.type:
        meta.append(f"type: {insight.type}")
    if insight.tags:
        meta.append("tags: " + ", ".join(insight.tags))
    if insight.git_branch:
        meta.append(f"branch: {insight.git_branch}")
    if meta:
        lines.extend(["", " | ".join(meta)])
    return "\n".join(lines)


def insight_to_bite(insight: Insight) -> Bite:
    subject = insight_subject(insight.key)
    return Bite(
        dedup_key=subject,
        subject=subject,
        kind=INSIGHT_KIND,
        content=render_insight(insight),
    )


def enqueue_new_insights(store: InsightStore, outbox: Outbox) -> int:
    """Queue new insights as bites; return how many were newly enqueued."""
    queued = 0
    for insight in store.by_status("new"):
        if outbox.enqueue(insight_to_bite(insight)):
            queued += 1
            store.set_status(insight.key, "queued")
    return queued


def insight_stream_resolver(store: InsightStore):
    """Build a MultiStreamTelegramChannel resolver keyed by insight type."""

    def _resolve(item: OutboxItem) -> tuple[str, str]:
        if item.kind != INSIGHT_KIND:
            return stream_for(item)
        insight = store.get(item.subject)
        insight_type = _slug(insight.type if insight else "") or "general"
        label = _label(insight_type) or "General"
        return f"insight:{insight_type}", f"daily-agent · Insights · {label}"

    return _resolve
