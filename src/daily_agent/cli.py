"""Command-line interface.

  daily-agent collect            Gather recent repo activity into the store.
  daily-agent summary            Summarize accumulated activity into a digest.
  daily-agent ask REPO "..."     Deep-dive into one project (business logic).
  daily-agent repos              List the org repos currently being watched.
  daily-agent feed               Deliver accumulated activity as deduped bites.
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

from .agents.assistant import AssistantDeps, ask_anything, build_assistant
from .agents.docs_qa import ask_docs
from .agents.person_brief import summarize_person
from .agents.summarizer import summarize
from .cache import Cache
from .deliver import render_markdown, write_file
from .config import get_settings
from .feed.channels import ConsoleChannel, FileChannel
from .feed.delta import bites_for_activity
from .feed.outbox import Channel, Outbox
from .models import ActivityDigest
from .sources.github import GitHubClient, GitHubError
from .sources.huly import HulyClient, HulyError
from .sources.outline import OutlineClient, OutlineError
from .storage import Store
from .team import load_team, resolve_member

app = typer.Typer(
    add_completion=False,
    help="AI agents that watch your org's repos and summarize what's being worked on.",
)
console = Console()


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _cache() -> Cache:
    s = get_settings()
    return Cache(s.db_path, enabled=s.cache_enabled)


def _github() -> GitHubClient:
    s = get_settings()
    return GitHubClient(
        token=s.github_token, org=s.github_org,
        cache=_cache(), cache_ttl=s.github_cache_ttl,
    )


def _huly() -> HulyClient:
    s = get_settings()
    return HulyClient(
        url=s.huly_url, workspace=s.huly_workspace, email=s.huly_email,
        password=s.huly_password, token=s.huly_token, node_bin=s.node_bin,
        cache=_cache(), cache_ttl=s.huly_cache_ttl,
    )


def _outline() -> OutlineClient:
    s = get_settings()
    return OutlineClient(
        s.outline_url, s.outline_token, cache=_cache(), cache_ttl=s.outline_cache_ttl,
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

        # Fetch repos concurrently (network-bound); write to SQLite serially after.
        sem = asyncio.Semaphore(8)

        async def fetch(name: str):
            async with sem:
                return name, await gh.repo_activity(name, since)

        results = await asyncio.gather(*(fetch(n) for n in names))
        for name, activity in results:
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
    digest: ActivityDigest = asyncio.run(summarize(s.bulk_model, activities, period))
    _print_digest(digest)


@app.command()
def ask(
    question: str = typer.Argument(
        ..., help="Ask anything — about a person, a project, a topic, or the daily report."
    ),
    repo: str = typer.Option(None, "--repo", help="Optional: pin the investigation to one repo."),
) -> None:
    """Ask anything; the agent investigates across repos, PRs, Huly tasks, docs, and people."""
    s = get_settings()

    async def _run() -> str:
        async with AsyncExitStack() as stack:
            gh = await stack.enter_async_context(_github())
            outline = (
                await stack.enter_async_context(_outline())
                if s.outline_enabled else None
            )
            huly = await stack.enter_async_context(_huly()) if s.huly_enabled else None
            team = load_team(s.team_path)
            return await ask_anything(
                s.model, question, gh, settings=s, team=team,
                huly=huly, outline=outline, repo_hint=repo,
            )

    try:
        answer = asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)
    console.print(Panel(Markdown(answer), title="Answer", border_style="cyan"))


@app.command()
def chat(
    repo: str = typer.Option(None, "--repo", help="Optional: focus the session on one repo."),
) -> None:
    """Interactive session — ask follow-up questions with the conversation remembered.

    Commands: 'exit'/'quit' to leave, '/reset' to clear the conversation.
    """
    s = get_settings()

    async def _run() -> None:
        async with AsyncExitStack() as stack:
            gh = await stack.enter_async_context(_github())
            outline = (
                await stack.enter_async_context(_outline())
                if s.outline_enabled else None
            )
            huly = await stack.enter_async_context(_huly()) if s.huly_enabled else None
            agent = build_assistant(s.model)
            deps = AssistantDeps(
                github=gh, settings=s, team=load_team(s.team_path), huly=huly, outline=outline
            )
            history: list = []
            loop = asyncio.get_event_loop()

            console.print(Panel(
                "Interactive chat. Ask about people, projects, tasks, docs, or the daily report.\n"
                "Follow-ups keep context — say \"go deeper on that\". "
                "[dim]exit/quit to leave · /reset to clear history[/dim]",
                title="daily-agent chat", border_style="cyan",
            ))
            first = True
            while True:
                try:
                    user = await loop.run_in_executor(
                        None, lambda: console.input("[bold green]you ›[/bold green] ")
                    )
                except (EOFError, KeyboardInterrupt):
                    break
                user = user.strip()
                if not user:
                    continue
                if user.lower() in ("exit", "quit", ":q"):
                    break
                if user.lower() in ("/reset", "reset"):
                    history, first = [], True
                    console.print("[dim]history cleared[/dim]")
                    continue
                if first and repo:
                    user = f"(Focus on the repo: {repo})\n\n{user}"
                first = False
                try:
                    with console.status("[dim]thinking…[/dim]"):
                        result = await agent.run(user, deps=deps, message_history=history)
                    history = result.all_messages()
                    console.print(Panel(Markdown(result.output), border_style="cyan"))
                except Exception as e:  # noqa: BLE001 - keep the session alive on errors
                    console.print(f"[red]Error:[/red] {type(e).__name__}: {e}")
            console.print("[dim]bye[/dim]")

    try:
        asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)


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
        async with _outline() as ol:
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
        async with _outline() as ol:
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
    status: str = typer.Option(None, "--status", help="Filter by status name, e.g. 'In Review'."),
    assignee: str = typer.Option(None, "--assignee", help="Filter by assignee name (substring match)."),
    priority: str = typer.Option(None, "--priority", help="Filter by priority: none|urgent|high|medium|low."),
    projects: bool = typer.Option(False, "--projects", "-p", help="List projects instead of issues."),
) -> None:
    """List Huly issues for a project (defaults to the configured project)."""
    s = get_settings()
    if not s.huly_enabled:
        console.print("[red]Huly not configured[/red] (set DAILY_AGENT_HULY_WORKSPACE + creds).")
        raise typer.Exit(1)
    target = project or s.huly_default_project

    # Resolve an assignee name/handle (e.g. "me", "harshit") to a Huly name.
    if assignee:
        member = resolve_member(load_team(s.team_path), assignee, me=s.me)
        if member:
            assignee = member.huly

    async def _run():
        async with _huly() as h:
            if projects or not target:
                return ("projects", await h.projects())
            return ("issues", await h.issues(
                target, limit=limit, status=status, assignee=assignee, priority=priority
            ))

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
    active = [f"{k}={v}" for k, v in
              (("status", status), ("assignee", assignee), ("priority", priority)) if v]
    suffix = f" [dim](filters: {', '.join(active)})[/dim]" if active else ""
    console.print(f"[bold]{len(rows)} issues in {target}:[/bold]{suffix}")
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


@app.command()
def daily(
    days: int = typer.Option(None, help="Window in days (default from config lookback_days)."),
    people: bool = typer.Option(True, help="Include per-person briefs for active contributors."),
) -> None:
    """Full daily job: collect -> cross-project digest -> per-person briefs -> Markdown file."""
    s = get_settings()
    window = days if days is not None else s.lookback_days
    since = _since(window)
    date_str = datetime.now(timezone.utc).date().isoformat()

    async def _impl():
        # 1. Collect recent activity into the store.
        await _collect(window)

        # 2. Cross-project summary (fast model).
        store = Store(s.db_path)
        activities = store.activity_since(since)
        digest = await summarize(s.bulk_model, activities, f"last {window} day(s)")

        # 3. Per-person briefs for active contributors — run concurrently.
        briefs: list = []
        if people:
            authors = {pr.author for a in activities for pr in a.pull_requests}
            team = load_team(s.team_path)
            active = [m for m in team.values() if m.github in authors]

            tasks_by_assignee: dict[str, list[dict]] = {}
            if s.huly_enabled and active:
                try:
                    async with _huly() as h:
                        issues = await h.issues(s.huly_default_project or None, limit=200)
                    for i in issues:
                        if (i.get("modifiedOn") or "") >= since.isoformat():
                            tasks_by_assignee.setdefault(i.get("assignee"), []).append(i)
                except HulyError as e:
                    console.print(f"[yellow]Huly unavailable for briefs: {e}[/yellow]")

            sem = asyncio.Semaphore(5)

            async def one(m):
                async with sem:
                    person_prs = [
                        pr for a in activities for pr in a.pull_requests if pr.author == m.github
                    ]
                    try:
                        pb = await summarize_person(
                            s.bulk_model, m.name, person_prs, tasks_by_assignee.get(m.huly, [])
                        )
                        return (m, pb)
                    except Exception as e:  # one bad brief shouldn't sink the digest
                        console.print(f"[yellow]Brief failed for {m.name} ({type(e).__name__})[/yellow]")
                        return None

            briefs = [r for r in await asyncio.gather(*(one(m) for m in active)) if r]
        return digest, briefs

    try:
        digest, briefs = asyncio.run(_impl())
    except GitHubError as e:
        console.print(f"[red]GitHub error during daily:[/red] {e}")
        raise typer.Exit(1)

    # 4. Render + deliver.
    content = render_markdown(date_str, digest, briefs)
    path = write_file(content, s.digest_dir, date_str)
    console.print(
        f"[green]Wrote digest[/green] {path}  "
        f"({len(digest.projects)} projects, {len(briefs)} people)"
    )


@app.command()
def brief(
    person: str = typer.Argument(None, help="Person name/handle. Omit for 'me'."),
    days: int = typer.Option(7, help="Look back this many days ('this week')."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip the LLM summary; just list tasks/PRs."),
) -> None:
    """What someone is working on lately: a synthesized briefing + their tasks/PRs."""
    s = get_settings()
    team = load_team(s.team_path)
    if not team:
        console.print(
            f"[red]No team map[/red] at {s.team_path}. Copy team.example.json to team.json."
        )
        raise typer.Exit(1)
    member = resolve_member(team, person or "me", me=s.me)
    if member is None:
        who = person or "me"
        hint = "set DAILY_AGENT_ME" if who == "me" else "known: " + ", ".join(team)
        console.print(f"[red]Couldn't resolve '{who}'[/red] ({hint}).")
        raise typer.Exit(1)

    since = _since(days)
    since_iso = since.isoformat()

    async def _run():
        huly_issues: list[dict] = []
        if s.huly_enabled:
            async with _huly() as h:
                issues = await h.issues(
                    s.huly_default_project or None, assignee=member.huly, limit=100
                )
            huly_issues = [i for i in issues if (i.get("modifiedOn") or "") >= since_iso]
        prs: list = []
        if member.github:
            async with _github() as gh:
                prs = await gh.search_pull_requests(member.github, since, limit=50)
        return huly_issues, prs

    try:
        huly_issues, prs = asyncio.run(_run())
    except (HulyError, GitHubError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    title = f"{member.name} — last {days} days  [dim](huly: {member.huly} · gh: {member.github})[/dim]"
    console.print(Panel(title, border_style="magenta"))

    # Lead with a synthesized briefing (unless --no-ai or there's nothing to summarize).
    if not no_ai and (huly_issues or prs):
        from .agents.person_brief import summarize_person

        try:
            pb = asyncio.run(summarize_person(s.bulk_model, member.name, prs, huly_issues))
            body = f"**{pb.headline}**\n\n{pb.summary}"
            if pb.themes:
                body += "\n\n" + "\n".join(f"- {t}" for t in pb.themes)
            console.print(Panel(Markdown(body), title="Summary", border_style="green"))
        except Exception as e:  # model/transport hiccup — don't lose the listing below
            console.print(f"[yellow]Summary unavailable ({type(e).__name__}); showing details only.[/yellow]")

    console.print(f"[bold]Huly tasks ({len(huly_issues)}):[/bold]")
    for i in huly_issues:
        console.print(
            f"  • [cyan]{i['identifier']}[/cyan] [{i['status']}] {i['title']}"
            f"  [dim]({i['priority']})[/dim]"
        )
    if not huly_issues:
        console.print("  [dim](none updated in window)[/dim]")

    console.print(f"\n[bold]GitHub PRs ({len(prs)}):[/bold]")
    for pr in prs:
        state = "merged" if pr.merged else pr.state
        console.print(f"  • [cyan]{pr.repo}#{pr.number}[/cyan] [{state}] {pr.title}")
        console.print(f"    [dim]{pr.url}[/dim]")
    if not prs:
        console.print("  [dim](no PRs in window)[/dim]")


_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def _github_pr_links(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PR_URL_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


@app.command()
def cache(
    clear: bool = typer.Option(False, "--clear", help="Delete all cached entries."),
) -> None:
    """Inspect or clear the response cache."""
    s = get_settings()
    c = Cache(s.db_path, enabled=True)
    if clear:
        n = c.clear()
        console.print(f"[green]Cleared[/green] {n} cached entries.")
        return
    total, perm = c.stats()
    console.print(
        f"Cache: [bold]{total}[/bold] entries ([bold]{perm}[/bold] permanent — "
        f"merged PRs / DONE issues), {total - perm} on TTL. "
        f"{'enabled' if s.cache_enabled else 'DISABLED'} in config."
    )


@app.command()
def feed(
    days: int = typer.Option(7, help="Build bites from activity in the last N days."),
    to_file: str = typer.Option(
        None, "--to-file", help="Append bites to this file instead of the console."
    ),
    limit: int = typer.Option(None, help="Deliver at most N bites this run."),
    status: bool = typer.Option(False, "--status", help="Show outbox stats and exit."),
) -> None:
    """Turn accumulated activity into deduped bites and deliver them.

    Idempotent: enqueuing re-derives stable keys and the outbox skips anything
    already queued or delivered, so running `feed` repeatedly never repeats a
    bite. This is the channel-agnostic Phase 1 core (console / file channels).
    """
    s = get_settings()
    outbox = Outbox(s.db_path)
    if status:
        st = outbox.stats()
        console.print(
            f"Outbox: [bold]{st['pending']}[/bold] pending, {st['failed']} retrying, "
            f"[green]{st['sent']}[/green] sent, [red]{st['dead']}[/red] dead — "
            f"{st['delivered']} in the delivered ledger."
        )
        return

    store = Store(s.db_path)
    activities = store.activity_since(_since(days))
    new = outbox.enqueue_all(bites_for_activity(activities))

    channel: Channel = FileChannel(to_file) if to_file else ConsoleChannel(console)
    result = outbox.drain(channel, limit=limit)

    dest = to_file if to_file else "console"
    console.print(
        f"[green]Feed[/green] queued {new} new bite(s); delivered "
        f"[bold]{result.sent}[/bold] to {dest}"
        + (f", {result.failed} deferred" if result.failed else "")
        + (f", [red]{result.dead} dead[/red]" if result.dead else "")
        + "."
    )


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
