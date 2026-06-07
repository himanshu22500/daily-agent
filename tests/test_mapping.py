"""Initiative catalog + the deterministic/LLM mapping orchestrator (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_agent.cache import Cache
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
    # No orphans -> nothing to send to the LLM, so the mapper is never called.
    assert called["n"] == 0


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


# --- caching (issue #26) --------------------------------------------------- #
@pytest.mark.asyncio
async def test_cache_skips_llm_for_already_mapped_prs(monkeypatch, tmp_path):
    """Across two runs over the same orphan PRs, the LLM mapper is consulted
    once per PR; the second (fully cached) run sends nothing to the LLM."""
    seen: list[list[str]] = []

    async def fake_map(model, prs, catalog):
        seen.append([pr.title for pr in prs])
        return {f"{p.repo}#{p.number}": "ENG-1" for p in prs}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    cache = Cache(tmp_path / "c.db")
    issues = [_issue("ENG-1", "Billing Revamp")]
    prs = [_pr("api", 2, "feat(billing): add proration")]  # orphan (no ticket)

    out1 = await resolve_initiatives("m", prs, issues, cache=cache)
    assert out1["api#2"].key == "ENG-1"
    assert len(seen) == 1  # mapper consulted on the first run

    out2 = await resolve_initiatives("m", prs, issues, cache=cache)
    assert out2["api#2"].key == "ENG-1"  # same result, from cache
    assert len(seen) == 1  # NOT re-sent to the LLM on the second run


@pytest.mark.asyncio
async def test_cache_still_maps_new_unseen_prs(monkeypatch, tmp_path):
    """A warm cache must not starve new PRs: only cache-misses go to the LLM."""
    seen: list[list[str]] = []

    async def fake_map(model, prs, catalog):
        seen.append(sorted(f"{p.repo}#{p.number}" for p in prs))
        return {f"{p.repo}#{p.number}": "ENG-1" for p in prs}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    cache = Cache(tmp_path / "c.db")
    issues = [_issue("ENG-1", "Billing Revamp")]

    first = [_pr("api", 2, "feat(billing): add proration")]
    await resolve_initiatives("m", first, issues, cache=cache)

    # Second run: one already-mapped PR + one brand-new orphan.
    both = first + [_pr("api", 3, "feat(billing): add credits")]
    out = await resolve_initiatives("m", both, issues, cache=cache)
    assert out["api#2"].key == "ENG-1"
    assert out["api#3"].key == "ENG-1"
    # Only the unseen PR (api#3) was sent to the LLM on the second run.
    assert seen == [["api#2"], ["api#3"]]


@pytest.mark.asyncio
async def test_cached_key_absent_from_catalog_falls_to_untracked(monkeypatch, tmp_path):
    """A cached key that isn't in the current catalog must not crash; it is
    treated as untracked rather than re-mapped."""
    async def fake_map(model, prs, catalog):
        return {f"{p.repo}#{p.number}": "ENG-99" for p in prs}  # not in catalog later

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    cache = Cache(tmp_path / "c.db")
    prs = [_pr("api", 7, "feat: mystery")]

    # First run: a catalog where ENG-99 is a valid key, so it gets cached.
    await resolve_initiatives("m", prs, [_issue("ENG-99", "Old Thing")], cache=cache)
    # Second run: ENG-99 no longer in the catalog -> untracked, no crash, no LLM.
    out = await resolve_initiatives("m", prs, [_issue("ENG-1", "New Thing")], cache=cache)
    assert out["api#7"].lane == "untracked"


@pytest.mark.asyncio
async def test_no_cache_preserves_legacy_behavior(monkeypatch):
    """Without a cache, every orphan is mapped on every run (no persistence)."""
    calls = {"n": 0}

    async def fake_map(model, prs, catalog):
        calls["n"] += 1
        return {f"{p.repo}#{p.number}": "ENG-1" for p in prs}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-1", "Billing Revamp")]
    prs = [_pr("api", 2, "feat(billing): add proration")]
    await resolve_initiatives("m", prs, issues)
    await resolve_initiatives("m", prs, issues)
    assert calls["n"] == 2  # mapped both runs; nothing cached


@pytest.mark.asyncio
async def test_oncall_pr_routed_to_ops(monkeypatch):
    async def fake_map(model, prs, catalog):
        return {}

    monkeypatch.setattr(mapping_mod, "map_orphans", fake_map)
    issues = [_issue("ENG-5", "hotfix", [("ENG-6", "Project Oncall [Jun]")])]
    prs = [_pr("api", 5, "ENG-5 hotfix")]
    out = await resolve_initiatives("m", prs, issues)
    assert out["api#5"].lane == "ops"
