"""Initiative mapper prompt rendering."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.agents.initiative_mapper import _render
from daily_agent.feed.initiative import Initiative
from daily_agent.models import PullRequest


def test_render_includes_pr_branch_name():
    now = datetime.now(timezone.utc)
    pr = PullRequest(
        repo="api",
        number=42,
        title="feat: process variants in bulk",
        head_ref_name="feat/v3/process-variant-bulk",
        author="alice",
        state="closed",
        merged=True,
        created_at=now,
        merged_at=now,
        url="https://github.example/api/pull/42",
        body="Adds the bulk processing flow for variants.",
    )
    catalog = [Initiative(lane="initiative", key="pm#1", title="Variant V3 Migration")]

    rendered = _render([pr], catalog)

    assert "api#42" in rendered
    assert "branch: feat/v3/process-variant-bulk" in rendered
    assert "description: Adds the bulk processing flow for variants." in rendered
