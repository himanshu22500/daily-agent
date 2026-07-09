"""Command-line interface.

daily-agent collect            Gather recent repo activity into the store.
daily-agent summary            Summarize accumulated activity into a digest.
daily-agent ask REPO "..."     Deep-dive into one project (business logic).
daily-agent repos              List the org repos currently being watched.
daily-agent feed               Deliver accumulated activity as deduped bites.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from .agents.insight_extractor import extract_insights
from .agents.person_brief import summarize_person
from .agents.summarizer import summarize
from .cache import Cache
from .deliver import render_markdown, write_file
from .config import get_settings
from .feed.channel_registry import ChannelRegistry, reap_stale
from .feed.channels import (
    ConsoleChannel,
    FileChannel,
    MultiStreamTelegramChannel,
    SlackChannel,
    SlackError,
    TelegramChannel,
    TelegramError,
)
from .feed.delta import bites_for_activity
from .feed.initiatives_store import InitiativeStore
from .feed.insights_capture import collect_insights, collect_marked
from .feed.insights_feed import (
    INSIGHT_KIND,
    enqueue_new_insights,
    insight_stream_resolver,
)
from .feed.insights_store import InsightStore
from .feed.listener import FollowUp, Listener, ListenerStore, TelegramUpdates
from .feed.outbox import Channel, Outbox
from .feed.pacer import Pacer
from .feed.storyteller import chapters_to_bites, render_chapters
from .models import ActivityDigest
from .sources.github import GitHubClient, GitHubError
from .sources.github_projects import GitHubProjectsClient, GitHubProjectsError
from .sources.outline import OutlineClient, OutlineError
from .sources.telegram_provision import TelethonProvisioner
from .storage import Store
from .team import load_team, resolve_member

app = typer.Typer(
    add_completion=False,
    help="AI agents that watch your org's repos and summarize what's being worked on.",
)
insights_app = typer.Typer(
    no_args_is_help=True,
    help="Personal insight feed — capture + recall from Claude Code sessions.",
)
app.add_typer(insights_app, name="insights")
console = Console()


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _cache() -> Cache:
    s = get_settings()
    return Cache(s.db_path, enabled=s.cache_enabled)


def _github() -> GitHubClient:
    s = get_settings()
    return GitHubClient(
        token=s.github_token,
        org=s.github_org,
        cache=_cache(),
        cache_ttl=s.github_cache_ttl,
    )


def _projects() -> GitHubProjectsClient:
    s = get_settings()
    return GitHubProjectsClient(
        token=s.project_token,
        owner=s.project_owner,
        number=s.github_project_number,
        cache=_cache(),
        cache_ttl=s.projects_cache_ttl,
    )


def _outline() -> OutlineClient:
    s = get_settings()
    return OutlineClient(
        s.outline_url,
        s.outline_token,
        cache=_cache(),
        cache_ttl=s.outline_cache_ttl,
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
    days: int = typer.Option(
        None, help="Lookback window in days (default from config)."
    ),
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
        ...,
        help="Ask anything — about a person, a project, a topic, or the daily report.",
    ),
    repo: str = typer.Option(
        None, "--repo", help="Optional: pin the investigation to one repo."
    ),
) -> None:
    """Ask anything; the agent investigates across repos, PRs, docs, and people."""
    s = get_settings()

    async def _run() -> str:
        async with AsyncExitStack() as stack:
            gh = await stack.enter_async_context(_github())
            outline = (
                await stack.enter_async_context(_outline())
                if s.outline_enabled
                else None
            )
            team = load_team(s.team_path)
            return await ask_anything(
                s.model,
                question,
                gh,
                settings=s,
                team=team,
                outline=outline,
                repo_hint=repo,
            )

    try:
        answer = asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]GitHub error:[/red] {e}")
        raise typer.Exit(1)
    console.print(Panel(Markdown(answer), title="Answer", border_style="cyan"))


@app.command()
def chat(
    repo: str = typer.Option(
        None, "--repo", help="Optional: focus the session on one repo."
    ),
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
                if s.outline_enabled
                else None
            )
            agent = build_assistant(s.model)
            deps = AssistantDeps(
                github=gh,
                settings=s,
                team=load_team(s.team_path),
                outline=outline,
            )
            history: list = []
            loop = asyncio.get_event_loop()

            console.print(
                Panel(
                    "Interactive chat. Ask about people, projects, tasks, docs, or the daily report.\n"
                    'Follow-ups keep context — say "go deeper on that". '
                    "[dim]exit/quit to leave · /reset to clear history[/dim]",
                    title="daily-agent chat",
                    border_style="cyan",
                )
            )
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
                        result = await agent.run(
                            user, deps=deps, message_history=history
                        )
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
        console.print(
            "[red]Outline not configured[/red] (set DAILY_AGENT_OUTLINE_URL/TOKEN)."
        )
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
    question: str = typer.Argument(
        ..., help="A how-to / setup / 'how does X work' question."
    ),
) -> None:
    """Answer a question from your Outline docs — finds, reads, and synthesizes steps."""
    s = get_settings()
    if not s.outline_enabled:
        console.print(
            "[red]Outline not configured[/red] (set DAILY_AGENT_OUTLINE_URL/TOKEN)."
        )
        raise typer.Exit(1)

    async def _run() -> str:
        async with _outline() as ol:
            return await ask_docs(s.model, ol, question)

    try:
        answer = asyncio.run(_run())
    except OutlineError as e:
        console.print(f"[red]Outline error:[/red] {e}")
        raise typer.Exit(1)
    console.print(
        Panel(Markdown(answer), title="From the docs", border_style="magenta")
    )


@app.command()
def daily(
    days: int = typer.Option(
        None, help="Window in days (default from config lookback_days)."
    ),
    people: bool = typer.Option(
        True, help="Include per-person briefs for active contributors."
    ),
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

            sem = asyncio.Semaphore(5)

            async def one(m):
                async with sem:
                    person_prs = [
                        pr
                        for a in activities
                        for pr in a.pull_requests
                        if pr.author == m.github
                    ]
                    try:
                        pb = await summarize_person(
                            s.bulk_model,
                            m.name,
                            person_prs,
                            [],
                        )
                        return (m, pb)
                    except Exception as e:  # one bad brief shouldn't sink the digest
                        console.print(
                            f"[yellow]Brief failed for {m.name} ({type(e).__name__})[/yellow]"
                        )
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
    no_ai: bool = typer.Option(
        False, "--no-ai", help="Skip the LLM summary; just list PRs."
    ),
) -> None:
    """What someone is working on lately: a synthesized briefing + their PRs."""
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

    async def _run():
        prs: list = []
        if member.github:
            async with _github() as gh:
                prs = await gh.search_pull_requests(member.github, since, limit=50)
        return prs

    try:
        prs = asyncio.run(_run())
    except GitHubError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    title = f"{member.name} — last {days} days  [dim](gh: {member.github})[/dim]"
    console.print(Panel(title, border_style="magenta"))

    # Lead with a synthesized briefing (unless --no-ai or there's nothing to summarize).
    if not no_ai and prs:
        from .agents.person_brief import summarize_person

        try:
            pb = asyncio.run(summarize_person(s.bulk_model, member.name, prs, []))
            body = f"**{pb.headline}**\n\n{pb.summary}"
            if pb.themes:
                body += "\n\n" + "\n".join(f"- {t}" for t in pb.themes)
            console.print(Panel(Markdown(body), title="Summary", border_style="green"))
        except Exception as e:  # model/transport hiccup — don't lose the listing below
            console.print(
                f"[yellow]Summary unavailable ({type(e).__name__}); showing details only.[/yellow]"
            )

    console.print(f"[bold]GitHub PRs ({len(prs)}):[/bold]")
    for pr in prs:
        state = "merged" if pr.merged else pr.state
        console.print(f"  • [cyan]{pr.repo}#{pr.number}[/cyan] [{state}] {pr.title}")
        console.print(f"    [dim]{pr.url}[/dim]")
    if not prs:
        console.print("  [dim](no PRs in window)[/dim]")


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
    to_slack: bool = typer.Option(
        False,
        "--to-slack",
        help="Deliver bites to Slack (uses configured bot token + destination).",
    ),
    to_telegram: bool = typer.Option(
        False,
        "--to-telegram",
        help="Deliver bites to Telegram (uses configured bot token + chat ID).",
    ),
    to_file: str = typer.Option(
        None, "--to-file", help="Append bites to this file instead of the console."
    ),
    limit: int = typer.Option(None, help="Deliver at most N bites this run."),
    status: bool = typer.Option(False, "--status", help="Show outbox stats and exit."),
) -> None:
    """Turn accumulated activity into deduped bites and deliver them.

    Idempotent: enqueuing re-derives stable keys and the outbox skips anything
    already queued or delivered, so running `feed` repeatedly never repeats a
    bite. Delivers to the console by default; `--to-slack` / `--to-telegram` push
    them to you, `--to-file` appends them to a transcript.

    Paced by default (DAILY_AGENT_FEED_MAX_PER_RUN + quiet hours): run it
    periodically and the backlog trickles out. `--limit` overrides the pacer.
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

    # An explicit --to-* flag wins; otherwise fall back to the configured
    # default channel (DAILY_AGENT_FEED_CHANNEL).
    if to_slack:
        choice = "slack"
    elif to_telegram:
        choice = "telegram"
    elif to_file:
        choice = "file"
    else:
        choice = (s.feed_channel or "console").lower()

    if choice == "slack" and not s.slack_enabled:
        console.print(
            "[red]Slack not configured.[/red] Set DAILY_AGENT_SLACK_BOT_TOKEN and "
            "DAILY_AGENT_SLACK_DESTINATION, then run `daily-agent slack-check`."
        )
        raise typer.Exit(1)
    if choice == "telegram" and not s.telegram_enabled:
        console.print(
            "[red]Telegram not configured.[/red] Set DAILY_AGENT_TELEGRAM_BOT_TOKEN "
            "and DAILY_AGENT_TELEGRAM_CHAT_ID, then run `daily-agent telegram-check`."
        )
        raise typer.Exit(1)

    prs = [
        pr
        for a in Store(s.db_path).activity_since(_since(days))
        for pr in a.pull_requests
    ]
    if s.projects_enabled and prs:
        # Rich feed: PRs → initiatives → plain-language storyline chapters.
        async def _build():
            async with _projects() as projects:
                issues = await projects.issues(limit=500)
            return await chapters_to_bites(
                s.model, prs, issues, InitiativeStore(s.db_path), cache=_cache()
            )

        try:
            bites = asyncio.run(_build())
        except GitHubProjectsError as e:
            console.print(f"[red]GitHub Projects error:[/red] {e}")
            raise typer.Exit(1)
    else:
        # Fallback without GitHub Projects: mechanical per-PR bites.
        bites = bites_for_activity(Store(s.db_path).activity_since(_since(days)))
    new = outbox.enqueue_all(bites)

    # Cadence: an explicit --limit overrides the pacer; otherwise the pacer caps
    # how many to release now and stays silent during quiet hours.
    pacer = Pacer(s.feed_max_per_run, s.feed_quiet_start, s.feed_quiet_end)
    allow = limit if limit is not None else pacer.allowance(datetime.now())
    if allow == 0:
        held = outbox.stats()["pending"]
        console.print(
            f"[yellow]Quiet hours[/yellow] — queued {new} new bite(s); "
            f"holding {held} for later (none delivered)."
        )
        return

    if choice == "slack":
        channel: Channel = SlackChannel(s.slack_bot_token, s.slack_destination)
        dest = "Slack"
    elif choice == "telegram" and s.feed_multi_stream and s.telegram_mtproto_enabled:
        registry = ChannelRegistry(s.db_path)
        provisioner = TelethonProvisioner(
            api_id=s.telegram_api_id,
            api_hash=s.telegram_api_hash,
            session=s.telegram_session,
            bot_username=s.telegram_bot_username,
        )
        channel = MultiStreamTelegramChannel(
            registry,
            provisioner,
            bot_factory=lambda cid: TelegramChannel(s.telegram_bot_token, str(cid)),
        )
        dest = "Telegram (multi-stream)"
    elif choice == "telegram":
        channel = TelegramChannel(s.telegram_bot_token, s.telegram_chat_id)
        dest = "Telegram"
    elif choice == "file":
        path = to_file or f"{s.digest_dir}/feed.log"
        channel = FileChannel(path)
        dest = path
    else:
        channel = ConsoleChannel(console)
        dest = "console"

    try:
        result = outbox.drain(channel, limit=allow, exclude_kind=INSIGHT_KIND)
    finally:
        if hasattr(channel, "close"):
            channel.close()

    held = outbox.stats()["pending"]
    console.print(
        f"[green]Feed[/green] queued {new} new bite(s); delivered "
        f"[bold]{result.sent}[/bold] to {dest}"
        + (f", {result.failed} deferred" if result.failed else "")
        + (f", [red]{result.dead} dead[/red]" if result.dead else "")
        + (f"; [dim]{held} still queued (paced)[/dim]" if held else "")
        + "."
    )


@insights_app.command("feed")
def insights_feed(
    to_telegram: bool = typer.Option(
        False,
        "--to-telegram",
        help="Deliver insight bites to per-type Telegram channels.",
    ),
    to_file: str = typer.Option(
        None,
        "--to-file",
        help="Append insight bites to this file instead of the console.",
    ),
    limit: int = typer.Option(None, help="Deliver at most N insight bites this run."),
) -> None:
    """Queue captured insights and trickle them through the outbox."""
    s = get_settings()
    store = InsightStore(s.db_path)
    outbox = Outbox(s.db_path)
    queued = enqueue_new_insights(store, outbox)

    pacer = Pacer(s.insights_feed_max_per_run, s.feed_quiet_start, s.feed_quiet_end)
    allow = limit if limit is not None else pacer.allowance(datetime.now())
    if allow == 0:
        held = outbox.stats()["pending"]
        console.print(
            f"[yellow]Quiet hours[/yellow] — queued {queued} new insight bite(s); "
            f"holding {held} pending bite(s) for later."
        )
        return

    if to_telegram:
        if not s.telegram_bot_token:
            console.print(
                "[red]Telegram bot not configured.[/red] Set "
                "DAILY_AGENT_TELEGRAM_BOT_TOKEN."
            )
            raise typer.Exit(1)
        if not s.telegram_mtproto_enabled:
            console.print(
                "[red]Telegram MTProto not configured.[/red] Per-type insight "
                "channels require DAILY_AGENT_TELEGRAM_API_ID / _API_HASH / "
                "_BOT_USERNAME + `daily-agent telegram-auth`."
            )
            raise typer.Exit(1)
        registry = ChannelRegistry(s.db_path)
        provisioner = TelethonProvisioner(
            api_id=s.telegram_api_id,
            api_hash=s.telegram_api_hash,
            session=s.telegram_session,
            bot_username=s.telegram_bot_username,
        )
        channel: Channel = MultiStreamTelegramChannel(
            registry,
            provisioner,
            bot_factory=lambda cid: TelegramChannel(s.telegram_bot_token, str(cid)),
            resolver=insight_stream_resolver(store),
        )
        dest = "Telegram (per-type channels)"
    elif to_file:
        channel = FileChannel(to_file)
        dest = to_file
    else:
        channel = ConsoleChannel(console)
        dest = "console"

    try:
        result = outbox.drain(channel, limit=allow, kind=INSIGHT_KIND)
    finally:
        if hasattr(channel, "close"):
            channel.close()

    held = outbox.stats()["pending"]
    console.print(
        f"[green]Insight feed[/green] queued {queued} new bite(s); delivered "
        f"[bold]{result.sent}[/bold] to {dest}"
        + (f", {result.failed} deferred" if result.failed else "")
        + (f", [red]{result.dead} dead[/red]" if result.dead else "")
        + (f"; [dim]{held} pending bite(s) remain[/dim]" if held else "")
        + "."
    )


@app.command(name="feed-preview")
def feed_preview(
    days: int = typer.Option(7, help="Use PR activity from the last N days."),
    limit: int = typer.Option(5, help="Render at most N initiative chapters."),
) -> None:
    """Dry-run the rich feed: render initiative chapters to the console.

    Maps PRs → initiatives, then writes a plain-language chapter per initiative.
    Read-only — nothing is persisted or delivered, so it's safe to react to the
    tone/length before wiring chapters into the live feed.
    """
    s = get_settings()
    if not s.projects_enabled:
        console.print(
            "[red]GitHub Projects not configured.[/red] Set "
            "DAILY_AGENT_GITHUB_PROJECT_NUMBER (and an owner + a token with the "
            "read:project scope) to preview initiative chapters."
        )
        raise typer.Exit(1)

    async def _run():
        async with _projects() as projects:
            issues = await projects.issues(limit=500)
        prs = [
            pr
            for a in Store(s.db_path).activity_since(_since(days))
            for pr in a.pull_requests
        ]
        if not prs:
            return [], 0
        store = InitiativeStore(s.db_path)
        chapters = await render_chapters(s.model, prs, issues, store=store, limit=limit)
        return chapters, len(prs)

    try:
        chapters, n_prs = asyncio.run(_run())
    except GitHubProjectsError as e:
        console.print(f"[red]GitHub Projects error:[/red] {e}")
        raise typer.Exit(1)
    if not chapters:
        console.print(
            "[yellow]No PR activity in the window. Run `collect` first.[/yellow]"
        )
        return
    console.print(
        f"[dim]Rendered top {len(chapters)} initiatives from {n_prs} PRs (last {days}d):[/dim]\n"
    )
    for rc in chapters:
        console.print(Panel(rc.content, border_style="cyan"))


@insights_app.command("collect")
def insights_collect(
    extract: bool = typer.Option(
        True,
        "--extract/--no-extract",
        help="Run the LLM extraction lane in addition to explicit markers.",
    ),
) -> None:
    """Capture insights from local Claude Code transcripts into the store.

    Scans this project's `*.jsonl` transcripts for messages containing the marker
    (DAILY_AGENT_INSIGHTS_MARKER, default `insight:`) and, by default, also asks
    the extraction agent for durable unmarked insights. Only records appended
    since the last run are read (a per-file watermark).
    """
    s = get_settings()
    path = s.transcripts_path
    if not Path(path).exists():
        console.print(
            f"[yellow]No transcripts found[/yellow] at {path}. "
            "Set DAILY_AGENT_INSIGHTS_TRANSCRIPTS_DIR if they live elsewhere."
        )
        raise typer.Exit(1)
    store = InsightStore(s.db_path)
    if extract:
        result = asyncio.run(
            collect_insights(
                store,
                path,
                s.insights_marker,
                lambda messages: extract_insights(s.model, messages),
            )
        )
        console.print(
            f"[green]Insights[/green] captured [bold]{result.new}[/bold] new "
            f"({result.marked} marker, {result.extracted} extracted) "
            f"from {result.scanned} new record(s) in {path}"
        )
        return

    new, scanned = collect_marked(store, path, s.insights_marker)
    console.print(
        f"[green]Insights[/green] captured [bold]{new}[/bold] new "
        f"(marker '{s.insights_marker}', extraction disabled) "
        f"from {scanned} new record(s) in {path}"
    )


@app.command(name="slack-check")
def slack_check() -> None:
    """Send a test DM to confirm the Slack bot token + destination work."""
    s = get_settings()
    if not s.slack_enabled:
        console.print(
            "[red]Slack not configured.[/red] Set DAILY_AGENT_SLACK_BOT_TOKEN "
            "(xoxb-… with chat:write) and DAILY_AGENT_SLACK_DESTINATION (your "
            "Slack user ID to DM, or a channel ID)."
        )
        raise typer.Exit(1)
    channel = SlackChannel(s.slack_bot_token, s.slack_destination)
    try:
        channel.send_text(
            ":wave: daily-agent is connected — your feed will arrive here."
        )
    except SlackError as e:
        console.print(
            f"[red]Slack rejected the message:[/red] {e}\n"
            "Common fixes: invalid/expired token, missing chat:write scope, or the "
            "destination ID is wrong (DM = your user ID, not a channel name)."
        )
        raise typer.Exit(1)
    finally:
        channel.close()
    console.print(
        f"[green]Sent[/green] a test message to {s.slack_destination}. Check Slack."
    )


@app.command(name="telegram-check")
def telegram_check() -> None:
    """Send a test message to confirm the Telegram bot token + chat ID work."""
    s = get_settings()
    if not s.telegram_enabled:
        console.print(
            "[red]Telegram not configured.[/red] Set DAILY_AGENT_TELEGRAM_BOT_TOKEN "
            "(from @BotFather) and DAILY_AGENT_TELEGRAM_CHAT_ID (your numeric ID — "
            "send the bot /start, then get the ID from @userinfobot)."
        )
        raise typer.Exit(1)
    channel = TelegramChannel(s.telegram_bot_token, s.telegram_chat_id)
    try:
        channel.send_text("👋 daily-agent is connected — your feed will arrive here.")
    except TelegramError as e:
        console.print(
            f"[red]Telegram rejected the message:[/red] {e}\n"
            "Common fixes: wrong token, wrong chat ID, or you haven't sent the bot "
            "/start yet (bots can't message you until you start the chat)."
        )
        raise typer.Exit(1)
    finally:
        channel.close()
    console.print(
        f"[green]Sent[/green] a test message to chat {s.telegram_chat_id}. Check Telegram."
    )


@app.command(name="telegram-listen")
def telegram_listen() -> None:
    """Long-poll Telegram for replies to feed bites (inbound follow-ups).

    The one persistent process: it watches every channel the bot posts to and,
    when you reply to a bite, identifies that follow-up against the messages we
    sent. Run it under launchd (`scripts/install-listen-launchd.sh`) so it stays
    up. Phase 2 identifies + logs the follow-up; grounding an answer and posting
    it threaded back land in later phases.
    """
    s = get_settings()
    if not s.telegram_enabled:
        console.print(
            "[red]Telegram not configured.[/red] Set DAILY_AGENT_TELEGRAM_BOT_TOKEN "
            "and DAILY_AGENT_TELEGRAM_CHAT_ID, then run `daily-agent telegram-check`."
        )
        raise typer.Exit(1)

    outbox = Outbox(s.db_path)
    updates = TelegramUpdates(s.telegram_bot_token)

    def handler(f: FollowUp) -> None:
        console.print(
            f"[cyan]Follow-up[/cyan] on [bold]{f.subject}[/bold]: {f.text!r} "
            f"(reply to bite {f.dedup_key})"
        )

    listener = Listener(
        updates,
        ListenerStore(s.db_path),
        outbox.sent_message,
        handler,
        on_event=lambda m: console.print(f"[yellow]listener[/yellow] {m}"),
    )
    console.print(
        "[green]Listening[/green] for Telegram follow-ups — reply to a bite in the "
        "feed channel. Ctrl-C to stop."
    )
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        console.print("\nStopped.")
    finally:
        updates.close()


@app.command(name="telegram-auth")
def telegram_auth() -> None:
    """One-time interactive login for MTProto (multi-stream channel creation).

    Run this in your own terminal (prefix with `! ` in the agent) so you can type
    the login code. Creates/authorizes the session file used to auto-create
    Telegram channels. Needs DAILY_AGENT_TELEGRAM_API_ID / _API_HASH (from
    my.telegram.org). Requires the optional dep: `uv sync --extra telegram`.
    """
    s = get_settings()
    if not (s.telegram_api_id and s.telegram_api_hash):
        console.print(
            "[red]MTProto not configured.[/red] Set DAILY_AGENT_TELEGRAM_API_ID and "
            "DAILY_AGENT_TELEGRAM_API_HASH (from my.telegram.org → API development tools)."
        )
        raise typer.Exit(1)
    try:
        from telethon.sync import TelegramClient
    except ModuleNotFoundError:
        console.print(
            "[red]Telethon not installed.[/red] Run: uv sync --extra telegram"
        )
        raise typer.Exit(1)

    console.print(
        "Starting Telegram login — enter your phone, the code, and 2FA if set."
    )
    client = TelegramClient(
        s.telegram_session, int(s.telegram_api_id), s.telegram_api_hash
    )
    client.start()  # interactive: prompts on stdin
    me = client.get_me()
    client.disconnect()
    console.print(
        f"[green]Authorized[/green] as {me.first_name} "
        f"(@{me.username or me.id}). Session saved to {s.telegram_session}."
    )


@app.command(name="telegram-reap")
def telegram_reap(
    idle_days: int = typer.Option(
        None, help="Delete channels unused for at least N days (default from config)."
    ),
) -> None:
    """Delete stale auto-created channels (unused past the idle threshold)."""
    s = get_settings()
    if not s.telegram_mtproto_enabled:
        console.print(
            "[red]MTProto not configured.[/red] Multi-stream channels require "
            "DAILY_AGENT_TELEGRAM_API_ID / _API_HASH / _BOT_USERNAME + `telegram-auth`."
        )
        raise typer.Exit(1)
    days = idle_days if idle_days is not None else s.channel_reap_idle_days
    registry = ChannelRegistry(s.db_path)
    provisioner = TelethonProvisioner(
        api_id=s.telegram_api_id,
        api_hash=s.telegram_api_hash,
        session=s.telegram_session,
        bot_username=s.telegram_bot_username,
    )
    reaped = reap_stale(registry, provisioner, days)
    if reaped:
        console.print(
            f"[green]Reaped[/green] {len(reaped)} stale channel(s): {', '.join(reaped)}."
        )
    else:
        console.print(f"No channels idle for {days}+ days.")


def _print_digest(digest: ActivityDigest) -> None:
    console.print(
        Panel(
            digest.overview,
            title=f"Activity digest — {digest.period}",
            border_style="green",
        )
    )
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
