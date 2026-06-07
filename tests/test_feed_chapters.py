"""Incremental chapter→bite pipeline: only-what's-new + story-state (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed import storyteller as st_mod
from daily_agent.feed.initiative import Initiative
from daily_agent.feed.initiatives_store import InitiativeStore
from daily_agent.feed.storyteller import (
    _chapter_dedup_key,
    _new_since,
    chapters_to_bites,
)
from daily_agent.agents.chapter_writer import Chapter
from daily_agent.models import PullRequest


def _pr(num, *, merged=True, days_ago=2):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return PullRequest(repo="api", number=num, title=f"t{num}", author="a", state="closed",
                       merged=merged, created_at=ts, merged_at=ts if merged else None,
                       url=f"http://x/{num}", body="")


# --- helpers --------------------------------------------------------------- #
def test_new_since_filters_by_activity():
    old = _pr(1, days_ago=5)
    new = _pr(2, days_ago=1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    assert _new_since([old, new], cutoff) == [new]
    assert _new_since([old, new], None) == [old, new]


def test_dedup_key_stable_for_same_set_changes_with_new_pr():
    a = _chapter_dedup_key("ENG-1", [_pr(1), _pr(2)])
    b = _chapter_dedup_key("ENG-1", [_pr(2), _pr(1)])   # order-independent
    c = _chapter_dedup_key("ENG-1", [_pr(1), _pr(2), _pr(3)])
    assert a == b
    assert a != c


# --- pipeline -------------------------------------------------------------- #
def _patch(monkeypatch, mapping):
    async def fake_resolve(model, prs, issues):
        return mapping

    async def fake_write(model, *, title, prior_state, prs):
        return Chapter(chapter=f"{title}: {len(prs)} new", story_state=f"state@{len(prs)}")

    monkeypatch.setattr(st_mod, "resolve_initiatives", fake_resolve)
    monkeypatch.setattr(st_mod, "write_chapter", fake_write)


@pytest.mark.asyncio
async def test_first_run_emits_bites_and_records_state(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="ENG-1", title="Billing")
    _patch(monkeypatch, {"api#1": init, "api#2": init})
    store = InitiativeStore(tmp_path / "i.db")

    bites = await chapters_to_bites("m", [_pr(1), _pr(2)], [], store)
    assert len(bites) == 1
    assert bites[0].subject == "initiative:ENG-1"
    assert bites[0].kind == "chapter"
    assert "2 merged" in bites[0].content
    # Story-state advanced + persisted.
    st = store.get("ENG-1")
    assert st.story_state == "state@2" and st.last_narrated_at is not None


@pytest.mark.asyncio
async def test_second_run_no_new_activity_emits_nothing(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="ENG-1", title="Billing")
    _patch(monkeypatch, {"api#1": init})
    store = InitiativeStore(tmp_path / "i.db")

    first = await chapters_to_bites("m", [_pr(1)], [], store)
    assert len(first) == 1
    # Same PRs again — all older than last_narrated → nothing new.
    second = await chapters_to_bites("m", [_pr(1)], [], store)
    assert second == []


@pytest.mark.asyncio
async def test_untracked_and_initiative_are_separate_subjects(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="ENG-1", title="Billing")
    untr = Initiative(lane="untracked", key="untracked", title="Untracked work")
    _patch(monkeypatch, {"api#1": init, "api#2": untr})
    store = InitiativeStore(tmp_path / "i.db")

    bites = await chapters_to_bites("m", [_pr(1), _pr(2)], [], store)
    subjects = {b.subject for b in bites}
    assert subjects == {"initiative:ENG-1", "untracked:untracked"}
