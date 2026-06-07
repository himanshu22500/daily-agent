"""Storage round-trip + dedup/upsert tests (no network, no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daily_agent.models import Commit, PullRequest, RepoActivity
from daily_agent.storage import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _activity(repo: str = "api") -> RepoActivity:
    now = _now()
    return RepoActivity(
        repo=repo,
        pull_requests=[
            PullRequest(
                repo=repo,
                number=1,
                title="Add billing",
                author="alice",
                state="closed",
                merged=True,
                created_at=now,
                merged_at=now,
                url="http://x/1",
            )
        ],
        commits=[
            Commit(
                repo=repo,
                sha="abc123",
                author="alice",
                message="init",
                date=now,
                url="http://x/c/abc123",
            )
        ],
    )


def test_save_and_read_back(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_activity(_activity())
    out = store.activity_since(_now() - timedelta(days=1))
    assert len(out) == 1
    assert out[0].repo == "api"
    assert out[0].pull_requests[0].title == "Add billing"
    assert out[0].commits[0].sha == "abc123"


def test_upsert_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_activity(_activity())
    store.save_activity(_activity())  # same PR + commit again
    out = store.activity_since(_now() - timedelta(days=1))
    assert len(out[0].pull_requests) == 1
    assert len(out[0].commits) == 1


def test_pr_state_updates_on_reupsert(tmp_path):
    store = Store(tmp_path / "t.db")
    now = _now()
    open_pr = RepoActivity(
        repo="web",
        pull_requests=[
            PullRequest(
                repo="web",
                number=5,
                title="WIP",
                author="bob",
                state="open",
                merged=False,
                created_at=now,
                url="http://x/5",
            )
        ],
    )
    store.save_activity(open_pr)
    merged = open_pr.model_copy(deep=True)
    merged.pull_requests[0].state = "closed"
    merged.pull_requests[0].merged = True
    merged.pull_requests[0].merged_at = now
    store.save_activity(merged)
    out = store.activity_since(now - timedelta(days=1))
    pr = out[0].pull_requests[0]
    assert pr.merged is True
    assert pr.state == "closed"
