"""The delta engine — turns accumulated activity into bite-sized deltas.

This is the step that makes the difference between a *feed* and a *digest*: it
emits only discrete, individually-identifiable facts, each with a stable
``dedup_key``, and lets the outbox decide what's actually new (via its
``UNIQUE`` constraint + delivered ledger). Re-running over the same activity
produces the same keys, so nothing is ever delivered twice.

Phase 1 emits PR-level bites — a PR opening and a PR merging are two distinct,
high-signal events with naturally stable keys. Commit-level noise and
per-person / LLM-narrated rollups are deliberately left for later phases; this
layer stays pure, deterministic, and LLM-free so dedup is easy to verify.
"""

from __future__ import annotations

from ..models import Bite, PullRequest, RepoActivity


def _pr_opened_bite(pr: PullRequest) -> Bite:
    return Bite(
        dedup_key=f"pr:{pr.repo}#{pr.number}@opened",
        subject=f"repo:{pr.repo}",
        kind="pr_opened",
        content=(
            f"PR opened in {pr.repo}: #{pr.number} {pr.title} (by {pr.author})\n"
            f"{pr.url}"
        ),
    )


def _pr_merged_bite(pr: PullRequest) -> Bite:
    churn = f"+{pr.additions}/-{pr.deletions} across {pr.changed_files} files"
    return Bite(
        dedup_key=f"pr:{pr.repo}#{pr.number}@merged",
        subject=f"repo:{pr.repo}",
        kind="pr_merged",
        content=(
            f"PR merged in {pr.repo}: #{pr.number} {pr.title} (by {pr.author}) "
            f"— {churn}\n{pr.url}"
        ),
    )


def bites_for_activity(activities: list[RepoActivity]) -> list[Bite]:
    """Produce all candidate bites for a batch of repo activity.

    A merged PR yields both an ``@opened`` and an ``@merged`` bite (two real
    moments in its life); the outbox dedups each independently, so a PR seen
    first open then merged delivers two messages over time, never repeats either.
    """
    bites: list[Bite] = []
    for act in activities:
        for pr in act.pull_requests:
            bites.append(_pr_opened_bite(pr))
            if pr.merged:
                bites.append(_pr_merged_bite(pr))
    return bites
