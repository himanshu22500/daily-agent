"""General-purpose assistant agent — ask anything across the org.

Unlike the repo-pinned researcher, this agent isn't tied to a single repo. It
has tools spanning all sources (GitHub repos + PRs, Huly tasks, Outline docs),
plus the team identity map and the latest daily digest. It can resolve a person,
find the right repos itself, and "double-click" on what someone is working on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic_ai import Agent, RunContext

from ..config import Settings
from ..sources.github import GitHubClient
from ..sources.huly import HulyClient, HulyError
from ..sources.outline import OutlineClient, OutlineError
from ..team import TeamMember, resolve_member
from .model import build_model


@dataclass
class AssistantDeps:
    github: GitHubClient
    settings: Settings
    team: dict[str, TeamMember]
    huly: HulyClient | None = None
    outline: OutlineClient | None = None


_SYSTEM_PROMPT = """\
You are an engineering assistant for a software org. Answer the user's question
by investigating with your tools — don't guess. You can:
  - list and inspect repositories (file tree, README, files, recent PRs),
  - look up what a *person* is working on (person_activity resolves names),
  - read the Huly task tracker (projects, issues, one issue's detail),
  - search and read Outline engineering docs,
  - read the latest daily digest (for questions about "the report").

Approach:
  1. If the question names a person, start with person_activity to see their
     PRs and tasks, then read the relevant repos/docs to "double-click".
  2. If it names a topic/initiative, use list_repos + recent PRs + docs to find
     where it lives.
  3. If it references "the report"/"the digest", read latest_digest first.

Synthesize a clear, concrete answer grounded in what you find. Cite specific
repos, PR numbers, task ids, or doc titles. Be honest about gaps. Don't dump raw
file contents — explain.
"""


def build_assistant(model: str) -> Agent[AssistantDeps, str]:
    agent = Agent(build_model(model), deps_type=AssistantDeps, system_prompt=_SYSTEM_PROMPT)

    @agent.tool
    async def list_repos(ctx: RunContext[AssistantDeps]) -> str:
        """List the org repos active recently (names)."""
        repos = await ctx.deps.github.list_repos(
            active_within_days=ctx.deps.settings.github_active_within_days
        )
        return ", ".join(r["name"] for r in repos) or "(none)"

    @agent.tool
    async def repo_files(ctx: RunContext[AssistantDeps], repo: str) -> list[str]:
        """List a repository's file paths."""
        return await ctx.deps.github.file_tree(repo)

    @agent.tool
    async def read_readme(ctx: RunContext[AssistantDeps], repo: str) -> str:
        """Read a repository's README."""
        return await ctx.deps.github.readme(repo) or "(no README)"

    @agent.tool
    async def read_file(ctx: RunContext[AssistantDeps], repo: str, path: str) -> str:
        """Read a file's contents from a repository."""
        return await ctx.deps.github.read_file(repo, path) or f"(could not read {path})"

    @agent.tool
    async def repo_pull_requests(ctx: RunContext[AssistantDeps], repo: str) -> str:
        """Recent pull requests for a repository (titles, authors, descriptions)."""
        since = datetime.now(timezone.utc) - timedelta(days=60)
        activity = await ctx.deps.github.repo_activity(repo, since)
        if not activity.pull_requests:
            return "(no recent PRs)"
        return "\n".join(
            f"#{pr.number} [{'merged' if pr.merged else pr.state}] {pr.title} (by {pr.author})"
            f"\n  {' '.join(pr.body.split())[:240]}"
            for pr in activity.pull_requests
        )

    @agent.tool
    async def person_activity(ctx: RunContext[AssistantDeps], name: str) -> str:
        """What a person is working on: their recent GitHub PRs + Huly tasks.

        Resolves a name/handle/"me" via the team map. Use this to double-click
        on someone's work.
        """
        member = resolve_member(ctx.deps.team, name, me=ctx.deps.settings.me)
        if member is None:
            known = ", ".join(ctx.deps.team) or "(team map empty)"
            return f"(unknown person '{name}'. Known: {known})"
        since = datetime.now(timezone.utc) - timedelta(days=14)
        lines = [f"{member.name} (huly: {member.huly}, github: {member.github})", "", "Recent PRs:"]
        prs = await ctx.deps.github.search_pull_requests(member.github, since, limit=40)
        lines += [
            f"- {pr.repo}#{pr.number} [{'merged' if pr.merged else pr.state}] {pr.title}"
            for pr in prs
        ] or ["(none)"]
        if ctx.deps.huly:
            try:
                issues = await ctx.deps.huly.issues(
                    ctx.deps.settings.huly_default_project or None, assignee=member.huly, limit=50
                )
                lines += ["", "Huly tasks:"] + (
                    [f"- {i['identifier']} [{i['status']}] {i['title']}" for i in issues] or ["(none)"]
                )
            except (HulyError, Exception) as e:  # noqa: BLE001 - degrade gracefully
                lines.append(f"(Huly unavailable: {e})")
        return "\n".join(lines)

    @agent.tool
    async def huly_issues(ctx: RunContext[AssistantDeps], project: str = "", status: str = "") -> str:
        """List Huly issues, optionally filtered by project and status name."""
        if ctx.deps.huly is None:
            return "(Huly not configured)"
        proj = project or ctx.deps.settings.huly_default_project or None
        try:
            issues = await ctx.deps.huly.issues(proj, status=status or None, limit=50)
        except HulyError as e:
            return f"(Huly error: {e})"
        return "\n".join(
            f"- {i['identifier']} [{i['status']}] {i['title']} ({i['assignee'] or 'unassigned'})"
            for i in issues
        ) or "(no issues)"

    @agent.tool
    async def huly_issue(ctx: RunContext[AssistantDeps], identifier: str) -> str:
        """One Huly issue's full detail (incl. description)."""
        if ctx.deps.huly is None:
            return "(Huly not configured)"
        try:
            issue = await ctx.deps.huly.issue(identifier)
        except HulyError as e:
            return f"(Huly error: {e})"
        if not issue:
            return f"(no issue {identifier})"
        return (
            f"{issue['identifier']} [{issue['status']}] {issue['title']}\n"
            f"assignee: {issue['assignee']}, priority: {issue['priority']}\n\n"
            f"{issue.get('description') or '(no description)'}"
        )

    @agent.tool
    async def search_docs(ctx: RunContext[AssistantDeps], query: str) -> str:
        """Search Outline engineering docs. Returns titles + ids + snippets."""
        if ctx.deps.outline is None:
            return "(Outline not configured)"
        try:
            results = await ctx.deps.outline.search(query, limit=8)
        except OutlineError as e:
            return f"(Outline error: {e})"
        return "\n".join(
            f"- {r['title']} (id: {r['id']})\n    {' '.join((r['context'] or '').split())[:200]}"
            for r in results
        ) or f"(no docs for '{query}')"

    @agent.tool
    async def read_doc(ctx: RunContext[AssistantDeps], doc_id: str) -> str:
        """Read an Outline document's full content by id."""
        if ctx.deps.outline is None:
            return "(Outline not configured)"
        try:
            doc = await ctx.deps.outline.read_document(doc_id)
        except OutlineError as e:
            return f"(Outline error: {e})"
        return f"# {doc['title']}\n\n{doc['text']}"

    @agent.tool
    async def latest_digest(ctx: RunContext[AssistantDeps]) -> str:
        """Read the most recent daily digest (for questions about 'the report')."""
        d = Path(ctx.deps.settings.digest_dir)
        files = sorted(d.glob("*.md")) if d.exists() else []
        if not files:
            return "(no digests generated yet — run `daily-agent daily`)"
        return files[-1].read_text()[:12000]

    return agent


async def ask_anything(
    model: str,
    question: str,
    github: GitHubClient,
    *,
    settings: Settings,
    team: dict[str, TeamMember],
    huly: HulyClient | None = None,
    outline: OutlineClient | None = None,
    repo_hint: str | None = None,
) -> str:
    agent = build_assistant(model)
    deps = AssistantDeps(github=github, settings=settings, team=team, huly=huly, outline=outline)
    prompt = question if not repo_hint else f"(Focus on the repo: {repo_hint})\n\n{question}"
    result = await agent.run(prompt, deps=deps)
    return result.output
