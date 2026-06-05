"""Person brief agent — synthesize what one person is working on.

Turns a person's recent GitHub PRs and Huly tasks into a short, readable
briefing (themes and intent), instead of a raw list of links.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from ..models import PullRequest
from .model import build_model, cache_settings


class PersonBrief(BaseModel):
    headline: str = Field(description="One line: the person's main focus right now.")
    summary: str = Field(
        description="2-4 sentences on what they're working on and why it matters."
    )
    themes: list[str] = Field(
        default_factory=list,
        description="Distinct workstreams, each a short phrase with the gist.",
    )


_SYSTEM_PROMPT = """\
You summarize what a single engineer is currently working on, for a busy leader.

Given their recent GitHub PRs (titles + descriptions) and Huly tasks, infer the
THEMES and intent of their work — what they're building or fixing and why — not
a list of PRs. Group related PRs into workstreams. Be concise and concrete.

Some people don't use the task tracker (e.g. founders); if Huly tasks are
absent, rely on the PRs and don't remark on the absence.
"""


def build_person_brief_agent(model: str) -> Agent[None, PersonBrief]:
    return Agent(
        build_model(model), output_type=PersonBrief, system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def _render(name: str, prs: list[PullRequest], tasks: list[dict]) -> str:
    lines = [f"Person: {name}", ""]
    if tasks:
        lines.append("Huly tasks:")
        for t in tasks:
            lines.append(f"  - {t['identifier']} [{t['status']}] {t['title']}")
        lines.append("")
    lines.append("GitHub PRs:")
    for pr in prs:
        state = "merged" if pr.merged else pr.state
        lines.append(f"  - {pr.repo}#{pr.number} [{state}] {pr.title}")
        body = " ".join((pr.body or "").split())[:300]
        if body:
            lines.append(f"      {body}")
    return "\n".join(lines)


async def summarize_person(
    model: str, name: str, prs: list[PullRequest], tasks: list[dict]
) -> PersonBrief:
    agent = build_person_brief_agent(model)
    result = await agent.run(_render(name, prs, tasks))
    return result.output
