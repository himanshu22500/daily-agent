"""Initiative resolver — chain-walking, bucket-skipping, lane routing (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.feed.initiative import (
    extract_ticket,
    index_issues,
    initiative_for_pr,
    is_ops,
    is_process_bucket,
    resolve_initiative,
)
from daily_agent.models import PullRequest


# --- helpers --------------------------------------------------------------- #
def _issue(identifier: str, title: str, parents: list[tuple[str, str]] | None = None) -> dict:
    """Build a bridge-shaped issue dict. parents = [(identifier, title), ...]
    immediate parent first → root last."""
    return {
        "identifier": identifier,
        "title": title,
        "parents": [{"identifier": i, "id": f"hid-{i}", "title": t} for i, t in (parents or [])],
    }


def _pr(title: str, body: str = "") -> PullRequest:
    now = datetime.now(timezone.utc)
    return PullRequest(
        repo="tranzact-v2", number=1, title=title, author="alice", state="closed",
        merged=True, created_at=now, merged_at=now, url="http://x/1", body=body,
    )


# --- ticket extraction ----------------------------------------------------- #
def test_extract_ticket_from_title_and_body():
    assert extract_ticket("ENG-16326 add thing") == "ENG-16326"
    assert extract_ticket("no ticket", "fixes eng-42 here") == "ENG-42"
    assert extract_ticket("nothing", "") is None


def test_extract_ticket_first_wins_and_uppercases():
    assert extract_ticket("eng-7 and ENG-9") == "ENG-7"


# --- bucket / ops detection ------------------------------------------------ #
def test_process_bucket_detection():
    assert is_process_bucket("Perform QA Testing")
    assert is_process_bucket("Perform QA Testing | Item Details v3")
    assert is_process_bucket("Test || Stock Valuation")
    assert not is_process_bucket("TZ-Agents Phase 2")
    assert not is_process_bucket(None)


def test_ops_detection():
    assert is_ops("Project Oncall [1st Jun '26 - 7th Jun '26]")
    assert is_ops("Project OnCall [25th May]")
    assert is_ops("Production incident: webhook outage")
    assert not is_ops("Item Details v3")


# --- resolution ------------------------------------------------------------ #
def test_topmost_non_bucket_is_the_initiative():
    # ENG-16970 "Usecase7 Skill" -> Use Case 7 -> TZ-Agents Phase 2 (root).
    issue = _issue("ENG-16970", "TZ-Agents Usecase7 Skill",
                   [("ENG-16938", "Use Case 7: Invite Users"), ("ENG-16326", "TZ-Agents Phase 2")])
    init = resolve_initiative(issue)
    assert init.lane == "initiative"
    assert init.key == "ENG-16326"
    assert init.title == "TZ-Agents Phase 2"
    assert init.subject == "initiative:ENG-16326"


def test_qa_bucket_in_chain_is_skipped():
    # ENG-16974 "Test || ..." -> "Perform QA Testing" (bucket) -> "TS - 80".
    issue = _issue("ENG-16974", "Test || Stock Valuation",
                   [("ENG-16966", "Perform QA Testing"), ("ENG-16442", "TS - 80")])
    init = resolve_initiative(issue)
    assert init.lane == "initiative"
    assert init.key == "ENG-16442"  # skipped both the Test|| self-node and the QA parent


def test_oncall_routes_to_ops_lane():
    issue = _issue("ENG-16951", "Fix flaky job",
                   [("ENG-16903", "Project Oncall [1st Jun '26 - 7th Jun '26]")])
    init = resolve_initiative(issue)
    assert init.lane == "ops"
    assert init.key == "ops"


def test_standalone_issue_is_its_own_initiative():
    issue = _issue("ENG-16999", "Report generation times out")  # no parents
    init = resolve_initiative(issue)
    assert init.lane == "initiative"
    assert init.key == "ENG-16999"


def test_all_bucket_chain_falls_back_to_root():
    issue = _issue("ENG-1", "Test || x", [("ENG-2", "Perform QA Testing")])
    init = resolve_initiative(issue)
    # No non-bucket node exists; fall back to the root rather than dropping it.
    assert init.key == "ENG-2"


def test_no_issue_is_untracked():
    init = resolve_initiative(None)
    assert init.lane == "untracked"
    assert init.key == "untracked"
    assert init.subject == "untracked:untracked"


# --- PR mapping ------------------------------------------------------------ #
def test_pr_with_ticket_resolves_through_index():
    issues = [_issue("ENG-16326", "TZ-Agents Phase 2")]
    idx = index_issues(issues)
    init = initiative_for_pr(_pr("ENG-16326 wire up skill"), idx)
    assert init.key == "ENG-16326"


def test_pr_without_ticket_is_untracked():
    init = initiative_for_pr(_pr("quick fix, no ticket"), {})
    assert init.lane == "untracked"


def test_pr_with_unknown_ticket_is_untracked():
    # References a ticket we don't have in the index (e.g. older than our window).
    init = initiative_for_pr(_pr("ENG-99999 mystery"), index_issues([_issue("ENG-1", "x")]))
    assert init.lane == "untracked"
