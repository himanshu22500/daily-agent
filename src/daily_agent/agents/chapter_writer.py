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
            "The message to deliver: AT MOST 2 sentences (~45 words). Sentence 1: "
            "what shipped. Sentence 2: what it is in the product, plainly. No "
            "preamble, no status, no risks, no advice."
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

Hard rules for the chapter text:
- AT MOST 2 SENTENCES, ~45 words total. This is a glanceable bite, not a report.
  Be ruthless: one sentence on what shipped, one on what it is in the product.
- NO preamble ("This update…", "The team…"), NO lists, NO PR numbers.
- LEAD WITH WHAT SHIPPED (merged work). Ignore in-flight work unless nothing
  shipped.
- TRANSLATE tech into plain product terms — what it actually is/does for the
  product. If it's internal plumbing, say what it sets up, briefly.
- DESCRIBE, never judge: no status, no risk/watch flags, no recommendations.
- CONTINUE THE STORY: if a prior summary is given, only add what's NEW; never
  repeat what was already told.

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


# --------------------------------------------------------------------------- #
# Untracked lane — terse itemized digest, not a forced narrative
# --------------------------------------------------------------------------- #
class ItemizedDigest(BaseModel):
    items: list[str] = Field(
        description=(
            "One short plain-language line per shipped change — what it is in the "
            "product. No PR numbers, no prefixes, no grouping into prose."
        )
    )


_ITEMS_SYSTEM_PROMPT = """\
You summarize a set of UNRELATED engineering changes that shipped but aren't tied
to any initiative, for a busy observer who just wants to not miss what went into
the product.

Return one short line PER change (merged PRs first): a plain-language note of what
it is in the product — translate the technical title, drop jargon and PR numbers.
Keep each line under ~12 words. Do NOT weave them into a paragraph; they're
unrelated. Skip purely trivial chores (formatting, lint) unless nothing else
shipped.
"""


def _build_items(model: str) -> Agent[None, ItemizedDigest]:
    return Agent(
        build_model(model), output_type=ItemizedDigest, system_prompt=_ITEMS_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def _render_items(prs: list[PullRequest]) -> str:
    merged = [p for p in prs if p.merged]
    open_ = [p for p in prs if not p.merged]
    lines = ["Shipped (merged):"]
    for p in merged:
        lines.append(f"  - {p.repo}#{p.number} {p.title}")
    if open_:
        lines.append("In flight:")
        for p in open_:
            lines.append(f"  - {p.repo}#{p.number} {p.title}")
    return "\n".join(lines)


async def write_untracked_items(model: str, prs: list[PullRequest]) -> list[str]:
    agent = _build_items(model)
    result = await agent.run(_render_items(prs))
    return result.output.items
