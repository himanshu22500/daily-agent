"""Incremental chapter→bite pipeline: only-what's-new + story-state (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.agents.chapter_writer import Chapter
from daily_agent.feed import storyteller as st_mod
from daily_agent.feed.initiative import Initiative
from daily_agent.feed.initiatives_store import InitiativeStore
from daily_agent.feed.outbox import MAX_ATTEMPTS, Outbox, OutboxItem
from daily_agent.feed.storyteller import (
    _chapter_dedup_key,
    _new_since,
    chapters_to_bites,
)
from daily_agent.models import PullRequest


def _pr(num, *, merged=True, days_ago=2):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return PullRequest(
        repo="api",
        number=num,
        title=f"t{num}",
        author="a",
        state="closed",
        merged=merged,
        created_at=ts,
        merged_at=ts if merged else None,
        url=f"http://x/{num}",
        body="",
    )


# --- helpers --------------------------------------------------------------- #
def test_new_since_filters_by_activity():
    old = _pr(1, days_ago=5)
    new = _pr(2, days_ago=1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    assert _new_since([old, new], cutoff) == [new]
    assert _new_since([old, new], None) == [old, new]


def test_dedup_key_stable_for_same_set_changes_with_new_pr():
    a = _chapter_dedup_key("pm#1", [_pr(1), _pr(2)])
    b = _chapter_dedup_key("pm#1", [_pr(2), _pr(1)])  # order-independent
    c = _chapter_dedup_key("pm#1", [_pr(1), _pr(2), _pr(3)])
    assert a == b
    assert a != c


# --- pipeline -------------------------------------------------------------- #
def _patch(monkeypatch, mapping):
    async def fake_resolve(model, prs, issues, *, cache=None):
        return mapping

    async def fake_write(model, *, title, prior_state, prs):
        return Chapter(
            chapter=f"{title}: {len(prs)} new", story_state=f"state@{len(prs)}"
        )

    async def fake_items(model, prs):
        return [f"item {p.repo}#{p.number}" for p in prs]

    monkeypatch.setattr(st_mod, "resolve_initiatives", fake_resolve)
    monkeypatch.setattr(st_mod, "write_chapter", fake_write)
    monkeypatch.setattr(st_mod, "write_untracked_items", fake_items)


class _Collector:
    name = "collector"

    def __init__(self) -> None:
        self.sent: list[OutboxItem] = []

    def send(self, item: OutboxItem) -> None:
        self.sent.append(item)


class _Flaky:
    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def send(self, item: OutboxItem) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_first_run_emits_bites_without_recording_state(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    _patch(monkeypatch, {"api#1": init, "api#2": init})
    store = InitiativeStore(tmp_path / "i.db")

    bites = await chapters_to_bites("m", [_pr(1), _pr(2)], [], store)
    assert len(bites) == 1
    assert bites[0].subject == "initiative:pm#1"
    assert bites[0].kind == "chapter"
    assert "2 merged" in bites[0].content
    assert bites[0].story_state_update is not None
    assert bites[0].story_state_update.initiative_key == "pm#1"
    assert bites[0].story_state_update.story_state == "state@2"
    st = store.get("pm#1")
    assert st.story_state is None and st.last_narrated_at is None


@pytest.mark.asyncio
async def test_delivery_success_records_state_and_stops_reruns(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    _patch(monkeypatch, {"api#1": init})
    db = tmp_path / "feed.db"
    store = InitiativeStore(db)
    outbox = Outbox(db)

    bites = await chapters_to_bites("m", [_pr(1)], [], store)
    assert outbox.enqueue_all(bites) == 1
    assert outbox.drain(_Collector()).sent == 1

    st = store.get("pm#1")
    assert st.story_state == "state@1" and st.last_narrated_at is not None

    second = await chapters_to_bites("m", [_pr(1)], [], store)
    assert second == []


@pytest.mark.asyncio
async def test_failed_delivery_does_not_record_state_then_success_does(
    tmp_path, monkeypatch
):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    _patch(monkeypatch, {"api#1": init})
    db = tmp_path / "feed.db"
    store = InitiativeStore(db)
    outbox = Outbox(db)
    now = datetime.now(timezone.utc)

    assert outbox.enqueue_all(await chapters_to_bites("m", [_pr(1)], [], store)) == 1
    flaky = _Flaky(fail_times=1)
    assert outbox.drain(flaky, now=now).failed == 1
    assert store.get("pm#1").story_state is None

    assert outbox.drain(flaky, now=now + timedelta(minutes=10)).sent == 1
    assert store.get("pm#1").story_state == "state@1"


@pytest.mark.asyncio
async def test_dead_chapter_does_not_record_state_and_prs_remain_eligible(
    tmp_path, monkeypatch
):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    _patch(monkeypatch, {"api#1": init, "api#2": init})
    db = tmp_path / "feed.db"
    store = InitiativeStore(db)
    outbox = Outbox(db)
    now = datetime.now(timezone.utc)

    assert outbox.enqueue_all(await chapters_to_bites("m", [_pr(1)], [], store)) == 1
    flaky = _Flaky(fail_times=MAX_ATTEMPTS)
    for i in range(MAX_ATTEMPTS):
        outbox.drain(flaky, now=now + timedelta(hours=i))

    assert outbox.stats()["dead"] == 1
    assert store.get("pm#1").story_state is None

    next_bites = await chapters_to_bites("m", [_pr(1), _pr(2)], [], store)
    assert len(next_bites) == 1
    assert "2 merged" in next_bites[0].content
    assert outbox.enqueue_all(next_bites) == 1

    assert outbox.drain(_Collector(), now=now + timedelta(days=1)).sent == 1
    assert store.get("pm#1").story_state == "state@2"


@pytest.mark.asyncio
async def test_rerun_before_delivery_is_idempotent(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    _patch(monkeypatch, {"api#1": init})
    db = tmp_path / "feed.db"
    store = InitiativeStore(db)
    outbox = Outbox(db)

    first = await chapters_to_bites("m", [_pr(1)], [], store)
    second = await chapters_to_bites("m", [_pr(1)], [], store)

    assert [b.dedup_key for b in first] == [b.dedup_key for b in second]
    assert outbox.enqueue_all(first) == 1
    assert outbox.enqueue_all(second) == 0
    assert store.get("pm#1").story_state is None


@pytest.mark.asyncio
async def test_untracked_and_initiative_are_separate_subjects(tmp_path, monkeypatch):
    init = Initiative(lane="initiative", key="pm#1", title="Billing")
    untr = Initiative(lane="untracked", key="untracked", title="Untracked work")
    _patch(monkeypatch, {"api#1": init, "api#2": untr})
    store = InitiativeStore(tmp_path / "i.db")

    bites = await chapters_to_bites("m", [_pr(1), _pr(2)], [], store)
    subjects = {b.subject for b in bites}
    assert subjects == {"initiative:pm#1", "untracked:untracked"}
