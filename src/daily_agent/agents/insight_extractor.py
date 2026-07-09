"""LLM extraction lane for the personal insight feed.

The marker lane captures explicit `insight:` notes verbatim. This agent is the
best-effort complementary lane: it scans transcript messages for durable lessons
that were not manually marked.
"""

from __future__ import annotations

from pydantic_ai import Agent

from ..feed.transcripts import TranscriptMessage
from ..models import InsightCandidate, InsightExtraction
from .model import build_model, cache_settings

_SYSTEM_PROMPT = """\
You extract durable engineering insights from Claude Code pairing transcripts.

Return only candidates that are likely to be useful weeks later:
- repo or architecture facts that are not obvious from filenames alone
- gotchas, footguns, constraints, and non-obvious debugging lessons
- reusable implementation or testing techniques

Do NOT emit transient chatter, step-by-step debugging, command output summaries,
status updates, todos, vague preferences, or anything useful only for the current
moment. If there are no durable insights, return an empty candidates list.

For each candidate:
- `text`: concise, standalone, plain language
- `canonical_key`: stable lower-kebab semantic identity for exact-key dedup
- `type`: one of repo, technique, gotcha, architecture, or general
- `tags`: short lower-kebab tags
- `score`: 0.0 to 1.0, based on durability, non-obviousness, and reuse value
"""


def build_insight_extractor(model: str) -> Agent[None, InsightExtraction]:
    return Agent(
        build_model(model),
        output_type=InsightExtraction,
        system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def _clean_text(text: str, *, limit: int = 1800) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _render(messages: list[TranscriptMessage]) -> str:
    lines = [
        "Extract durable insights from these transcript messages.",
        "Ignore explicit marker-lane notes if they only restate an `insight:` marker.",
        "",
        "MESSAGES:",
    ]
    for idx, msg in enumerate(messages, start=1):
        branch = msg.git_branch or "-"
        timestamp = msg.timestamp or "-"
        lines.append(f"[{idx}] {timestamp} | {msg.role} | branch={branch}")
        lines.append(_clean_text(msg.text))
        lines.append("")
    return "\n".join(lines).strip()


async def extract_insights(
    model: str, messages: list[TranscriptMessage]
) -> list[InsightCandidate]:
    if not messages:
        return []
    agent = build_insight_extractor(model)
    result = await agent.run(_render(messages))
    return result.output.candidates
