"""Initiative resolver — map a PR to the initiative storyline it belongs to.

This is the deterministic backbone of the rich feed (see ROADMAP "Rich content —
the initiative-storyline model"). Given a PR and the set of GitHub Project issues
(each carrying its sub-issue parent chain and the PRs that close it, from
``sources/github_projects.py``), it answers: *which initiative is this work part of?*

The logic, grounded in the real ``fcbtech`` Project #86:
- An issue sits in a native **sub-issue tree whose root is the initiative**
  ("Tranzact Agents [Phase 1]", "Document Create Schemas (v2.5) [Phase-2]", …).
  We walk to the top of the chain to find it. (There are no QA/"Test ||" scaffolding
  buckets to skip — QA is a project *field* now, not a parent issue.)
- A PR links to its issue natively via ``closedByPullRequestsReferences``; we invert
  that into a ``"<repo>#<number>" -> issue`` map, so the link is **deterministic**
  (no ticket-id text matching, which on the old Huly board covered only ~17%).
- Three lanes: ``initiative`` (the real efforts), ``ops`` (the weekly
  "Project OnCall [dates]" rotation + incidents — a single quiet stream),
  ``untracked`` (PRs that close no tracked issue; an LLM best-effort maps these onto
  the catalog later).

Identity is the issue's ``<repo>#<number>`` (e.g. ``pm#56``), stable forever, so an
initiative's storyline stays attached across runs. This layer is pure/deterministic
and LLM-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import PullRequest

# Operational nodes → the quiet "ops" lane: the weekly "Project OnCall [dates]"
# rotation and incidents. Deliberately NOT a bare "on-?call" — that would swallow
# real product initiatives like "CX - Oncall Helper ChatBot".
_OPS_RE = re.compile(r"project\s*on-?call|incident", re.IGNORECASE)

OPS_KEY = "ops"
UNTRACKED_KEY = "untracked"


@dataclass(frozen=True)
class Initiative:
    """The storyline subject a piece of work belongs to.

    ``key`` is the stable identity chapters attach to (a ``<repo>#<number>``
    identifier for real initiatives, or the synthetic ``ops`` / ``untracked`` lane
    keys).
    """

    lane: str  # "initiative" | "ops" | "untracked"
    key: str
    title: str

    @property
    def subject(self) -> str:
        """Subject string for the feed/outbox, e.g. 'initiative:pm#56'."""
        return f"{self.lane}:{self.key}"


def is_ops(title: str | None) -> bool:
    """An operational node (weekly oncall rotation / incident) → the ops lane."""
    return bool(title and _OPS_RE.search(title))


def _chain(issue: dict) -> list[dict]:
    """The issue itself first, then its ancestors (immediate parent → root)."""
    self_node = {
        "identifier": issue.get("identifier"),
        "title": issue.get("title") or "",
    }
    return [self_node, *issue.get("parents", [])]


def resolve_initiative(issue: dict | None) -> Initiative:
    """Map a GitHub Project issue (with its parent chain) to its initiative.

    ``None`` (the PR closes no tracked issue) → the untracked lane.
    """
    if issue is None:
        return Initiative(lane="untracked", key=UNTRACKED_KEY, title="Untracked work")

    chain = _chain(issue)

    # Any oncall/incident node anywhere in the chain → the single quiet ops lane.
    if any(is_ops(n.get("title")) for n in chain):
        return Initiative(lane="ops", key=OPS_KEY, title="Ops & Oncall")

    # The initiative is the root of the sub-issue tree (topmost ancestor).
    chosen = chain[-1]
    key = chosen.get("identifier")
    if not key:
        # No identifier to anchor on (shouldn't happen for board issues) → untracked.
        return Initiative(lane="untracked", key=UNTRACKED_KEY, title="Untracked work")
    return Initiative(lane="initiative", key=key, title=chosen.get("title") or key)


def index_by_linked_pr(issues: list[dict]) -> dict[str, dict]:
    """Invert each issue's ``linked_prs`` into a ``"<repo>#<number>" -> issue`` map.

    This is the deterministic PR→issue link (native ``closedByPullRequestsReferences``).
    Keys match ``initiative_mapper.pr_key`` (``f"{pr.repo}#{pr.number}"``, bare repo).
    If two issues list the same closing PR, the first one wins (rare).
    """
    out: dict[str, dict] = {}
    for issue in issues:
        for lp in issue.get("linked_prs", []):
            out.setdefault(f"{lp['repo']}#{lp['number']}", issue)
    return out


def initiative_for_pr(pr: PullRequest, pr_to_issue: dict[str, dict]) -> Initiative:
    """Resolve the initiative a PR belongs to (untracked if it closes no issue)."""
    return resolve_initiative(pr_to_issue.get(f"{pr.repo}#{pr.number}"))
