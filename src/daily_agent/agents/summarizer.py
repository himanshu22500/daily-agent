"""Cross-project summarizer agent.

Takes the raw activity gathered from sources and produces a structured
``ActivityDigest`` the user can read to know what's being worked on org-wide.
"""

from __future__ import annotations

from pydantic_ai import Agent

from ..models import ActivityDigest, RepoActivity
from .model import build_model, cache_settings

_SYSTEM_PROMPT = """\
You are an engineering-activity analyst for a software organization. You are
given recent activity (pull requests and commits) across many repositories.

Your job: tell a busy leader, in plain language, what is actually being worked
on. Focus on INTENT and OUTCOMES, not raw git noise. Group by project/repo.

Guidelines:
- Infer the *purpose* of changes from PR titles/bodies and commit messages
  (e.g. "shipping X feature", "hardening auth", "paying down Y tech debt").
- Highlight merged work (it landed) over still-open work, but mention notable
  in-flight PRs.
- Name the people driving each project.
- Be concise and concrete. No filler. If activity is trivial, say so briefly.
- The `overview` should synthesize themes across projects, not just list them.
"""


def build_summarizer(model: str) -> Agent[None, ActivityDigest]:
    return Agent(
        build_model(model),
        output_type=ActivityDigest,
        system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def _render_activity(activities: list[RepoActivity], period: str) -> str:
    lines: list[str] = [f"Activity window: {period}", ""]
    for act in activities:
        if act.is_empty:
            continue
        lines.append(f"## Repo: {act.repo}")
        if act.pull_requests:
            lines.append("Pull requests:")
            for pr in act.pull_requests:
                status = "MERGED" if pr.merged else pr.state.upper()
                lines.append(f"  - #{pr.number} [{status}] {pr.title} (by {pr.author})")
                if pr.body.strip():
                    body = " ".join(pr.body.split())[:400]
                    lines.append(f"      desc: {body}")
        if act.commits:
            lines.append("Commits:")
            for c in act.commits[:40]:
                lines.append(f"  - {c.sha[:7]} {c.message} (by {c.author})")
        lines.append("")
    return "\n".join(lines)


async def summarize(
    model: str, activities: list[RepoActivity], period: str
) -> ActivityDigest:
    non_empty = [a for a in activities if not a.is_empty]
    if not non_empty:
        return ActivityDigest(
            period=period,
            overview="No repository activity in this window.",
            projects=[],
        )
    agent = build_summarizer(model)
    prompt = (
        "Summarize the following engineering activity into a digest.\n\n"
        + _render_activity(non_empty, period)
    )
    result = await agent.run(prompt)
    return result.output
