"""Initiative story-state store + chapter rendering orchestration (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_agent.agents.chapter_writer import Chapter
from daily_agent.feed import storyteller as st_mod
from daily_agent.feed.initiative import Initiative
from daily_agent.feed.initiatives_store import InitiativeStore
from daily_agent.feed.storyteller import group_by_initiative, render_chapters
from daily_agent.models import PullRequest


def _pr(repo, num, merged=True):
    now = datetime.now(timezone.utc)
    return PullRequest(repo=repo, number=num, title=f"t{num}", author="a", state="closed",
                       merged=merged, created_at=now, merged_at=now if merged else None,
                       url=f"http://x/{num}", body="")


# --- store ----------------------------------------------------------------- #
def test_store_upsert_and_record_chapter(tmp_path):
    s = InitiativeStore(tmp_path / "i.db")
    assert s.get("ENG-1") is None
    s.upsert("ENG-1", "initiative", "Billing Revamp")
    got = s.get("ENG-1")
    assert got.title == "Billing Revamp" and got.story_state is None

    s.record_chapter("ENG-1", "Shipped the invoice API.")
    got = s.get("ENG-1")
    assert got.story_state == "Shipped the invoice API."
    assert got.last_narrated_at is not None


def test_store_upsert_refreshes_title(tmp_path):
    s = InitiativeStore(tmp_path / "i.db")
    s.upsert("ENG-1", "initiative", "Old name")
    s.record_chapter("ENG-1", "state")
    s.upsert("ENG-1", "initiative", "New name")
    got = s.get("ENG-1")
    assert got.title == "New name"
    assert got.story_state == "state"  # state preserved across re-upsert


# --- grouping -------------------------------------------------------------- #
def test_group_by_initiative_orders_by_activity():
    a = Initiative(lane="initiative", key="ENG-1", title="A")
    b = Initiative(lane="initiative", key="ENG-2", title="B")
    prs = [_pr("r", 1), _pr("r", 2), _pr("r", 3)]
    mapping = {"r#1": a, "r#2": b, "r#3": a}  # A has 2, B has 1
    grouped = group_by_initiative(prs, mapping)
    assert [init.key for init, _ in grouped] == ["ENG-1", "ENG-2"]
    assert len(grouped[0][1]) == 2


# --- render orchestration -------------------------------------------------- #
@pytest.mark.asyncio
async def test_render_chapters_uses_prior_state_and_limit(tmp_path, monkeypatch):
    a = Initiative(lane="initiative", key="ENG-1", title="A")
    b = Initiative(lane="initiative", key="ENG-2", title="B")

    async def fake_resolve(model, prs, issues, *, cache=None):
        return {"r#1": a, "r#2": a, "r#3": b}

    seen_prior = {}

    async def fake_write(model, *, title, prior_state, prs):
        seen_prior[title] = prior_state
        return Chapter(chapter=f"chapter for {title}", story_state=f"state {title}")

    monkeypatch.setattr(st_mod, "resolve_initiatives", fake_resolve)
    monkeypatch.setattr(st_mod, "write_chapter", fake_write)

    store = InitiativeStore(tmp_path / "i.db")
    store.upsert("ENG-1", "initiative", "A")
    store.record_chapter("ENG-1", "previously shipped X")

    prs = [_pr("r", 1), _pr("r", 2), _pr("r", 3)]
    out = await render_chapters("m", prs, issues=[], store=store, limit=1)

    assert len(out) == 1                       # limit respected
    assert out[0].initiative.key == "ENG-1"    # most active first
    assert out[0].merged == 2
    assert "chapter for A" in out[0].content   # rendered content carried
    assert seen_prior["A"] == "previously shipped X"   # prior state passed in


@pytest.mark.asyncio
async def test_render_chapters_does_not_persist(tmp_path, monkeypatch):
    a = Initiative(lane="initiative", key="ENG-1", title="A")

    async def fake_resolve(model, prs, issues, *, cache=None):
        return {"r#1": a}

    async def fake_write(model, *, title, prior_state, prs):
        return Chapter(chapter="c", story_state="NEW STATE")

    monkeypatch.setattr(st_mod, "resolve_initiatives", fake_resolve)
    monkeypatch.setattr(st_mod, "write_chapter", fake_write)

    store = InitiativeStore(tmp_path / "i.db")
    store.upsert("ENG-1", "initiative", "A")
    await render_chapters("m", [_pr("r", 1)], issues=[], store=store)
    # Preview must not write story-state back.
    assert store.get("ENG-1").story_state is None
