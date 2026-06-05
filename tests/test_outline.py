"""OutlineClient request/response handling, offline via httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from daily_agent.sources.outline import OutlineClient, OutlineError, OutlineNotConfigured


def _client_with(handler) -> OutlineClient:
    c = OutlineClient("https://outline.example.com", "ol_api_test")
    c._client = httpx.AsyncClient(
        base_url="https://outline.example.com/api",
        headers={"Authorization": "Bearer ol_api_test"},
        transport=httpx.MockTransport(handler),
    )
    return c


def test_requires_config():
    with pytest.raises(OutlineNotConfigured):
        OutlineClient("", "")


@pytest.mark.asyncio
async def test_search_parses_results_and_builds_urls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents.search"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "context": "matched <b>text</b> here",
                        "document": {"id": "abc", "title": "ERD: Foo", "url": "/doc/erd-foo-abc"},
                    }
                ],
            },
        )

    async with _client_with(handler) as ol:
        results = await ol.search("foo")
    assert results == [
        {
            "id": "abc",
            "title": "ERD: Foo",
            "context": "matched <b>text</b> here",
            "url": "https://outline.example.com/doc/erd-foo-abc",
        }
    ]


@pytest.mark.asyncio
async def test_read_document_returns_markdown():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents.info"
        return httpx.Response(
            200, json={"ok": True, "data": {"title": "Foo", "text": "# Foo\nbody", "url": "/doc/foo"}}
        )

    async with _client_with(handler) as ol:
        doc = await ol.read_document("abc")
    assert doc["title"] == "Foo"
    assert "body" in doc["text"]
    assert doc["url"] == "https://outline.example.com/doc/foo"


@pytest.mark.asyncio
async def test_api_error_surfaces():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "authentication_required"})

    async with _client_with(handler) as ol:
        with pytest.raises(OutlineError):
            await ol.search("foo")
