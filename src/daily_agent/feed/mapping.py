"""Resolve every PR to an initiative — deterministic anchors + LLM mapping.

The hybrid the data demands: tickets cover only ~17% of PRs, so we anchor those
(and ops) deterministically, then let the LLM assign the ticket-less majority
onto the Huly-derived catalog. Anything the LLM can't place stays untracked —
identity is never invented.
"""

from __future__ import annotations

from ..agents.initiative_mapper import map_orphans, pr_key
from ..cache import Cache
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


def _cache_key(pr: PullRequest) -> str:
    """Namespaced cache key for a PR's LLM-mapped initiative assignment."""
    return f"initiative-map:{pr_key(pr)}"


async def resolve_initiatives(
    model: str,
    prs: list[PullRequest],
    issues: list[dict],
    *,
    cache: Cache | None = None,
) -> dict[str, Initiative]:
    """Map each PR (by ``repo#number``) to its initiative.

    Deterministic first: PRs with a resolvable ticket (or oncall) are anchored
    without the LLM. The rest are mapped onto the catalog by the LLM; unmatched
    ones fall to the untracked lane.

    A PR's initiative assignment is stable, so LLM results for orphan PRs are
    cached permanently in the SQLite ``cache`` (keyed by ``repo#number``) when a
    ``cache`` is supplied. Already-mapped orphans skip the LLM on later runs; only
    cache-misses are sent to ``map_orphans``.
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

    # The LLM-mapped initiative key per orphan, from cache hits then fresh calls.
    keys: dict[str, str] = {}
    misses: list[PullRequest] = []
    for pr in orphans:
        cached = cache.get(_cache_key(pr), ttl=None) if cache else None
        if cached is not None:
            keys[pr_key(pr)] = cached
        else:
            misses.append(pr)

    # Only cache-misses go to the mapper; a fully warm run skips the LLM entirely.
    if misses:
        fresh = await map_orphans(model, misses, catalog)
        for pr in misses:
            chosen = fresh.get(pr_key(pr), UNTRACKED_KEY)
            keys[pr_key(pr)] = chosen
            if cache:
                cache.set(_cache_key(pr), chosen, permanent=True)

    for pr in orphans:
        chosen = keys.get(pr_key(pr))
        # A cached key absent from the current catalog → treat as untracked.
        resolved[pr_key(pr)] = by_key.get(chosen) if chosen else None
        if resolved[pr_key(pr)] is None:
            resolved[pr_key(pr)] = _untracked()
    return resolved
