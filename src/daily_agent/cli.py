"""CLI for capturing and resurfacing personal coding insights."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import typer
from dotenv import load_dotenv

load_dotenv()

from rich.console import Console

from .agents.insight_extractor import extract_insights
from .config import get_settings
from .feed.channel_registry import ChannelRegistry, reap_stale
from .feed.channels import (
    ConsoleChannel,
    FileChannel,
    MultiStreamTelegramChannel,
    TelegramChannel,
    TelegramError,
)
from .feed.insights_capture import collect_insights, collect_marked
from .feed.insights_feed import (
    INSIGHT_KIND,
    enqueue_new_insights,
    insight_stream_resolver,
)
from .feed.insights_flush import (
    FlushPaths,
    RunLock,
    mark_ran,
    recently_ran,
    wait_for_quiet_path,
)
from .feed.insights_store import InsightStore
from .feed.outbox import Channel, Outbox
from .feed.pacer import Pacer
from .sources.telegram_provision import TelethonProvisioner

app = typer.Typer(
    add_completion=False,
    help="Capture durable insights from coding-agent transcripts and resurface them.",
)
insights_app = typer.Typer(no_args_is_help=True, help="Capture and resurface insights.")
app.add_typer(insights_app, name="insights")
console = Console()


def _delivery_channel(store: InsightStore, *, to_telegram: bool, to_file: str | None):
    settings = get_settings()
    if to_telegram:
        if not settings.telegram_bot_token or not settings.telegram_mtproto_enabled:
            console.print(
                "[red]Telegram not configured.[/red] Set the bot token, API ID, "
                "API hash, and bot username; then run `daily-agent telegram-auth`."
            )
            raise typer.Exit(1)
        channel: Channel = MultiStreamTelegramChannel(
            ChannelRegistry(settings.db_path),
            TelethonProvisioner(
                api_id=settings.telegram_api_id,
                api_hash=settings.telegram_api_hash,
                session=settings.telegram_session,
                bot_username=settings.telegram_bot_username,
            ),
            bot_factory=lambda channel_id: TelegramChannel(
                settings.telegram_bot_token, str(channel_id)
            ),
            resolver=insight_stream_resolver(store),
        )
        return channel, "Telegram (per-type channels)"
    if to_file:
        return FileChannel(to_file), to_file
    return ConsoleChannel(console), "console"


def _drain(
    store: InsightStore,
    *,
    queued: int,
    to_telegram: bool,
    to_file: str | None,
    limit: int | None,
    label: str,
) -> None:
    settings = get_settings()
    outbox = Outbox(settings.db_path)
    pacer = Pacer(
        settings.insights_feed_max_per_run,
        settings.feed_quiet_start,
        settings.feed_quiet_end,
    )
    allowance = limit if limit is not None else pacer.allowance(datetime.now())
    if allowance == 0:
        console.print(
            f"[yellow]Quiet hours[/yellow] — {label}; queued {queued} bite(s)."
        )
        return

    channel, destination = _delivery_channel(
        store, to_telegram=to_telegram, to_file=to_file
    )
    try:
        result = outbox.drain(channel, limit=allowance, kind=INSIGHT_KIND)
    finally:
        if hasattr(channel, "close"):
            channel.close()
    stats = outbox.stats()
    console.print(
        f"[green]{label}[/green]; queued {queued} bite(s); delivered "
        f"[bold]{result.sent}[/bold] to {destination}"
        + (f", {result.failed} deferred" if result.failed else "")
        + (f", [red]{result.dead} dead[/red]" if result.dead else "")
        + (
            f"; [dim]{stats['pending'] + stats['failed']} remain[/dim]"
            if stats["pending"] + stats["failed"]
            else ""
        )
        + "."
    )


@insights_app.command("collect")
def insights_collect(
    extract: bool = typer.Option(
        True,
        "--extract/--no-extract",
        help="Run LLM extraction in addition to explicit markers.",
    ),
) -> None:
    """Capture new insights from local coding-agent transcripts."""
    settings = get_settings()
    path = Path(settings.transcripts_path)
    if not path.exists():
        console.print(
            f"[yellow]No transcripts found[/yellow] at {path}. Set "
            "DAILY_AGENT_INSIGHTS_TRANSCRIPTS_DIR if they live elsewhere."
        )
        raise typer.Exit(1)
    store = InsightStore(settings.db_path)
    if extract:
        result = asyncio.run(
            collect_insights(
                store,
                path,
                settings.insights_marker,
                lambda messages: extract_insights(settings.model, messages),
            )
        )
        console.print(
            f"Captured [bold]{result.new}[/bold] new insight(s) "
            f"({result.marked} marker, {result.extracted} extracted) from "
            f"{result.scanned} new record(s)."
        )
        return
    new, scanned = collect_marked(store, path, settings.insights_marker)
    console.print(
        f"Captured [bold]{new}[/bold] marker insight(s) from {scanned} records."
    )


@insights_app.command("feed")
def insights_feed(
    to_telegram: bool = typer.Option(False, "--to-telegram"),
    to_file: str = typer.Option(None, "--to-file"),
    limit: int = typer.Option(None, help="Deliver at most N insights."),
) -> None:
    """Queue captured insights and deliver a paced batch."""
    settings = get_settings()
    store = InsightStore(settings.db_path)
    queued = enqueue_new_insights(store, Outbox(settings.db_path))
    _drain(
        store,
        queued=queued,
        to_telegram=to_telegram,
        to_file=to_file,
        limit=limit,
        label="Insight feed",
    )


@insights_app.command("flush")
def insights_flush(
    extract: bool = typer.Option(True, "--extract/--no-extract"),
    to_telegram: bool = typer.Option(False, "--to-telegram"),
    to_file: str = typer.Option(None, "--to-file"),
    limit: int = typer.Option(None, help="Deliver at most N insights."),
    quiet_seconds: int = typer.Option(30),
    debounce_seconds: int = typer.Option(20),
    lock_ttl_seconds: int = typer.Option(600),
) -> None:
    """Collect and deliver insights after transcript files become quiet."""
    settings = get_settings()
    path = Path(settings.transcripts_path)
    if not path.exists():
        console.print(f"[yellow]No transcripts found[/yellow] at {path}.")
        raise typer.Exit(1)
    paths = FlushPaths.for_db(settings.db_path)
    lock = RunLock(paths.lock, ttl_seconds=lock_ttl_seconds)
    if not lock.acquire():
        console.print("[yellow]Insight flush already running[/yellow] — skipping.")
        return
    try:
        if recently_ran(paths.stamp, debounce_seconds=debounce_seconds):
            console.print("[yellow]Insight flush debounced[/yellow] — skipping.")
            return
        if not wait_for_quiet_path(path, quiet_seconds=quiet_seconds):
            console.print("[yellow]Transcripts still changing[/yellow] — skipping.")
            return

        store = InsightStore(settings.db_path)
        if extract:
            captured = asyncio.run(
                collect_insights(
                    store,
                    path,
                    settings.insights_marker,
                    lambda messages: extract_insights(settings.model, messages),
                )
            )
            label = (
                f"Insight flush captured {captured.new} new "
                f"({captured.marked} marker, {captured.extracted} extracted) "
                f"from {captured.scanned} records"
            )
        else:
            new, scanned = collect_marked(store, path, settings.insights_marker)
            label = (
                f"Insight flush captured {new} marker insight(s) from {scanned} records"
            )
        queued = enqueue_new_insights(store, Outbox(settings.db_path))
        _drain(
            store,
            queued=queued,
            to_telegram=to_telegram,
            to_file=to_file,
            limit=limit,
            label=label,
        )
        mark_ran(paths.stamp)
    finally:
        lock.release()


@insights_app.command("status")
def insights_status() -> None:
    """Show captured-insight and delivery-queue counts."""
    settings = get_settings()
    store = InsightStore(settings.db_path)
    outbox = Outbox(settings.db_path)
    statuses: dict[str, int] = {}
    for insight in store.all():
        statuses[insight.status] = statuses.get(insight.status, 0) + 1
    console.print(f"Insights: {statuses or {'empty': 0}}")
    console.print(f"Outbox: {outbox.stats()}")


@app.command(name="telegram-check")
def telegram_check() -> None:
    """Send a test message to the configured Telegram chat."""
    settings = get_settings()
    if not settings.telegram_enabled:
        console.print("[red]Telegram bot token or chat ID is missing.[/red]")
        raise typer.Exit(1)
    channel = TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        channel.send_text("daily-agent insight delivery is connected.")
    except TelegramError as exc:
        console.print(f"[red]Telegram error:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        channel.close()
    console.print("[green]Telegram connected.[/green]")


@app.command(name="telegram-auth")
def telegram_auth() -> None:
    """Authorize the MTProto session used to create insight channels."""
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        console.print("[red]Telegram API ID or API hash is missing.[/red]")
        raise typer.Exit(1)
    from telethon.sync import TelegramClient

    client = TelegramClient(
        settings.telegram_session,
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
    )
    client.start()
    me = client.get_me()
    client.disconnect()
    console.print(
        f"[green]Authorized[/green] as {me.first_name} (@{me.username or me.id})."
    )


@app.command(name="telegram-reap")
def telegram_reap(
    idle_days: int = typer.Option(
        None, help="Delete insight channels unused for at least N days."
    ),
) -> None:
    """Delete stale auto-created insight channels."""
    settings = get_settings()
    if not settings.telegram_mtproto_enabled:
        console.print("[red]Telegram MTProto is not configured.[/red]")
        raise typer.Exit(1)
    days = idle_days if idle_days is not None else settings.channel_reap_idle_days
    reaped = reap_stale(
        ChannelRegistry(settings.db_path),
        TelethonProvisioner(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            session=settings.telegram_session,
            bot_username=settings.telegram_bot_username,
        ),
        days,
    )
    if reaped:
        console.print(
            f"[green]Reaped[/green] {len(reaped)} channel(s): {', '.join(reaped)}."
        )
    else:
        console.print(f"No channels idle for {days}+ days.")
