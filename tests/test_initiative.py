"""Initiative resolver — chain-walking, lane routing, linked-PR mapping (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.feed.initiative import (
    index_by_linked_pr,
    initiative_for_pr,
    is_ops,
    resolve_initiative,
)
from daily_agent.models import PullRequest


# --- helpers --------------------------------------------------------------- #
def _issue(identifier, title, parents=None, prs=None) -> dict:
    """A GitHub-Projects-shaped issue dict.

    ``parents`` = [(identifier, title), ...] immediate parent first → root last.
    ``prs`` = [(repo, number), ...] the PRs that close this issue.
    """
    return {
        "identifier": identifier,
        "title": title,
        "parents": [{"identifier": i, "title": t} for i, t in (parents or [])],
        "linked_prs": [{"repo": r, "number": n} for r, n in (prs or [])],
    }


def _pr(repo: str, number: int, title: str = "") -> PullRequest:
    now = datetime.now(timezone.utc)
    return PullRequest(
        repo=repo,
        number=number,
        title=title,
        author="alice",
        state="closed",
        merged=True,
        created_at=now,
        merged_at=now,
        url=f"http://x/{number}",
    )


# --- ops detection --------------------------------------------------------- #
def test_ops_detection_matches_oncall_rotation_and_incidents():
    assert is_ops("Project OnCall [29nd Jun '26 - 5th July '26]")
    assert is_ops("Project Oncall [25th May]")
    assert is_ops("Production incident: webhook outage")


def test_ops_detection_does_not_swallow_oncall_product():
    # A real product initiative — must NOT be routed to ops just for "Oncall".
    assert not is_ops("CX - Oncall Helper ChatBot")
    assert not is_ops("Document Create Schemas (v2.5) [Phase-2]")
    assert not is_ops(None)


# --- resolution ------------------------------------------------------------ #
def test_root_of_chain_is_the_initiative():
    # A leaf issue -> its sub-issue parent -> the root (the initiative).
    issue = _issue(
        "pm#86",
        "AI Chat Widget — stacked PR rollout",
        [("pm#56", "Tranzact Agents [Phase 1]")],
    )
    init = resolve_initiative(issue)
    assert init.lane == "initiative"
    assert init.key == "pm#56"
    assert init.title == "Tranzact Agents [Phase 1]"
    assert init.subject == "initiative:pm#56"


def test_deeper_chain_anchors_on_the_topmost_ancestor():
    issue = _issue(
        "pm#200",
        "leaf",
        [("pm#100", "mid"), ("pm#16", "Document Create Schemas (v2.5) [Phase-2]")],
    )
    init = resolve_initiative(issue)
    assert init.key == "pm#16"


def test_standalone_issue_is_its_own_initiative():
    issue = _issue("pm#105", "Fix currency conversion in doc create.")  # no parents
    init = resolve_initiative(issue)
    assert init.lane == "initiative"
    assert init.key == "pm#105"


def test_oncall_anywhere_in_chain_routes_to_ops_lane():
    issue = _issue(
        "pm#120",
        "Fix flaky job",
        [("pm#112", "Project OnCall [29nd Jun '26 - 5th July '26]")],
    )
    init = resolve_initiative(issue)
    assert init.lane == "ops"
    assert init.key == "ops"


def test_no_issue_is_untracked():
    init = resolve_initiative(None)
    assert init.lane == "untracked"
    assert init.key == "untracked"
    assert init.subject == "untracked:untracked"


# --- PR mapping (via inverted linked-PR graph) ----------------------------- #
def test_index_by_linked_pr_inverts_the_graph_with_bare_repos():
    issues = [
        _issue("pm#56", "Tranzact Agents [Phase 1]", prs=[("tz-vue-3", 1596)]),
        _issue("pm#33", "Document Create API", prs=[("tranzact-v2", 5639)]),
    ]
    idx = index_by_linked_pr(issues)
    assert set(idx) == {"tz-vue-3#1596", "tranzact-v2#5639"}
    assert idx["tz-vue-3#1596"]["identifier"] == "pm#56"


def test_pr_resolves_through_its_linked_issue():
    issues = [
        _issue(
            "pm#86",
            "AI Chat Widget",
            [("pm#56", "Tranzact Agents [Phase 1]")],
            prs=[("tz-vue-3", 1596)],
        )
    ]
    idx = index_by_linked_pr(issues)
    init = initiative_for_pr(_pr("tz-vue-3", 1596, "feat(chat): panel"), idx)
    assert init.key == "pm#56"  # resolves to the root initiative


def test_pr_closing_no_tracked_issue_is_untracked():
    init = initiative_for_pr(_pr("api", 9, "quick fix"), {})
    assert init.lane == "untracked"


def test_first_issue_wins_when_a_pr_closes_two():
    issues = [
        _issue("pm#1", "First", prs=[("api", 7)]),
        _issue("pm#2", "Second", prs=[("api", 7)]),
    ]
    idx = index_by_linked_pr(issues)
    assert idx["api#7"]["identifier"] == "pm#1"
