"""Outline (engineering docs) source — STUB.

Outline has a clean JSON API (POST /api/documents.search, /api/documents.info,
etc.) with a bearer token. Once you provide DAILY_AGENT_OUTLINE_URL and
DAILY_AGENT_OUTLINE_TOKEN I'll implement:
  * full-text search across the knowledge base,
  * fetching a document's content,
  * collection/project scoped browsing,
so the deep-dive agent can ground its business-logic explanations in docs.
"""

from __future__ import annotations

from dataclasses import dataclass


class OutlineNotConfigured(RuntimeError):
    pass


@dataclass
class OutlineClient:
    base_url: str = ""
    token: str = ""

    def _require(self) -> None:
        if not self.base_url or not self.token:
            raise OutlineNotConfigured(
                "Outline is not configured yet. Provide DAILY_AGENT_OUTLINE_URL and "
                "DAILY_AGENT_OUTLINE_TOKEN to enable docs context."
            )

    async def search(self, query: str) -> list[dict]:
        self._require()
        raise NotImplementedError("Outline integration pending access details.")
