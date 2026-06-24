"""Initiative mapper agent — assign ticket-less PRs onto the Huly catalog.

Only ~17% of PRs cite an `ENG-` ticket; the rest use conventional-commit scopes
(`feat(comm-v3)`, `feat(inventory-v3)`) that line up with real initiatives. This
agent reads those scopes/titles/bodies and assigns each PR to one of the known
initiatives — or "untracked" if none fits. It is constrained to the catalog: it
must NOT invent initiatives (that would drift identity and break storylines).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from ..feed.initiative import UNTRACKED_KEY, Initiative
from ..models import PullRequest
from .model import build_model, cache_settings


class _Assignment(BaseModel):
    pr: str = Field(description="The PR key, copied exactly as given (repo#number).")
    initiative: str = Field(
        description="An initiative key from the catalog, or 'untracked' if none fits."
    )


class _Mapping(BaseModel):
    assignments: list[_Assignment] = Field(default_factory=list)


_SYSTEM_PROMPT = """\
You route engineering pull requests to the initiative each one belongs to, for an
awareness feed. You are given a CATALOG of known initiatives (key + name) and a
list of PRs (key + title + branch + description). PR titles and branch names use
conventional-commit scopes like feat(inventory-v3) or refactor/documents_v3 —
these scopes are your best signal for which initiative a PR serves.

Rules:
- For each PR, output its key and the initiative key it belongs to.
- Choose ONLY from the catalog keys, or the literal string "untracked".
- Use "untracked" when no catalog initiative is a clear fit. Do not force a match,
  and never invent an initiative key that isn't in the catalog.
- Map every PR exactly once. Copy PR keys verbatim.
"""


def _build(model: str) -> Agent[None, _Mapping]:
    return Agent(
        build_model(model),
        output_type=_Mapping,
        system_prompt=_SYSTEM_PROMPT,
        model_settings=cache_settings(model),
    )


def pr_key(pr: PullRequest) -> str:
    return f"{pr.repo}#{pr.number}"


def _render(prs: list[PullRequest], catalog: list[Initiative]) -> str:
    lines = ["CATALOG (choose by key):"]
    for c in catalog:
        lines.append(f"  {c.key} — {c.title}")
    lines.append("")
    lines.append("PRs to assign:")
    for pr in prs:
        lines.append(f"  {pr_key(pr)} — {pr.title}")
        if pr.head_ref_name:
            lines.append(f"      branch: {pr.head_ref_name}")
        body = " ".join((pr.body or "").split())[:200]
        if body:
            lines.append(f"      description: {body}")
    lines.append("")
    lines.append("Assign every PR to a catalog key or 'untracked'.")
    return "\n".join(lines)


async def map_orphans(
    model: str, prs: list[PullRequest], catalog: list[Initiative]
) -> dict[str, str]:
    """Return {pr_key: initiative_key|'untracked'} for the given PRs.

    No PRs or an empty catalog → everything untracked (nothing to map onto).
    """
    if not prs or not catalog:
        return {pr_key(pr): UNTRACKED_KEY for pr in prs}
    agent = _build(model)
    result = await agent.run(_render(prs, catalog))
    return {a.pr: a.initiative for a in result.output.assignments}
