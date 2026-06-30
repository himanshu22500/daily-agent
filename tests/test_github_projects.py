"""GitHubProjectsClient shaping + pagination + cache (offline; HTTP mocked)."""

from __future__ import annotations

import pytest

from daily_agent.cache import Cache
from daily_agent.sources.github_projects import (
    GitHubProjectsClient,
    GitHubProjectsNotConfigured,
    shape_items,
)


def _issue(
    number, title, *, repo="fcbtech/pm", parent=None, labels=(), prs=(), state="OPEN"
):
    return {
        "content": {
            "__typename": "Issue",
            "number": number,
            "title": title,
            "state": state,
            "repository": {"nameWithOwner": repo},
            "parent": parent,
            "labels": {"nodes": [{"name": n} for n in labels]},
            "closedByPullRequestsReferences": {
                "nodes": [
                    {"number": n, "repository": {"nameWithOwner": r}} for r, n in prs
                ]
            },
        },
        "fieldValues": {"nodes": []},
    }


def _parent(number, title, *, repo="fcbtech/pm", parent=None):
    return {
        "number": number,
        "title": title,
        "repository": {"nameWithOwner": repo},
        "parent": parent,
    }


# --- shaping --------------------------------------------------------------- #
def test_skips_non_issue_and_empty_content():
    nodes = [
        _issue(56, "root"),
        {
            "content": {"__typename": "PullRequest", "number": 9},
            "fieldValues": {"nodes": []},
        },
        {"content": None, "fieldValues": {"nodes": []}},
        {"content": {"__typename": "DraftIssue"}, "fieldValues": {"nodes": []}},
    ]
    shaped = shape_items(nodes)
    assert [i["identifier"] for i in shaped] == ["pm#56"]


def test_identifier_uses_bare_repo():
    [shaped] = shape_items([_issue(56, "root")])
    assert shaped["identifier"] == "pm#56"


def test_parent_chain_flattened_immediate_to_root():
    chain = _parent(100, "mid", parent=_parent(16, "root"))
    [shaped] = shape_items([_issue(200, "leaf", parent=chain)])
    assert shaped["parents"] == [
        {"identifier": "pm#100", "title": "mid"},
        {"identifier": "pm#16", "title": "root"},
    ]


def test_root_issue_has_no_parents():
    [shaped] = shape_items([_issue(56, "root", parent=None)])
    assert shaped["parents"] == []


def test_linked_prs_normalize_to_bare_repo():
    node = _issue(
        20, "x", prs=[("fcbtech/tranzact-v2", 5714), ("fcbtech/tz-vue-3", 1596)]
    )
    [shaped] = shape_items([node])
    assert shaped["linked_prs"] == [
        {"repo": "tranzact-v2", "number": 5714},
        {"repo": "tz-vue-3", "number": 1596},
    ]


def test_tags_combine_labels_and_single_select_fields():
    node = _issue(33, "x", labels=["new-feature"])
    node["fieldValues"]["nodes"] = [
        {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": "WIP",
            "field": {"name": "Status"},
        },
        {
            "__typename": "ProjectV2ItemFieldRepositoryValue"
        },  # not a single-select -> ignored
        {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": "In Review",
            "field": {"name": "Review State"},
        },
    ]
    [shaped] = shape_items([node])
    assert shaped["tags"] == ["new-feature", "WIP", "In Review"]


# --- pagination + cache (client, _query mocked) ---------------------------- #
def _client(monkeypatch, pages, *, cache=None):
    c = GitHubProjectsClient(token="t", owner="fcbtech", number=86, cache=cache)
    cursors: list = []

    async def fake_query(cursor):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(c, "_query", fake_query)
    return c, cursors


@pytest.mark.asyncio
async def test_pagination_follows_cursor(monkeypatch):
    pages = [
        {
            "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
            "nodes": [_issue(1, "a")],
        },
        {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_issue(2, "b")],
        },
    ]
    c, cursors = _client(monkeypatch, pages)
    try:
        issues = await c.issues()
    finally:
        await c._client.aclose()
    assert cursors == [None, "C1"]  # second page fetched with the first's endCursor
    assert [i["identifier"] for i in issues] == ["pm#1", "pm#2"]


@pytest.mark.asyncio
async def test_issues_cached_across_calls(monkeypatch, tmp_path):
    cache = Cache(tmp_path / "c.db")
    pages = [
        {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_issue(1, "a")],
        }
    ]
    c, cursors = _client(monkeypatch, pages, cache=cache)
    try:
        first = await c.issues()
        second = await c.issues()
    finally:
        await c._client.aclose()
    assert first == second
    assert len(cursors) == 1  # second call served from cache, no new query


def test_requires_config():
    with pytest.raises(GitHubProjectsNotConfigured):
        GitHubProjectsClient(token="", owner="fcbtech", number=86)
    with pytest.raises(GitHubProjectsNotConfigured):
        GitHubProjectsClient(token="t", owner="", number=86)
    with pytest.raises(GitHubProjectsNotConfigured):
        GitHubProjectsClient(token="t", owner="fcbtech", number=0)
