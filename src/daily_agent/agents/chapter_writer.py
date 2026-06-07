"""Chapter writer — turn an initiative's new activity into a feed chapter.

This is the renderer at the heart of the awareness feed. Given an initiative,
its prior story-state, and the PRs that just landed, it writes the next *chapter*:
a short, plain-language account of what shipped and *what it actually is in the
product* — and an updated story-state to remember for next time.

Tone is set deliberately (see ROADMAP "Rich content"): the reader is a busy
SILENT OBSERVER, not a decision-maker. So the chapter describes, it does not
judge — no health/status, no risk/watch flags, no calls-to-action. The whole
value is translating technical git activity into product understanding.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from ..models import PullRequest
from .model import build_model, cache_settings


class Chapter(BaseModel):
    chapter: str = Field(
        description=(
            "The message to deliver: 2-4 sentences, plain language. Lead with what "
            "shipped, then explain what it actually is in the product. No status "
            "ratings, no risks, no advice."
        )
    )
    story_state: str = Field(
        description=(
            "An updated running summary of this whole initiative (a few sentences) "
            "to remember, so the next chapter only adds what's new. Internal note, "
            "not delivered."
        )
    )


_SYSTEM_PROMPT = """\
You write a single chapter of an ongoing, plain-language feed about ONE
engineering initiative, for a busy leader who is a SILENT OBSERVER. Their goal:
understand what's going into the product so they're never blindsided — not to
make decisions.

Write the chapter so it:
- LEADS WITH WHAT SHIPPED (merged PRs). Mention in-flight work only briefly, if
  at all.
- TRANSLATES the technical changes into plain product terms — what this actually
  is / does in the product. This is the whole point. Avoid jargon; if a PR is
  internal plumbing, say what it sets up in user/product terms.
- Is SHORT and scannable: 2-4 sentences. It's a bite, not a report.
- DESCRIBES, never judges: no health/status labels, no risk or "watch" flags, no
  recommendations or calls-to-action.
- CONTINUES THE STORY: if a prior summary is given, only add what's NEW since
  then; don't repeat what was already told.

Also return an updated `story_state`: a brief running summary of the whole
initiative so far (for your own memory next time), reflecting this chapter.
"""


def _build(model: str) -> Agent[None, Chapter]:
    return Agent(
        build_model(model), output_type=Chapter, system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def _render(title: str, prior_state: str | None, prs: list[PullRequest]) -> str:
    lines = [f"Initiative: {title}", ""]
    if prior_state:
        lines += ["Story so far (already told — only add what's new):", prior_state, ""]
    else:
        lines += ["(No prior chapter — this is the first time we cover this initiative.)", ""]
    merged = [p for p in prs if p.merged]
    open_ = [p for p in prs if not p.merged]
    if merged:
        lines.append("Shipped (merged) this period:")
        for p in merged:
            lines.append(f"  - {p.repo}#{p.number} {p.title} (by {p.author})")
            body = " ".join((p.body or "").split())[:300]
            if body:
                lines.append(f"      {body}")
    if open_:
        lines.append("In flight (still open):")
        for p in open_:
            lines.append(f"  - {p.repo}#{p.number} {p.title} (by {p.author})")
    return "\n".join(lines)


async def write_chapter(
    model: str, *, title: str, prior_state: str | None, prs: list[PullRequest]
) -> Chapter:
    agent = _build(model)
    result = await agent.run(_render(title, prior_state, prs))
    return result.output
