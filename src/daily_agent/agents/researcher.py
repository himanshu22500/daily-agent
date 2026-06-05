"""On-demand deep-dive researcher agent.

Given a project name and a question, this agent uses tools to gather context
(repo structure, README, key files, recent PRs — and later Huly tasks and
Outline docs) and explains the *business-logic layer*: what the project does,
why the recent changes matter, and how the pieces fit together.

It is a tool-using ReAct-style agent: the model decides which files to read and
how deep to go, rather than us pre-fetching everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from ..sources.github import GitHubClient
from ..sources.huly import HulyClient, HulyError, HulyNotConfigured
from ..sources.outline import OutlineClient, OutlineError, OutlineNotConfigured
from .model import build_model


@dataclass
class ResearchDeps:
    github: GitHubClient
    repo: str
    huly: HulyClient | None = None
    outline: OutlineClient | None = None


_SYSTEM_PROMPT = """\
You are a senior engineer onboarding a leader to a specific project. Your goal
is to explain the BUSINESS-LOGIC LAYER: what the project does for the business,
the core domain concepts, the main flows, and what recent changes mean.

You have tools to inspect the repository (file tree, README, file contents,
recent PRs) and, when available, the task tracker (Huly) and engineering docs
(Outline). Use them deliberately:
  1. Start with the README and file tree to orient yourself.
  2. Read the handful of files that define the domain model / entry points.
  3. Look at recent PRs to understand the current direction.
  4. Pull in docs/tasks for the "why" when available.

Don't dump file contents back. Synthesize. Prefer explaining concepts and flows
over listing code. Cite specific files/PRs when they support a point. If a
source isn't configured, note it and proceed with what you have.
"""


def build_researcher(model: str) -> Agent[ResearchDeps, str]:
    agent = Agent(build_model(model), deps_type=ResearchDeps, system_prompt=_SYSTEM_PROMPT)

    @agent.tool
    async def list_files(ctx: RunContext[ResearchDeps]) -> list[str]:
        """List the repository's files (paths) to understand its structure."""
        return await ctx.deps.github.file_tree(ctx.deps.repo)

    @agent.tool
    async def read_readme(ctx: RunContext[ResearchDeps]) -> str:
        """Read the repository's README."""
        return await ctx.deps.github.readme(ctx.deps.repo) or "(no README found)"

    @agent.tool
    async def read_file(ctx: RunContext[ResearchDeps], path: str) -> str:
        """Read a specific file's contents from the repository by path."""
        return await ctx.deps.github.read_file(ctx.deps.repo, path) or f"(could not read {path})"

    @agent.tool
    async def recent_pull_requests(ctx: RunContext[ResearchDeps]) -> str:
        """List recent pull requests with titles, authors, and descriptions."""
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=60)
        activity = await ctx.deps.github.repo_activity(ctx.deps.repo, since)
        if not activity.pull_requests:
            return "(no recent pull requests)"
        return "\n".join(
            f"#{pr.number} [{'MERGED' if pr.merged else pr.state}] {pr.title} "
            f"(by {pr.author})\n  {' '.join(pr.body.split())[:300]}"
            for pr in activity.pull_requests
        )

    @agent.tool
    async def search_docs(ctx: RunContext[ResearchDeps], query: str) -> str:
        """Search engineering docs (Outline) — PRDs, ERDs, SOWs, runbooks, etc.

        Returns matching document titles, ids, and context snippets. Use the id
        with `read_doc` to read a document's full content.
        """
        if ctx.deps.outline is None:
            return "(Outline docs not configured)"
        try:
            results = await ctx.deps.outline.search(query, limit=8)
        except (OutlineNotConfigured, OutlineError) as e:
            return f"(Outline unavailable: {e})"
        if not results:
            return f"(no docs found for '{query}')"
        return "\n".join(
            f"- {r['title']} (id: {r['id']})\n    {' '.join((r['context'] or '').split())[:200]}"
            for r in results
        )

    @agent.tool
    async def read_doc(ctx: RunContext[ResearchDeps], doc_id: str) -> str:
        """Read an Outline document's full content by its id (from search_docs)."""
        if ctx.deps.outline is None:
            return "(Outline docs not configured)"
        try:
            doc = await ctx.deps.outline.read_document(doc_id)
        except (OutlineNotConfigured, OutlineError) as e:
            return f"(Outline unavailable: {e})"
        return f"# {doc['title']}\n({doc['url']})\n\n{doc['text']}"

    @agent.tool
    async def huly_projects(ctx: RunContext[ResearchDeps]) -> str:
        """List projects in the Huly task tracker (identifier + name)."""
        if ctx.deps.huly is None:
            return "(Huly task tracker not configured)"
        try:
            projects = await ctx.deps.huly.projects()
        except (HulyNotConfigured, HulyError) as e:
            return f"(Huly unavailable: {e})"
        return "\n".join(f"- {p['identifier']}: {p['name']}" for p in projects) or "(no projects)"

    @agent.tool
    async def huly_issues(ctx: RunContext[ResearchDeps], project: str, limit: int = 30) -> str:
        """List recent issues for a Huly project identifier (e.g. 'ENG').

        Use this to learn what's planned / in progress / in review for the work
        behind the code — the "why" and status that GitHub alone doesn't show.
        """
        if ctx.deps.huly is None:
            return "(Huly task tracker not configured)"
        try:
            issues = await ctx.deps.huly.issues(project, limit=limit)
        except (HulyNotConfigured, HulyError) as e:
            return f"(Huly unavailable: {e})"
        if not issues:
            return f"(no issues for project '{project}')"
        return "\n".join(
            f"- {i['identifier']} [{i['status']}/{i['statusCategory']}] {i['title']} "
            f"(assignee: {i['assignee'] or 'none'}, priority: {i['priority']})"
            for i in issues
        )

    return agent


async def research(
    model: str,
    github: GitHubClient,
    repo: str,
    question: str,
    *,
    huly: HulyClient | None = None,
    outline: OutlineClient | None = None,
) -> str:
    agent = build_researcher(model)
    deps = ResearchDeps(github=github, repo=repo, huly=huly, outline=outline)
    prompt = (
        f"Project/repo: {repo}\n\nQuestion: {question}\n\n"
        "Investigate and explain the business-logic layer relevant to this question."
    )
    result = await agent.run(prompt, deps=deps)
    return result.output
