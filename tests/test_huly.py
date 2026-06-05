"""HulyClient argument-building + PR-link extraction (offline; bridge mocked)."""

from __future__ import annotations

import pytest

from daily_agent.cli import _github_pr_links
from daily_agent.sources.huly import HulyClient, HulyNotConfigured


def _client(monkeypatch):
    c = HulyClient(workspace="ws", email="a@b.c", password="pw")
    calls = []

    async def fake_run(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(c, "_run", fake_run)
    return c, calls


def test_requires_config():
    with pytest.raises(HulyNotConfigured):
        HulyClient(workspace="", email="", password="", token="")


def test_token_only_is_configured():
    HulyClient(workspace="ws", token="tok")  # should not raise


@pytest.mark.asyncio
async def test_issues_builds_args(monkeypatch):
    c, calls = _client(monkeypatch)
    await c.issues("ENG", limit=10)
    assert calls[0] == ("issues", "--limit", "10", "--project", "ENG")


@pytest.mark.asyncio
async def test_issues_without_project_omits_flag(monkeypatch):
    c, calls = _client(monkeypatch)
    await c.issues(None, limit=5)
    assert calls[0] == ("issues", "--limit", "5")


@pytest.mark.asyncio
async def test_issues_with_all_filters(monkeypatch):
    c, calls = _client(monkeypatch)
    await c.issues("ENG", limit=20, status="In Review", assignee="Himanshu", priority="high")
    assert calls[0] == (
        "issues", "--limit", "20", "--project", "ENG",
        "--status", "In Review", "--assignee", "Himanshu", "--priority", "high",
    )


@pytest.mark.asyncio
async def test_issue_and_projects_args(monkeypatch):
    c, calls = _client(monkeypatch)
    await c.issue("ENG-1")
    await c.projects()
    assert calls[0] == ("issue", "ENG-1")
    assert calls[1] == ("projects",)


def test_pr_link_extraction_dedupes_and_orders():
    text = (
        "see https://github.com/fcbtech/tz-vue-3/pull/1596 and "
        "https://github.com/fcbtech/tz-vue-3/pull/1588, also dup "
        "https://github.com/fcbtech/tz-vue-3/pull/1596 again"
    )
    assert _github_pr_links(text) == [
        "https://github.com/fcbtech/tz-vue-3/pull/1596",
        "https://github.com/fcbtech/tz-vue-3/pull/1588",
    ]


def test_pr_link_extraction_empty():
    assert _github_pr_links("no links here #1596") == []
