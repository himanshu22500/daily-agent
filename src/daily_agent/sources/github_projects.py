"""GitHub Projects (v2) source — the org's project board as the feed's PM data.

Replaces the former Huly source. The org now tracks initiatives as GitHub issues
on a Projects v2 board: native **sub-issues** form the parent chain (whose roots
are the initiatives), and **``closedByPullRequestsReferences``** links each issue
to the PRs that close it. We read the board over the GraphQL API and shape each
issue into the exact dict the feed's initiative resolver already consumes
(``identifier`` / ``title`` / ``parents`` / ``tags``), plus a new ``linked_prs``
that lets the resolver map PRs deterministically by inverting that graph — far
better coverage than the old ``ENG-<n>`` text match.

Project items, code PRs, and parents may live in different repos, so every repo
reference is normalized to its **bare name** (``fcbtech/pm`` -> ``pm``) to line up
with ``models.PullRequest.repo`` (also bare) and ``initiative_mapper.pr_key``.
"""

from __future__ import annotations

import httpx

from ..cache import Cache

_API = "https://api.github.com/graphql"

# One page (100 items max) of the project board, with each issue's parent chain
# (nested up to 3 levels — observed depth is 1), labels, single-select field
# values, and the PRs that close it.
_ISSUES_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            __typename
            ... on Issue {
              number
              title
              state
              repository { nameWithOwner }
              parent {
                number title repository { nameWithOwner }
                parent {
                  number title repository { nameWithOwner }
                  parent { number title repository { nameWithOwner } }
                }
              }
              labels(first: 10) { nodes { name } }
              closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
                nodes { number repository { nameWithOwner } }
              }
            }
          }
          fieldValues(first: 25) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubProjectsError(RuntimeError):
    pass


class GitHubProjectsNotConfigured(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pure shaping (no I/O) — project item nodes -> resolver-shaped issue dicts
# --------------------------------------------------------------------------- #
def _bare(name_with_owner: str | None) -> str:
    """``'fcbtech/pm'`` -> ``'pm'`` (matches ``PullRequest.repo``)."""
    return (name_with_owner or "").split("/")[-1]


def _issue_id(repo_node: dict, number: int) -> str:
    return f"{_bare((repo_node or {}).get('nameWithOwner'))}#{number}"


def _flatten_parents(content: dict) -> list[dict]:
    """Nested ``parent`` links -> ``[{identifier, title}]`` (immediate -> root)."""
    out: list[dict] = []
    node = content.get("parent")
    while node:
        out.append(
            {
                "identifier": _issue_id(node.get("repository"), node["number"]),
                "title": node.get("title") or "",
            }
        )
        node = node.get("parent")
    return out


def _tags(content: dict, field_values: dict) -> list[str]:
    """Labels + single-select field values (Status/Review State/Subtype) as tags."""
    labels = [
        n["name"]
        for n in (content.get("labels") or {}).get("nodes", [])
        if n.get("name")
    ]
    fields = [
        n["name"]
        for n in (field_values or {}).get("nodes", [])
        if n.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
        and n.get("name")
    ]
    return labels + fields


def _linked_prs(content: dict) -> list[dict]:
    """The PRs that close this issue, as ``[{repo (bare), number}]``."""
    nodes = (content.get("closedByPullRequestsReferences") or {}).get("nodes", [])
    return [
        {
            "repo": _bare((r.get("repository") or {}).get("nameWithOwner")),
            "number": r["number"],
        }
        for r in nodes
        if r.get("number") is not None
    ]


def shape_items(item_nodes: list[dict]) -> list[dict]:
    """Transform raw project item nodes into resolver-shaped issue dicts.

    Only ``Issue`` content is kept — PRs, draft issues, and redacted/empty items
    (``content`` is ``null`` or another type) are skipped.
    """
    out: list[dict] = []
    for item in item_nodes:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        out.append(
            {
                "identifier": _issue_id(content.get("repository"), content["number"]),
                "title": content.get("title") or "",
                "parents": _flatten_parents(content),
                "tags": _tags(content, item.get("fieldValues") or {}),
                "linked_prs": _linked_prs(content),
                "state": content.get("state") or "",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GitHubProjectsClient:
    """Read a GitHub Projects v2 board over GraphQL.

    Needs a token with the ``read:project`` scope (in addition to repo read). The
    ``gh`` CLI carries its own auth, but ``DAILY_AGENT_GITHUB_TOKEN`` may not — set
    ``DAILY_AGENT_GITHUB_PROJECT_TOKEN`` if the main token lacks project scope.
    """

    def __init__(
        self,
        *,
        token: str,
        owner: str,
        number: int,
        cache: Cache | None = None,
        cache_ttl: int = 600,
    ) -> None:
        if not token or not owner or not number:
            raise GitHubProjectsNotConfigured(
                "GitHub Projects is not configured. Set DAILY_AGENT_GITHUB_PROJECT_NUMBER "
                "(and an owner via DAILY_AGENT_GITHUB_PROJECT_OWNER or DAILY_AGENT_GITHUB_ORG) "
                "with a token that has the read:project scope."
            )
        self.owner = owner
        self.number = int(number)
        self._cache = cache
        self._ttl = cache_ttl
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "GitHubProjectsClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _query(self, cursor: str | None) -> dict:
        """One page of project items (the ``items`` connection object)."""
        resp = await self._client.post(
            _API,
            json={
                "query": _ISSUES_QUERY,
                "variables": {
                    "owner": self.owner,
                    "number": self.number,
                    "cursor": cursor,
                },
            },
        )
        if resp.status_code >= 400:
            raise GitHubProjectsError(
                f"GitHub Projects GraphQL -> {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if data.get("errors"):
            raise GitHubProjectsError(
                f"GitHub Projects GraphQL errors: {data['errors']}"
            )
        project = ((data.get("data") or {}).get("organization") or {}).get("projectV2")
        if project is None:
            raise GitHubProjectsError(
                f"Project #{self.number} not found for owner '{self.owner}' "
                "(check the number/owner and that the token can read it)."
            )
        return project["items"]

    async def issues(self, *, limit: int = 500) -> list[dict]:
        """All board issues, shaped for the initiative resolver (paginated)."""
        key = f"ghprojects:{self.owner}:{self.number}:{limit}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit

        shaped: list[dict] = []
        cursor: str | None = None
        while True:
            items = await self._query(cursor)
            shaped.extend(shape_items(items.get("nodes", [])))
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage") or len(shaped) >= limit:
                break
            cursor = page.get("endCursor")
        result = shaped[:limit]
        if self._cache:
            self._cache.set(key, result)
        return result
