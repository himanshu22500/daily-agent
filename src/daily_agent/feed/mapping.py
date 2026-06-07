"""Resolve every PR to an initiative — deterministic anchors + LLM mapping.

The hybrid the data demands: tickets cover only ~17% of PRs, so we anchor those
(and ops) deterministically, then let the LLM assign the ticket-less majority
onto the Huly-derived catalog. Anything the LLM can't place stays untracked —
identity is never invented.
"""

from __future__ import annotations

from ..agents.initiative_mapper import map_orphans, pr_key
from ..models import PullRequest
from .catalog import build_catalog
from .initiative import (
    UNTRACKED_KEY,
    Initiative,
    index_issues,
    initiative_for_pr,
)


def _untracked() -> Initiative:
    return Initiative(lane="untracked", key=UNTRACKED_KEY, title="Untracked work")


async def resolve_initiatives(
    model: str, prs: list[PullRequest], issues: list[dict]
) -> dict[str, Initiative]:
    """Map each PR (by ``repo#number``) to its initiative.

    Deterministic first: PRs with a resolvable ticket (or oncall) are anchored
    without the LLM. The rest are mapped onto the catalog by the LLM; unmatched
    ones fall to the untracked lane.
    """
    idx = index_issues(issues)
    catalog = build_catalog(issues)
    by_key = {c.key: c for c in catalog}

    resolved: dict[str, Initiative] = {}
    orphans: list[PullRequest] = []
    for pr in prs:
        init = initiative_for_pr(pr, idx)
        if init.lane != "untracked":
            resolved[pr_key(pr)] = init  # confident anchor (ticket or ops)
        else:
            orphans.append(pr)

    mapping = await map_orphans(model, orphans, catalog)
    for pr in orphans:
        chosen = mapping.get(pr_key(pr))
        resolved[pr_key(pr)] = by_key.get(chosen) if chosen else None
        if resolved[pr_key(pr)] is None:
            resolved[pr_key(pr)] = _untracked()
    return resolved
