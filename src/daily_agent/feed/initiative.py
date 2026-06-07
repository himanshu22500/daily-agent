"""Initiative resolver — map a PR to the initiative storyline it belongs to.

This is the deterministic backbone of the rich feed (see ROADMAP "Rich content —
the initiative-storyline model"). Given a PR and the set of Huly issues (each
carrying its pre-resolved parent chain from the bridge), it answers: *which
initiative is this work part of?*

The logic, grounded in the real `ENG` workspace:
- A PR references a Huly ticket via ``ENG-<n>`` in its title/body.
- That ticket sits in a multi-level parent tree whose nodes are a mix of real
  initiatives and **process buckets** ("Perform QA Testing", "Test || …") and
  **ops buckets** ("Project Oncall [dates]"). We walk the chain and pick the
  *topmost non-bucket* node — that's the initiative — which naturally skips a
  QA bucket sitting between a task and its real parent, and collapses the team's
  mirror ``Test || <feature>`` tasks into the feature itself.
- Three lanes: ``initiative`` (the real efforts), ``ops`` (oncall/incidents — a
  single quiet stream), ``untracked`` (PRs with no resolvable ticket, incl. devs
  who don't use Huly).

Identity is the Huly identifier (``ENG-<n>``, stable forever) so an initiative's
storyline stays attached across runs. This layer is pure/deterministic and
LLM-free; a later **normalizer** refines cryptic names and edge classifications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import PullRequest

_TICKET_RE = re.compile(r"\b(ENG-\d+)\b", re.IGNORECASE)

# Titles that are NOT initiatives — work scaffolding the team hangs tasks under.
_BUCKET_RE = re.compile(
    r"^\s*(?:perform\s+qa\s+testing|qa\s+testing|test\s*\|\||test\s*:)",
    re.IGNORECASE,
)
# Operational buckets → the quiet "ops" lane (weekly oncall, incidents).
_OPS_RE = re.compile(r"(?:project\s*)?on-?call|incident", re.IGNORECASE)

OPS_KEY = "ops"
UNTRACKED_KEY = "untracked"


@dataclass(frozen=True)
class Initiative:
    """The storyline subject a piece of work belongs to.

    ``key`` is the stable identity chapters attach to (an ``ENG-<n>`` identifier
    for real initiatives, or the synthetic ``ops`` / ``untracked`` lane keys).
    """

    lane: str  # "initiative" | "ops" | "untracked"
    key: str
    title: str

    @property
    def subject(self) -> str:
        """Subject string for the feed/outbox, e.g. 'initiative:ENG-16326'."""
        return f"{self.lane}:{self.key}"


def extract_ticket(*texts: str) -> str | None:
    """Return the first ``ENG-<n>`` found across the given texts, upper-cased."""
    for text in texts:
        if not text:
            continue
        m = _TICKET_RE.search(text)
        if m:
            return m.group(1).upper()
    return None


def is_process_bucket(title: str | None) -> bool:
    """A QA/test scaffolding node — never an initiative on its own."""
    return bool(title and _BUCKET_RE.search(title))


def is_ops(title: str | None) -> bool:
    """An operational node (oncall / incident) — routes to the ops lane."""
    return bool(title and _OPS_RE.search(title))


def _chain(issue: dict) -> list[dict]:
    """The issue itself first, then its ancestors (immediate parent → root)."""
    self_node = {
        "identifier": issue.get("identifier"),
        "title": issue.get("title") or "",
    }
    return [self_node, *issue.get("parents", [])]


def resolve_initiative(issue: dict | None) -> Initiative:
    """Map a Huly issue (with its parent chain) to its initiative.

    ``None`` (no ticket / ticket not found) → the untracked lane.
    """
    if issue is None:
        return Initiative(lane="untracked", key=UNTRACKED_KEY, title="Untracked work")

    chain = _chain(issue)

    # Any oncall/incident node anywhere in the chain → the single quiet ops lane.
    if any(is_ops(n.get("title")) for n in chain):
        return Initiative(lane="ops", key=OPS_KEY, title="Ops & Oncall")

    # The initiative is the topmost (closest to root) node that isn't a bucket.
    non_bucket = [n for n in chain if not is_process_bucket(n.get("title"))]
    chosen = non_bucket[-1] if non_bucket else chain[-1]
    key = chosen.get("identifier")
    if not key:
        # No identifier to anchor on (shouldn't happen for ENG issues) → untracked.
        return Initiative(lane="untracked", key=UNTRACKED_KEY, title="Untracked work")
    return Initiative(lane="initiative", key=key, title=chosen.get("title") or key)


def index_issues(issues: list[dict]) -> dict[str, dict]:
    """Index issue dicts by their ``ENG-<n>`` identifier for fast lookup."""
    return {i["identifier"]: i for i in issues if i.get("identifier")}


def initiative_for_pr(pr: PullRequest, issues_by_id: dict[str, dict]) -> Initiative:
    """Resolve the initiative a PR belongs to (untracked if no ticket link)."""
    ticket = extract_ticket(pr.title, pr.body)
    issue = issues_by_id.get(ticket) if ticket else None
    return resolve_initiative(issue)
