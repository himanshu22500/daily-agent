"""GitHub source parsing, offline via httpx.MockTransport."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from daily_agent.sources.github import GitHubClient


def _client_with(handler) -> GitHubClient:
    client = GitHubClient("gh_test", "acme")
    client._client = httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={
            "Authorization": "Bearer gh_test",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_repo_activity_captures_pull_request_head_ref_name():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/api/pulls":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 42,
                        "title": "feat: process variants in bulk",
                        "head": {"ref": "feat/v3/process-variant-bulk"},
                        "user": {"login": "alice"},
                        "state": "closed",
                        "merged_at": "2026-06-08T12:00:00Z",
                        "created_at": "2026-06-08T10:00:00Z",
                        "updated_at": "2026-06-08T12:30:00Z",
                        "html_url": "https://github.example/acme/api/pull/42",
                        "body": "Bulk variant processing",
                    }
                ],
            )
        if request.url.path == "/repos/acme/api/commits":
            return httpx.Response(200, json=[])
        return httpx.Response(404, text=f"unexpected path: {request.url.path}")

    since = datetime(2026, 6, 8, tzinfo=timezone.utc)
    async with _client_with(handler) as gh:
        activity = await gh.repo_activity("api", since)

    assert activity.pull_requests[0].head_ref_name == "feat/v3/process-variant-bulk"


@pytest.mark.asyncio
async def test_search_pull_requests_fetches_head_ref_name_from_pr_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "number": 7,
                            "title": "refactor: insights read workflows",
                            "user": {"login": "alice"},
                            "state": "closed",
                            "created_at": "2026-06-08T10:00:00Z",
                            "html_url": "https://github.example/acme/web/pull/7",
                            "body": "Clean up read paths",
                            "repository_url": "https://api.github.com/repos/acme/web",
                            "pull_request": {
                                "url": "https://api.github.com/repos/acme/web/pulls/7",
                                "merged_at": "2026-06-08T12:00:00Z",
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/repos/acme/web/pulls/7":
            return httpx.Response(
                200,
                json={
                    "head": {"ref": "refactor/insights-read-workflows"},
                    "merged_at": "2026-06-08T12:00:00Z",
                    "additions": 10,
                    "deletions": 2,
                    "changed_files": 3,
                },
            )
        return httpx.Response(404, text=f"unexpected path: {request.url.path}")

    since = datetime(2026, 6, 8, tzinfo=timezone.utc)
    async with _client_with(handler) as gh:
        prs = await gh.search_pull_requests("alice", since)

    assert prs[0].head_ref_name == "refactor/insights-read-workflows"
    assert prs[0].changed_files == 3
