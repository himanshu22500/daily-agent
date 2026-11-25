"""LLM extraction lane for the personal insight feed.

The marker lane captures explicit `insight:` notes verbatim. This agent is the
best-effort complementary lane: it scans transcript messages for durable lessons
that were not manually marked.

The same call recognises a session spent studying for interviews and writes
practice cards from it instead (issue #139): ``study_session`` and
``prep_cards`` in the reply. ``study=True`` (the session ran in a study folder)
tells the model, and makes it a study session whatever the model says.
"""

from __future__ import annotations

from pydantic_ai import Agent

from ..feed.practice_cards import CARD_GUIDE, MAX_STUDY_CARDS, card_list_schema
from ..feed.transcripts import TranscriptMessage
from ..models import InsightExtraction
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

The request may include READER FEEDBACK: items the reader rated in their feed.
Calibrate to it. Skip candidates like the "not useful" ones (same kind, same
level, same project habits), favour ones like the "useful" ones, and hold
sessions from projects the reader wants fewer insights from to a higher bar.
"""
# Interview study (issue #139): the same call writes cards instead.
_SYSTEM_PROMPT += f"""
INTERVIEW STUDY. The reader is preparing for interviews and sometimes studies
with Claude: works through an interview question, has a mock interview, or
learns DSA patterns or system design concepts for interviews. Set
`study_session` to true only for such a session, and always when the request
says SESSION: interview study. Building, debugging or designing real software
is work, not study, even when it touches the same topics; in a work session
leave `study_session` false and `prep_cards` empty.

A study session yields practice cards, not insights: leave `candidates` empty
and write up to {MAX_STUDY_CARDS} `prep_cards` on what was studied. The cards must be
correct even where the conversation wasn't (the reader may have answered
wrongly in a mock interview). When the session worked through a whole system
design, add one walkthrough card: `question` is the design prompt as an
interviewer would pose it, `answer` is "", and `steps` follow the design in
the order the session worked it (e.g. Hello Interview's Delivery Framework:
Requirements, Core entities, API, High-level design, Deep dives), each body
the key decisions and trade-offs in a few short lines. Other cards leave
`steps` empty.

{CARD_GUIDE}"""
STUDY_LINE = "SESSION: interview study (the reader ran it in a study folder)"


def extraction_schema() -> dict:
    """The reply's schema: ``InsightExtraction``, with the card limits."""
    schema = InsightExtraction.model_json_schema()
    schema["properties"]["prep_cards"] = card_list_schema(MAX_STUDY_CARDS, steps=True)
    return schema


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


def _render(
    messages: list[TranscriptMessage],
    *,
    feedback: str = "",
    project: str = "",
    study: bool = False,
) -> str:
    lines = [
        "Extract durable insights from these transcript messages.",
        "Ignore explicit marker-lane notes if they only restate an `insight:` marker.",
        "",
    ]
    if project:
        lines += [f"PROJECT: {project}", ""]
    if study:
        lines += [STUDY_LINE, ""]
    if feedback:
        lines += ["READER FEEDBACK:", feedback.strip(), ""]
    lines.append("MESSAGES:")
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
    model: str,
    messages: list[TranscriptMessage],
    *,
    feedback: str = "",
    project: str = "",
    study: bool = False,
) -> InsightExtraction:
    """Durable insights in ``messages``, or practice cards from a study session.

    ``feedback`` is the reader's ratings note (``feed.ratings``) and ``project``
    the session's project, so the model can calibrate to both (issue #126).
    ``study``: the session ran in a study folder (issue #139).
    """
    if not messages:
        return InsightExtraction(study_session=study)
    prompt = _render(messages, feedback=feedback, project=project, study=study)
    if is_claude_code(model):
        result = await run_claude(
            prompt,
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            json_schema=extraction_schema(),
        )
        try:
            found = InsightExtraction.model_validate(result.structured)
        except ValueError as exc:
            raise ClaudeCodeError(f"unexpected extraction shape: {exc}") from exc
    else:
        agent = build_insight_extractor(model)
        found = (await run_with_backoff(agent, prompt, attempts=ATTEMPTS)).output
    if study:
        found.study_session = True
    return found
