"""Command-line interface.

  daily-agent collect            Gather recent repo activity into the store.
  daily-agent summary            Summarize accumulated activity into a digest.
  daily-agent ask REPO "..."     Deep-dive into one project (business logic).
  daily-agent repos              List the org repos currently being watched.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import typer
from dotenv import load_dotenv

# Load .env into the process environment so provider SDKs (OpenAI, Anthropic,
# ...) can see their API keys, not just our DAILY_AGENT_* settings.
load_dotenv()
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agents.docs_qa import ask_docs
from .agents.researcher import research
from .agents.summarizer import summarize
from .config import get_settings
from .models import ActivityDigest
from .sources.github import GitHubClient, GitHubError
from .sources.huly import HulyClient, HulyError
from .sources.outline import OutlineClient, OutlineError
from .storage import Store

app = typer.Typer(
    add_completion=False,
    help="AI agents that watch your org's repos and summarize what's being worked on.",
)
console = Console()


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _github() -> GitHubClient:
    s = get_settings()
    return GitHubClient(token=s.github_token, org=s.github_org)


def _huly() -> HulyClient:
    s = get_settings()
    return HulyClient(
        url=s.huly_url, workspace=s.huly_workspace, email=s.huly_email,
        password=s.huly_password, token=s.huly_token, node_bin=s.node_bin,
    )


async def _collect(days: int) -> tuple[int, int, int]:
    s = get_settings()
    since = _since(days)
    store = Store(s.db_path)
    allow = set(s.repo_allowlist())
    total_pr = total_commit = 0
    async with _github() as gh:
        repos = await gh.list_repos(active_within_days=s.github_active_within_days)
        names = [r["name"] for r in repos]
        if allow:
            names = [n for n in names if n in allow]
        for name in names:
            activity = await gh.repo_activity(name, since)
            if activity.is_empty:
                continue
            n_pr, n_commit = store.save_activity(activity)
            total_pr += n_pr
            total_commit += n_commit
            console.print(
                f"  [dim]{name}[/dim]: {n_pr} PRs, {n_commit} commits", highlight=False
            )
    return len(names), total_pr, total_commit


@app.command()
def collect(
    days: int = typer.Option(None, help="Lookback window in days (default from config)."),
) -> None:
    """Gather recent repo activity from GitHub into the local store."""
    s = get_settings()
    window = days if days is not None else s.lookback_days
    try:
        n_repos, n_pr, n_commit = asyncio.run(_collect(window))
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)
    console.print(
        f"[green]Collected[/green] {n_pr} PRs and {n_commit} commits "
        f"across {n_repos} repos (last {window}d) -> {s.db_path}"
    )


@app.command()
def summary(
    days: int = typer.Option(7, help="Summarize activity from the last N days."),
) -> None:
    """Summarize accumulated activity into a cross-project digest."""
    s = get_settings()
    store = Store(s.db_path)
    activities = store.activity_since(_since(days))
    period = f"last {days} days"
    digest: ActivityDigest = asyncio.run(summarize(s.model, activities, period))
    _print_digest(digest)


@app.command()
def ask(
    repo: str = typer.Argument(..., help="Repo/project name to investigate."),
    question: str = typer.Argument(
        "What is this project, and what's the current focus?",
        help="What you want to understand.",
    ),
) -> None:
    """Deep-dive into one project to understand its business-logic layer."""
    s = get_settings()

    async def _run() -> str:
        async with AsyncExitStack() as stack:
            gh = await stack.enter_async_context(_github())
            outline = (
                await stack.enter_async_context(OutlineClient(s.outline_url, s.outline_token))
                if s.outline_enabled else None
            )
            huly = await stack.enter_async_context(_huly()) if s.huly_enabled else None
            return await research(s.model, gh, repo, question, huly=huly, outline=outline)

    try:
        answer = asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)
    console.print(Panel(Markdown(answer), title=f"Deep dive: {repo}", border_style="cyan"))


@app.command()
def repos() -> None:
    """List the org repos currently being watched (most recently pushed first)."""
    s = get_settings()

    async def _run() -> list[str]:
        async with _github() as gh:
            repos = await gh.list_repos(active_within_days=s.github_active_within_days)
            allow = set(s.repo_allowlist())
            return [r["name"] for r in repos if not allow or r["name"] in allow]

    try:
        names = asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[bold]{len(names)} repos watched:[/bold]")
    for n in names:
        console.print(f"  • {n}")


@app.command()
def docs(
    query: str = typer.Argument(..., help="Search the engineering docs (Outline)."),
) -> None:
    """Search your Outline knowledge base directly."""
    s = get_settings()
    if not s.outline_enabled:
        console.print("[red]Outline not configured[/red] (set DAILY_AGENT_OUTLINE_URL/TOKEN).")
        raise typer.Exit(1)

    async def _run() -> list[dict]:
        async with OutlineClient(s.outline_url, s.outline_token) as ol:
            return await ol.search(query, limit=10)

    try:
        results = asyncio.run(_run())
    except OutlineError as e:
        console.print(f"[red]Outline error:[/red] {e}")
        raise typer.Exit(1)
    if not results:
        console.print(f"No docs found for '{query}'.")
        return
    console.print(f"[bold]{len(results)} docs for '{query}':[/bold]")
    for r in results:
        console.print(f"  • [cyan]{r['title']}[/cyan]")
        if r["context"]:
            console.print(f"    [dim]{' '.join(r['context'].split())[:140]}[/dim]")
        console.print(f"    [dim]{r['url']}[/dim]")


@app.command()
def howto(
    question: str = typer.Argument(..., help="A how-to / setup / 'how does X work' question."),
) -> None:
    """Answer a question from your Outline docs — finds, reads, and synthesizes steps."""
    s = get_settings()
    if not s.outline_enabled:
        console.print("[red]Outline not configured[/red] (set DAILY_AGENT_OUTLINE_URL/TOKEN).")
        raise typer.Exit(1)

    async def _run() -> str:
        async with OutlineClient(s.outline_url, s.outline_token) as ol:
            return await ask_docs(s.model, ol, question)

    try:
        answer = asyncio.run(_run())
    except OutlineError as e:
        console.print(f"[red]Outline error:[/red] {e}")
        raise typer.Exit(1)
    console.print(Panel(Markdown(answer), title="From the docs", border_style="magenta"))


@app.command()
def tasks(
    project: str = typer.Argument(
        None, help="Huly project identifier (e.g. ENG). Defaults to DAILY_AGENT_HULY_DEFAULT_PROJECT."
    ),
    limit: int = typer.Option(30, help="Max issues to list."),
    projects: bool = typer.Option(False, "--projects", "-p", help="List projects instead of issues."),
) -> None:
    """List Huly issues for a project (defaults to the configured project)."""
    s = get_settings()
    if not s.huly_enabled:
        console.print("[red]Huly not configured[/red] (set DAILY_AGENT_HULY_WORKSPACE + creds).")
        raise typer.Exit(1)
    target = project or s.huly_default_project

    async def _run():
        async with _huly() as h:
            if projects or not target:
                return ("projects", await h.projects())
            return ("issues", await h.issues(target, limit=limit))

    try:
        kind, rows = asyncio.run(_run())
    except HulyError as e:
        console.print(f"[red]Huly error:[/red] {e}")
        raise typer.Exit(1)
    if kind == "projects":
        console.print(f"[bold]{len(rows)} Huly projects:[/bold]")
        for p in rows:
            console.print(f"  • [cyan]{p['identifier']}[/cyan] — {p['name']}")
        if not target:
            console.print("[dim]Tip: set DAILY_AGENT_HULY_DEFAULT_PROJECT to list its issues by default.[/dim]")
        return
    console.print(f"[bold]{len(rows)} issues in {target}:[/bold]")
    for i in rows:
        console.print(
            f"  • [cyan]{i['identifier']}[/cyan] [{i['status']}] {i['title']}"
            f"  [dim]({i['assignee'] or 'unassigned'})[/dim]"
        )


@app.command()
def task(
    identifier: str = typer.Argument(..., help="Huly issue identifier, e.g. ENG-16845."),
) -> None:
    """Show one Huly task's details (status, assignee, priority, description, PR links)."""
    s = get_settings()
    if not s.huly_enabled:
        console.print("[red]Huly not configured[/red] (set DAILY_AGENT_HULY_WORKSPACE + creds).")
        raise typer.Exit(1)

    async def _run():
        async with _huly() as h:
            return await h.issue(identifier)

    try:
        issue = asyncio.run(_run())
    except HulyError as e:
        console.print(f"[red]Huly error:[/red] {e}")
        raise typer.Exit(1)
    if issue is None:
        console.print(f"No Huly issue found: [bold]{identifier}[/bold]")
        raise typer.Exit(1)

    meta = (
        f"[bold]{issue['identifier']}[/bold]  {issue['title']}\n\n"
        f"Project: {issue.get('project', '?')}    Status: {issue['status']} "
        f"({issue['statusCategory']})\n"
        f"Assignee: {issue['assignee'] or 'unassigned'}    Priority: {issue['priority']}"
        f"    Due: {issue.get('dueDate') or '—'}"
    )
    console.print(Panel(meta, title=f"Task {issue['identifier']}", border_style="yellow"))

    desc = (issue.get("description") or "").strip()
    if desc:
        console.print(Panel(Markdown(desc), title="Description", border_style="blue"))
        prs = _github_pr_links(desc)
        if prs:
            console.print("[bold]Linked GitHub PRs:[/bold]")
            for url in prs:
                console.print(f"  • {url}")
    else:
        console.print("[dim](no description)[/dim]")


_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def _github_pr_links(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PR_URL_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def _print_digest(digest: ActivityDigest) -> None:
    console.print(Panel(digest.overview, title=f"Activity digest — {digest.period}", border_style="green"))
    for proj in digest.projects:
        body = [f"[bold]{proj.headline}[/bold]", "", proj.whats_happening]
        if proj.notable_changes:
            body.append("")
            body.extend(f"• {c}" for c in proj.notable_changes)
        if proj.contributors:
            body.append("")
            body.append(f"[dim]Contributors: {', '.join(proj.contributors)}[/dim]")
        console.print(Panel("\n".join(body), title=proj.project, border_style="blue"))


if __name__ == "__main__":
    app()
