"""Initiative catalog + the deterministic/LLM mapping orchestrator (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_agent.feed import mapping as mapping_mod
from daily_agent.feed.catalog import build_catalog
from daily_agent.feed.mapping import resolve_initiatives
from daily_agent.models import PullRequest


def _issue(identifier, title, parents=None):
    return {
        "identifier": identifier, "title": title,
        "parents": [{"identifier": i, "id": f"h-{i}", "title": t} for i, t in (parents or [])],
    }


def _pr(repo, num, title, body=""):
    now = datetime.now(timezone.utc)
    return PullRequest(repo=repo, number=num, title=title, author="a", state="closed",
                       merged=True, created_at=now, merged_at=now, url=f"http://x/{num}", body=body)


# --- catalog --------------------------------------------------------------- #
def test_catalog_is_distinct_initiatives_only():
    issues = [
        _issue("ENG-10", "Sub A", [("ENG-1", "Billing Revamp")]),
        _issue("ENG-11", "Sub B", [("ENG-1", "Billing Revamp")]),   # same initiative
        _issue("ENG-2", "Standalone Feature"),                       # itself
        _issue("ENG-12", "fix", [("ENG-3", "Project Oncall [Jun]")]),  # ops, excluded
    ]
    catalog = build_catalog(issues)
    keys = {c.key for c in catalog}
    assert keys == {"ENG-1", "ENG-2"}              # deduped; ops excluded
    assert all(c.lane == "initiative" for c in catalog)


# --- orchestrator ---------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ticketed_pr_anchored_without_llm(monkeypatch):
    called = {"n": 0}

    async def fake_map(model, prs, catalog):
        called["n"] += 1
        return {}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-1", "Billing Revamp")]
    prs = [_pr("api", 1, "ENG-1 add invoices")]
    out = await resolve_initiatives("m", prs, issues)
    assert out["api#1"].key == "ENG-1"
    # No orphans -> map_orphans still called (with empty list) but returns {}.
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_orphan_pr_mapped_by_llm_onto_catalog(monkeypatch):
    async def fake_map(model, prs, catalog):
        return {"api#2": "ENG-1"}  # LLM places the scoped PR onto Billing Revamp

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-1", "Billing Revamp")]
    prs = [_pr("api", 2, "feat(billing): add proration")]  # no ticket
    out = await resolve_initiatives("m", prs, issues)
    assert out["api#2"].key == "ENG-1"
    assert out["api#2"].lane == "initiative"


@pytest.mark.asyncio
async def test_llm_untracked_and_invalid_key_fall_to_untracked(monkeypatch):
    async def fake_map(model, prs, catalog):
        return {"api#3": "untracked", "api#4": "ENG-DOES-NOT-EXIST"}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-1", "Billing Revamp")]
    prs = [_pr("api", 3, "chore: tidy"), _pr("api", 4, "feat: mystery")]
    out = await resolve_initiatives("m", prs, issues)
    # Both untracked: one explicitly, one because the LLM returned a non-catalog key.
    assert out["api#3"].lane == "untracked"
    assert out["api#4"].lane == "untracked"


@pytest.mark.asyncio
async def test_oncall_pr_routed_to_ops(monkeypatch):
    async def fake_map(model, prs, catalog):
        return {}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-5", "hotfix", [("ENG-6", "Project Oncall [Jun]")])]
    prs = [_pr("api", 5, "ENG-5 hotfix")]
    out = await resolve_initiatives("m", prs, issues)
    assert out["api#5"].lane == "ops"
