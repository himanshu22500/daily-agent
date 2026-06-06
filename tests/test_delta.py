"""Delta engine: activity -> stable, deduped bites (no network, no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.feed.delta import bites_for_activity
from daily_agent.feed.outbox import Outbox
from daily_agent.models import PullRequest, RepoActivity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pr(number: int, *, merged: bool) -> PullRequest:
    now = _now()
    return PullRequest(
        repo="api", number=number, title=f"Feature {number}", author="alice",
        state="closed" if merged else "open", merged=merged,
        created_at=now, merged_at=now if merged else None,
        url=f"http://x/{number}", additions=10, deletions=2, changed_files=1,
    )


def test_open_pr_yields_one_bite():
    bites = bites_for_activity([RepoActivity(repo="api", pull_requests=[_pr(1, merged=False)])])
    assert [b.kind for b in bites] == ["pr_opened"]
    assert bites[0].dedup_key == "pr:api#1@opened"
    assert bites[0].subject == "repo:api"


def test_merged_pr_yields_opened_and_merged_bites():
    bites = bites_for_activity([RepoActivity(repo="api", pull_requests=[_pr(2, merged=True)])])
    assert {b.dedup_key for b in bites} == {"pr:api#2@opened", "pr:api#2@merged"}


def test_keys_are_stable_across_runs():
    act = [RepoActivity(repo="api", pull_requests=[_pr(3, merged=True)])]
    assert {b.dedup_key for b in bites_for_activity(act)} == {
        b.dedup_key for b in bites_for_activity(act)
    }


def test_pr_seen_open_then_merged_delivers_twice_never_repeats(tmp_path):
    """The lifecycle a real PR goes through: opened, later merged."""
    ob = Outbox(tmp_path / "f.db")

    # First pass: PR is still open.
    open_act = [RepoActivity(repo="api", pull_requests=[_pr(4, merged=False)])]
    assert ob.enqueue_all(bites_for_activity(open_act)) == 1
    assert ob.drain(_collector := _Collector()).sent == 1

    # Second pass: same PR, now merged. Only the new @merged bite is delivered;
    # the already-delivered @opened bite is not repeated.
    merged_act = [RepoActivity(repo="api", pull_requests=[_pr(4, merged=True)])]
    assert ob.enqueue_all(bites_for_activity(merged_act)) == 1
    assert ob.drain(_collector).sent == 1
    assert len(_collector.sent) == 2
    assert {i.kind for i in _collector.sent} == {"pr_opened", "pr_merged"}


class _Collector:
    name = "collector"

    def __init__(self) -> None:
        self.sent = []

    def send(self, item) -> None:
        self.sent.append(item)
