"""Outline (engineering docs) source.

Outline exposes a JSON-over-POST API (every endpoint is a POST that returns
``{"data": ..., "ok": true}``) authenticated with a Bearer token. See
https://www.getoutline.com/developers.

We use three endpoints:
  * ``documents.search``   — full-text search across the knowledge base
  * ``documents.info``     — fetch one document's full markdown
  * ``collections.list``   — enumerate top-level collections

This grounds the deep-dive agent's explanations in real engineering docs
(PRDs, ERDs, SOWs, migration plans, ...).
"""

from __future__ import annotations

import httpx

from ..cache import Cache


class OutlineError(RuntimeError):
    pass


class OutlineNotConfigured(RuntimeError):
    pass


class OutlineClient:
    def __init__(
        self, base_url: str, token: str, *, cache: Cache | None = None, cache_ttl: int = 604800
    ) -> None:
        if not base_url or not token:
            raise OutlineNotConfigured(
                "Outline is not configured. Set DAILY_AGENT_OUTLINE_URL and "
                "DAILY_AGENT_OUTLINE_TOKEN."
            )
        self._cache = cache
        self._ttl = cache_ttl
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "OutlineClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _post(self, endpoint: str, payload: dict) -> object:
        resp = await self._client.post(endpoint, json=payload)
        if resp.status_code >= 400:
            raise OutlineError(f"POST {endpoint} -> {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if not body.get("ok", True):
            raise OutlineError(f"POST {endpoint} -> {body.get('error', 'unknown error')}")
        return body.get("data")

    def _doc_url(self, url_path: str) -> str:
        return f"{self.base_url}{url_path}" if url_path else self.base_url

    # --- queries ---------------------------------------------------------- #
    async def search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Full-text search. Returns [{id, title, context, url}] snippets."""
        key = f"outline:search:{limit}:{query}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        data = await self._post("/documents.search", {"query": query, "limit": limit})
        out: list[dict] = []
        for row in data or []:
            doc = row.get("document", {})
            out.append(
                {
                    "id": doc.get("id"),
                    "title": doc.get("title"),
                    "context": row.get("context", ""),
                    "url": self._doc_url(doc.get("url", "")),
                }
            )
        if self._cache:
            self._cache.set(key, out)
        return out

    async def read_document(self, doc_id: str) -> dict:
        """Fetch one document's full markdown by id."""
        key = f"outline:doc:{doc_id}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        data = await self._post("/documents.info", {"id": doc_id})
        data = data or {}
        doc = {
            "title": data.get("title", ""),
            "text": (data.get("text") or "")[:12000],
            "url": self._doc_url(data.get("url", "")),
        }
        if self._cache:
            self._cache.set(key, doc)
        return doc

    async def list_collections(self, *, limit: int = 50) -> list[str]:
        data = await self._post("/collections.list", {"limit": limit})
        return [c.get("name", "") for c in (data or [])]
