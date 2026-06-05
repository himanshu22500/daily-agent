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
from ..sources.huly import HulyClient, HulyNotConfigured
from ..sources.outline import OutlineClient, OutlineNotConfigured
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
        """Search engineering docs (Outline) for the given query."""
        if ctx.deps.outline is None:
            return "(Outline docs not configured)"
        try:
            results = await ctx.deps.outline.search(query)
        except (OutlineNotConfigured, NotImplementedError) as e:
            return f"(Outline unavailable: {e})"
        return str(results)

    @agent.tool
    async def project_tasks(ctx: RunContext[ResearchDeps], project: str) -> str:
        """Fetch tasks/issues for a project from the task tracker (Huly)."""
        if ctx.deps.huly is None:
            return "(Huly task tracker not configured)"
        try:
            tasks = await ctx.deps.huly.project_tasks(project)
        except (HulyNotConfigured, NotImplementedError) as e:
            return f"(Huly unavailable: {e})"
        return str(tasks)

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
