"""GitHub data collection via the REST API (no extra SDK dependency).

Async ``httpx`` client that can:
  * list an org's repos (most-recently-pushed first),
  * pull recent PRs (with merge + diff stats) and commits for a repo,
  * fetch a repo's README and tree for the deep-dive assistant.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..cache import Cache
from ..models import Commit, PullRequest, RepoActivity

_API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self, token: str, org: str, *, cache: Cache | None = None, cache_ttl: int = 600
    ) -> None:
        if not token:
            raise GitHubError("No GitHub token configured (DAILY_AGENT_GITHUB_TOKEN).")
        if not org:
            raise GitHubError("No GitHub org configured (DAILY_AGENT_GITHUB_ORG).")
        self.org = org
        self._cache = cache
        self._ttl = cache_ttl
        self._client = httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    # --- low level -------------------------------------------------------- #
    async def _get(self, path: str, **params) -> httpx.Response:
        resp = await self._client.get(path, params=params or None)
        if resp.status_code >= 400:
            raise GitHubError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _paginate(self, path: str, *, limit: int, **params) -> list[dict]:
        out: list[dict] = []
        params.setdefault("per_page", 100)
        url: str | None = path
        while url and len(out) < limit:
            resp = await self._get(url, **params) if url == path else await self._raw(url)
            out.extend(resp.json())
            url = _next_link(resp)
            params = {}  # params are baked into the `next` URL
        return out[:limit]

    async def _raw(self, url: str) -> httpx.Response:
        resp = await self._client.get(url)
        if resp.status_code >= 400:
            raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    # --- repos ------------------------------------------------------------ #
    async def list_repos(self, *, active_within_days: int | None = None) -> list[dict]:
        repos = await self._paginate(
            f"/orgs/{self.org}/repos", limit=500, sort="pushed", direction="desc"
        )
        repos = [r for r in repos if not r.get("archived")]
        if active_within_days is not None:
            cutoff = _utcnow().timestamp() - active_within_days * 86400
            repos = [r for r in repos if _ts(r.get("pushed_at")) >= cutoff]
        return repos

    # --- activity --------------------------------------------------------- #
    async def repo_activity(self, repo: str, since: datetime) -> RepoActivity:
        key = f"gh:activity:{repo}:{since.date().isoformat()}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return RepoActivity.model_validate(hit)
        prs = await self._recent_pulls(repo, since)
        commits = await self._recent_commits(repo, since)
        activity = RepoActivity(repo=repo, pull_requests=prs, commits=commits)
        if self._cache:
            self._cache.set(key, activity.model_dump(mode="json"))
        return activity

    async def _recent_pulls(self, repo: str, since: datetime) -> list[PullRequest]:
        # PRs sorted by last update desc; stop once we pass the window.
        raw = await self._paginate(
            f"/repos/{self.org}/{repo}/pulls",
            limit=100, state="all", sort="updated", direction="desc",
        )
        out: list[PullRequest] = []
        for p in raw:
            updated = _parse(p["updated_at"])
            if updated < since:
                break
            out.append(
                PullRequest(
                    repo=repo,
                    number=p["number"],
                    title=p["title"],
                    author=(p.get("user") or {}).get("login", "unknown"),
                    state=p["state"],
                    merged=bool(p.get("merged_at")),
                    created_at=_parse(p["created_at"]),
                    merged_at=_parse(p["merged_at"]) if p.get("merged_at") else None,
                    url=p["html_url"],
                    body=(p.get("body") or "")[:4000],
                )
            )
        return out

    async def _recent_commits(self, repo: str, since: datetime) -> list[Commit]:
        try:
            raw = await self._paginate(
                f"/repos/{self.org}/{repo}/commits",
                limit=100, since=since.isoformat(),
            )
        except GitHubError:
            return []  # empty repo / no default branch
        out: list[Commit] = []
        for c in raw:
            commit = c.get("commit", {})
            author = (c.get("author") or {}).get("login") or commit.get("author", {}).get("name", "unknown")
            out.append(
                Commit(
                    repo=repo,
                    sha=c["sha"],
                    author=author,
                    message=commit.get("message", "").split("\n")[0][:300],
                    date=_parse(commit["author"]["date"]),
                    url=c["html_url"],
                )
            )
        return out

    async def search_pull_requests(
        self, author: str, since: datetime, *, limit: int = 50
    ) -> list[PullRequest]:
        """PRs across the org authored by `author` and updated since `since`."""
        key = f"gh:authorprs:{author}:{since.date().isoformat()}:{limit}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return [PullRequest.model_validate(x) for x in hit]
        q = (
            f"org:{self.org} type:pr author:{author} "
            f"updated:>={since.date().isoformat()}"
        )
        resp = await self._get(
            "/search/issues", q=q, sort="updated", order="desc",
            per_page=min(limit, 100),
        )
        out: list[PullRequest] = []
        for it in resp.json().get("items", [])[:limit]:
            pr_meta = it.get("pull_request") or {}
            repo = it.get("repository_url", "/").rsplit("/", 1)[-1]
            out.append(
                PullRequest(
                    repo=repo,
                    number=it["number"],
                    title=it["title"],
                    author=author,
                    state=it["state"],
                    merged=bool(pr_meta.get("merged_at")),
                    created_at=_parse(it["created_at"]),
                    merged_at=_parse(pr_meta["merged_at"]) if pr_meta.get("merged_at") else None,
                    url=it["html_url"],
                    body=(it.get("body") or "")[:1000],
                )
            )
        if self._cache:
            self._cache.set(key, [pr.model_dump(mode="json") for pr in out])
        return out

    # --- deep-dive helpers ----------------------------------------------- #
    async def readme(self, repo: str) -> str:
        key = f"gh:readme:{repo}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        try:
            resp = await self._get(
                f"/repos/{self.org}/{repo}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            text = resp.text[:8000]
        except GitHubError:
            text = ""
        if self._cache:
            self._cache.set(key, text)
        return text

    async def file_tree(self, repo: str, *, limit: int = 300) -> list[str]:
        key = f"gh:tree:{repo}:{limit}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        try:
            repo_info = (await self._get(f"/repos/{self.org}/{repo}")).json()
            branch = repo_info.get("default_branch", "main")
            tree = (
                await self._get(
                    f"/repos/{self.org}/{repo}/git/trees/{branch}", recursive="1"
                )
            ).json()
            paths = [t["path"] for t in tree.get("tree", []) if t["type"] == "blob"][:limit]
        except GitHubError:
            paths = []
        if self._cache:
            self._cache.set(key, paths)
        return paths

    async def read_file(self, repo: str, path: str) -> str:
        key = f"gh:file:{repo}:{path}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        try:
            resp = await self._get(
                f"/repos/{self.org}/{repo}/contents/{path}",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            text = resp.text[:12000]
        except GitHubError:
            text = ""
        if self._cache:
            self._cache.set(key, text)
        return text


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ts(value: str | None) -> float:
    return _parse(value).timestamp() if value else 0.0


def _next_link(resp: httpx.Response) -> str | None:
    link = resp.headers.get("Link", "")
    for part in link.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
