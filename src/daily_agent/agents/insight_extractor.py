"""LLM extraction lane for the personal insight feed.

The marker lane captures explicit `insight:` notes verbatim. This agent is the
best-effort complementary lane: it scans transcript messages for durable lessons
that were not manually marked.
"""

from __future__ import annotations

from pydantic_ai import Agent

from ..feed.transcripts import TranscriptMessage
from ..models import InsightCandidate, InsightExtraction
from .claude_code import ClaudeCodeError, is_claude_code, run_claude
from .model import build_model, cache_settings, run_with_backoff

# Unattended runs (the hook flush, the timed sweep) must never hang on a slow
# provider; 3 attempts x 120s also keeps one transcript well inside the
# 10-minute flush lock.
REQUEST_TIMEOUT_SECONDS = 120
ATTEMPTS = 3

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
        model_settings={
            **(cache_settings(model) or {}),
            "timeout": REQUEST_TIMEOUT_SECONDS,
        },
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


def prompt_chars(messages: list[TranscriptMessage]) -> int:
    """Size of the extraction prompt for ``messages`` (for cost estimates)."""
    return len(_SYSTEM_PROMPT) + len(_render(messages)) if messages else 0


async def extract_insights(
    model: str, messages: list[TranscriptMessage]
) -> list[InsightCandidate]:
    if not messages:
        return []
    if is_claude_code(model):
        result = await run_claude(
            _render(messages),
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            json_schema=InsightExtraction.model_json_schema(),
        )
        try:
            return InsightExtraction.model_validate(result.structured).candidates
        except ValueError as exc:
            raise ClaudeCodeError(f"unexpected extraction shape: {exc}") from exc
    agent = build_insight_extractor(model)
    result = await run_with_backoff(agent, _render(messages), attempts=ATTEMPTS)
    return result.output.candidates
