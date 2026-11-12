"""CLI for capturing and resurfacing personal coding insights."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import typer
from dotenv import load_dotenv, set_key

load_dotenv()

from rich.console import Console

from .agents.claude_code import SPEND
from .agents.headline_writer import write_headlines
from .agents.insight_extractor import extract_insights, prompt_chars
from .config import get_settings
from .db import display_url
from .db_copy import CopyError, copy_database, table_counts
from .feed.channel_registry import ChannelRegistry, reap_stale
from .feed.channels import (
    ConsoleChannel,
    FileChannel,
    MultiStreamTelegramChannel,
    TelegramChannel,
    TelegramError,
)
from .agents.cover_illustrator import draw_cover
from .feed.covers import CoverStore, generate_covers, import_covers
from .feed.headlines import apply_titles, export_for_titling, generate_headlines
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
from .feed.insights_sweep import pending_transcripts, sweep_insights, unread_messages
from .feed.outbox import Channel, Outbox
from .feed.pacer import Pacer
from .feed.instagram_posts import InstagramSync
from .feed.prep import PrepSync
from .feed.videos import VideoSync
from .sources.instagram import (
    SCOPES,
    Instagram,
    InstagramError,
    LinkedAccount,
    exchange_token,
    linked_accounts,
)
from .sources.telegram_provision import TelethonProvisioner
from .sources.deep_dive import DeepDiveError
from .sources.youtube import BadSourceLink, YouTube, YouTubeError

app = typer.Typer(
    add_completion=False,
    help="Capture durable insights from coding-agent transcripts and resurface them.",
)
insights_app = typer.Typer(no_args_is_help=True, help="Capture and resurface insights.")
app.add_typer(insights_app, name="insights")
videos_app = typer.Typer(
    no_args_is_help=True, help="Follow YouTube channels as feed stories."
)
app.add_typer(videos_app, name="videos")
instagram_app = typer.Typer(
    no_args_is_help=True, help="Follow Instagram accounts as feed stories."
)
app.add_typer(instagram_app, name="instagram")
db_app = typer.Typer(
    no_args_is_help=True, help="Which database the stores use, and moving data."
)
app.add_typer(db_app, name="db")
prep_app = typer.Typer(
    no_args_is_help=True, help="Interview prep from a resume deep dive, as feed cards."
)
app.add_typer(prep_app, name="prep")
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
            ChannelRegistry(settings.db),
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
    outbox = Outbox(settings.db)
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
    store = InsightStore(settings.db)
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
    store = InsightStore(settings.db)
    queued = enqueue_new_insights(store, Outbox(settings.db))
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

        store = InsightStore(settings.db)
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
        queued = enqueue_new_insights(store, Outbox(settings.db))
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


@insights_app.command("sweep")
def insights_sweep(
    settle_minutes: float = typer.Option(
        10.0,
        help="Leave transcripts written to in the last N minutes for the next sweep.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be read; don't call the model or move any cursor.",
    ),
    extract: bool = typer.Option(True, "--extract/--no-extract"),
    max_failures: int = typer.Option(
        3, help="Stop after this many transcripts fail in a row (provider outage)."
    ),
    lock_ttl_seconds: int = typer.Option(600),
    max_titles: int = typer.Option(
        100, help="Write at most N headlines for untitled insights (0 = none)."
    ),
    max_covers: int = typer.Option(
        10, help="Draw at most N covers for insights without one (0 = none)."
    ),
) -> None:
    """Capture insights from every project's transcripts, ended sessions or not.

    For sessions that rarely end, such as Claude inside Zed over ACP. New
    insights reach the web feed at once and are queued for Telegram, which
    keeps its own pace (only `flush` delivers). Then headlines and covers are
    written for insights that lack them, within per-run caps.
    """
    settings = get_settings()
    root = Path(settings.projects_path)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not root.exists():
        console.print(f"{stamp} [yellow]No transcripts found[/yellow] at {root}.")
        raise typer.Exit(1)
    store = InsightStore(settings.db)
    settle_seconds = max(0.0, settle_minutes * 60)

    if dry_run:
        pending = pending_transcripts(store, root, settle_seconds=settle_seconds)
        ready = [item for item in pending if item.settled]
        messages = chars = 0
        for item in ready:
            unread = unread_messages(store, item.path)
            messages += len(unread)
            chars += prompt_chars(unread)
        console.print(
            f"{stamp} Sweep dry run: {len(ready)} transcript(s) to read, "
            f"{messages} message(s), about {chars // 4:,} input tokens; "
            f"{len(pending) - len(ready)} still being written.",
            soft_wrap=True,
        )
        return

    lock = RunLock(
        FlushPaths.for_db(settings.db_path).lock, ttl_seconds=lock_ttl_seconds
    )
    if not lock.acquire():
        console.print(
            f"{stamp} [yellow]A flush or sweep is already running[/yellow] — skipping."
        )
        return

    async def background() -> tuple:
        swept = await sweep_insights(
            store,
            root,
            settings.insights_marker,
            (lambda messages: extract_insights(settings.model, messages))
            if extract
            else None,
            settle_seconds=settle_seconds,
            max_consecutive_failures=max_failures,
            on_progress=lock.refresh,
        )
        if swept.stopped_early:  # most likely the same outage; try next sweep
            return swept, None, None
        titled = covered = None
        if max_titles > 0:
            titled = await generate_headlines(
                store,
                lambda batch: write_headlines(settings.headline_model, batch),
                limit=max_titles,
                on_progress=lock.refresh,
            )
        if max_covers > 0:

            async def draw(record: dict, feedback: list[str] | None) -> str:
                lock.refresh()
                return await draw_cover(settings.cover_model, record, feedback)

            covered = await generate_covers(
                store,
                CoverStore(settings.db),
                draw,
                source=f"model:{settings.cover_model}",
                limit=max_covers,
                concurrency=2,
            )
        return swept, titled, covered

    try:
        result, titled, covered = asyncio.run(background())
        queued = enqueue_new_insights(store, Outbox(settings.db))
    finally:
        lock.release()

    got = result.captured
    console.print(
        f"{stamp} Sweep read {result.read} transcript(s): captured "
        f"[bold]{got.new}[/bold] new ({got.marked} marker, {got.extracted} "
        f"extracted); queued {queued} for Telegram; {result.waiting} still being "
        "written"
        + (f"; [red]{len(result.failed)} failed[/red]" if result.failed else "")
        + ".",
        soft_wrap=True,
    )
    for path, reason in result.failed.items():
        console.print(f"  {path}: {reason}", soft_wrap=True, markup=False)
    if titled is not None and (titled.written or titled.failed):
        console.print(
            f"  Headlines: wrote {titled.written}"
            + (f"; {len(titled.failed)} batch(es) failed" if titled.failed else "")
            + ".",
            soft_wrap=True,
        )
        for reason in titled.failed:
            console.print(f"    {reason}", soft_wrap=True, markup=False)
    if covered is not None and (covered.written or covered.failed):
        console.print(
            f"  Covers: drew {covered.written}"
            + (f"; {len(covered.failed)} failed" if covered.failed else "")
            + ".",
            soft_wrap=True,
        )
        for key, problems in covered.failed.items():
            console.print(
                f"    {key}: {'; '.join(problems)}", soft_wrap=True, markup=False
            )
    if SPEND.calls:
        console.print(
            f"  Claude Code: {SPEND.calls} call(s), about ${SPEND.cost_usd:.2f} "
            "at API rates.",
            soft_wrap=True,
        )
    if result.stopped_early:
        console.print(
            "[red]Stopped after repeated failures[/red]; the rest wait for the "
            "next sweep.",
            soft_wrap=True,
        )
        raise typer.Exit(1)


@insights_app.command("titles-generate")
def insights_titles_generate(
    limit: int = typer.Option(None, help="Write at most N headlines."),
    model: str = typer.Option(
        None, help="Claude Code model (default: headline_model)."
    ),
) -> None:
    """Write feed headlines for untitled insights with Claude Code."""
    settings = get_settings()
    chosen = model or settings.headline_model
    run = asyncio.run(
        generate_headlines(
            InsightStore(settings.db),
            lambda batch: write_headlines(chosen, batch),
            limit=limit,
        )
    )
    console.print(
        f"Wrote [bold]{run.written}[/bold] headline(s) with {chosen}"
        + (
            f"; [yellow]{len(run.failed)} batch(es) failed[/yellow]"
            if run.failed
            else ""
        )
        + "."
    )
    for reason in run.failed:
        console.print(f"  [dim]{reason}[/dim]", markup=False)


@insights_app.command("titles-export")
def insights_titles_export(
    out: Path = typer.Option(None, "--out", help="Write JSON here (default: stdout)."),
    include_titled: bool = typer.Option(
        False, "--all", help="Include insights that already have a headline."
    ),
) -> None:
    """Export insights needing a web-feed headline as JSON ({key, text, type, tags})."""
    records = export_for_titling(
        InsightStore(get_settings().db), include_titled=include_titled
    )
    payload = json.dumps(records, indent=1, ensure_ascii=False)
    if out is None:
        typer.echo(payload)
        return
    out.write_text(payload + "\n")
    console.print(f"Exported [bold]{len(records)}[/bold] insight(s) to {out}.")


@insights_app.command("titles-import")
def insights_titles_import(
    path: Path = typer.Argument(..., help='JSON object: {"insight:key": "Headline"}.'),
    overwrite: bool = typer.Option(False, help="Replace existing headlines."),
) -> None:
    """Store headlines written for the web feed (e.g. by a batch of subagents)."""
    try:
        titles = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        console.print(f"[red]Can't read {path}:[/red] {exc}")
        raise typer.Exit(1)
    if not isinstance(titles, dict):
        console.print('[red]Expected a JSON object: {"insight:key": "Headline"}.[/red]')
        raise typer.Exit(1)
    result = apply_titles(InsightStore(get_settings().db), titles, overwrite=overwrite)
    console.print(
        f"Stored [bold]{result.applied}[/bold] headline(s)"
        + (f"; kept {result.kept_existing} existing" if result.kept_existing else "")
        + (
            f"; [yellow]{len(result.unknown)} unknown key(s)[/yellow]"
            if result.unknown
            else ""
        )
        + (f"; [yellow]{len(result.empty)} empty[/yellow]" if result.empty else "")
        + "."
    )
    for key in result.unknown[:10] + result.empty[:10]:
        console.print(f"  [dim]skipped {key}[/dim]")


@insights_app.command("covers-import")
def insights_covers_import(
    source: Path = typer.Argument(..., help="Directory of <insight-slug>.svg files."),
    label: str = typer.Option("import", help="Who drew them (stored per cover)."),
) -> None:
    """Validate illustrated covers and store them in the database."""
    settings = get_settings()
    known = {i.key for i in InsightStore(settings.db).all()}
    result = import_covers(source, CoverStore(settings.db), known, source=label)
    console.print(
        f"Imported [bold]{result.imported}[/bold] cover(s) into the database"
        + (
            f"; [yellow]{len(result.rejected)} rejected[/yellow]"
            if result.rejected
            else ""
        )
        + (f"; {len(result.unknown)} for unknown insights" if result.unknown else "")
        + "."
    )
    for name, problems in list(result.rejected.items())[:20]:
        console.print(f"  [dim]{name}: {'; '.join(problems)}[/dim]")


@insights_app.command("covers-generate")
def insights_covers_generate(
    limit: int = typer.Option(None, help="Draw at most N covers."),
    concurrency: int = typer.Option(2, help="Drawings in flight at once."),
    model: str = typer.Option(None, help="Model to draw with (default: cover_model)."),
    overwrite: bool = typer.Option(False, help="Redraw insights that have a cover."),
) -> None:
    """Draw illustrated covers with the model for insights that lack one."""
    settings = get_settings()
    chosen = model or settings.cover_model

    async def draw(record: dict, feedback: list[str] | None) -> str:
        return await draw_cover(chosen, record, feedback)

    result = asyncio.run(
        generate_covers(
            InsightStore(settings.db),
            CoverStore(settings.db),
            draw,
            source=f"model:{chosen}",
            limit=limit,
            concurrency=concurrency,
            overwrite=overwrite,
        )
    )
    console.print(
        f"Drew [bold]{result.written}[/bold] cover(s) with {chosen}"
        + (f"; [yellow]{len(result.failed)} failed[/yellow]" if result.failed else "")
        + "."
    )
    for key, problems in list(result.failed.items())[:20]:
        console.print(f"  [dim]{key}: {'; '.join(problems)}[/dim]")


@insights_app.command("status")
def insights_status() -> None:
    """Show captured-insight and delivery-queue counts."""
    settings = get_settings()
    store = InsightStore(settings.db)
    outbox = Outbox(settings.db)
    statuses: dict[str, int] = {}
    for insight in store.all():
        statuses[insight.status] = statuses.get(insight.status, 0) + 1
    console.print(f"Insights: {statuses or {'empty': 0}}")
    console.print(f"Outbox: {outbox.stats()}")


@app.command(name="web")
def web(
    host: str = typer.Option(None, help="Interface to bind (default: web_host)."),
    port: int = typer.Option(None, help="Port to listen on (default: web_port)."),
) -> None:
    """Serve the insight feed as a scrollable web page."""
    import uvicorn

    from .web.app import create_app

    settings = get_settings()
    bind = host or settings.web_host
    listen = port or settings.web_port
    if bind not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]Listening on {bind}[/yellow] — the feed has no login; only "
            "bind to a private network you trust (e.g. Tailscale)."
        )
    extra = [h.strip() for h in settings.web_allowed_hosts.split(",") if h.strip()]
    allowed = list(dict.fromkeys(["127.0.0.1", "localhost", bind, *extra]))
    console.print(f"Insight feed at [bold]http://{bind}:{listen}/[/bold]")
    uvicorn.run(
        create_app(
            settings.db,
            page_size=settings.web_page_size,
            allowed_hosts=allowed,
            videos=_video_sync(settings),
            instagram=_instagram_sync(settings),
            video_loop=settings.web_video_sync,
            prep=_prep_sync(settings),
            prep_loop=settings.web_prep_sync,
        ),
        host=bind,
        port=listen,
        log_level="warning",
    )


def _video_sync(settings, model: str | None = None) -> VideoSync:
    from .agents.video_writer import write_story

    chosen = model or settings.video_model
    return VideoSync(
        settings.db,
        YouTube(),
        lambda video, transcript: write_story(chosen, video, transcript),
        check_every=timedelta(hours=settings.video_check_hours),
    )


@videos_app.command("add")
def videos_add(
    link: str = typer.Argument(..., help="A YouTube channel or playlist link."),
) -> None:
    """Follow a channel: queue its latest 5 videos and every new upload."""
    sync = _video_sync(get_settings())
    try:
        source = asyncio.run(sync.follow(link))
    except (BadSourceLink, YouTubeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1)
    console.print(
        f"Following {source.title or source.id} ({source.id}). Stories are "
        "written by the feed server, or by `daily-agent videos sync`.",
        markup=False,
    )


@videos_app.command("list")
def videos_list() -> None:
    """Followed channels and their story counts."""
    statuses = _video_sync(get_settings()).store.statuses()
    if not statuses:
        console.print("Not following any channels.")
    for status in statuses:
        console.print(
            f"{status.id}  {status.title}  stories={status.stories} "
            f"queued={status.queued} failed={len(status.failed)}",
            markup=False,
        )
        if status.check_error:
            console.print(f"  last check failed: {status.check_error}", markup=False)


@videos_app.command("remove")
def videos_remove(
    source_id: str = typer.Argument(..., help="Source id from `videos list`."),
) -> None:
    """Stop following a channel (its stories stay in the feed)."""
    if not _video_sync(get_settings()).unfollow(source_id):
        console.print(f"Not following {source_id}.", style="red", markup=False)
        raise typer.Exit(1)
    console.print(f"Stopped following {source_id}.", markup=False)


@videos_app.command("sync")
def videos_sync(
    limit: int = typer.Option(None, help="Write at most N stories (default 5)."),
    model: str = typer.Option(None, help="Claude Code model (default: video_model)."),
) -> None:
    """Check followed channels now and write stories for queued videos."""
    settings = get_settings()
    report = asyncio.run(_video_sync(settings, model).run_once(limit=limit))
    console.print(
        f"Checked {report.checked} source(s), queued {report.queued} video(s), "
        f"wrote {report.written} story(ies)"
        + (f", {report.waiting} waiting for captions" if report.waiting else "")
        + (f", {report.remaining} still queued" if report.remaining else "")
        + (f"; cost ${SPEND.cost_usd:.2f}" if SPEND.calls else "")
        + "."
    )
    for error in report.errors:
        console.print(f"  [dim]{error}[/dim]", markup=False)


def _instagram_sync(settings, model: str | None = None) -> InstagramSync | None:
    """``None`` until `instagram connect` has saved a token."""
    if not settings.instagram_enabled:
        return None
    from .agents.post_writer import write_post_story

    chosen = model or settings.instagram_model
    return InstagramSync(
        settings.db,
        Instagram(settings.instagram_token, settings.instagram_user_id),
        lambda post, name, images: write_post_story(chosen, post, name, images),
        check_every=timedelta(hours=settings.instagram_check_hours),
    )


def _require_instagram() -> InstagramSync:
    sync = _instagram_sync(get_settings())
    if sync is None:
        console.print(
            "[red]Instagram isn't connected. Run `daily-agent instagram connect` "
            "first.[/red]"
        )
        raise typer.Exit(1)
    return sync


_CONNECT_GUIDE = f"""\
Connect your Instagram professional (Business or Creator) account, once.
You need (see docs/instagram-setup.md for the clicks):
  1. Your Instagram account switched to Business or Creator and linked to a
     Facebook Page.
  2. A Meta app (developers.facebook.com, type Business) with its App ID and
     App secret (App settings > Basic).
  3. A User token from the Graph API Explorer for that app with these
     permissions: {", ".join(SCOPES)}.
Nothing you paste is printed. The token saved to .env is the Page's token,
which doesn't expire.
"""


async def _linked(app_id: str, app_secret: str, short: str) -> list[LinkedAccount]:
    long_lived = await exchange_token(app_id, app_secret, short)
    return await linked_accounts(long_lived)


def _save_env(path: Path, values: dict[str, str]) -> None:
    """Write ``values`` into the .env file, keeping (or making private) its mode."""
    existed = path.exists()
    mode = path.stat().st_mode & 0o777 if existed else 0o600
    if not existed:
        path.touch(mode=0o600)
    for key, value in values.items():
        set_key(str(path), key, value)
    os.chmod(path, mode)


@instagram_app.command("connect")
def instagram_connect(
    env_file: Path = typer.Option(Path(".env"), help="The .env file to save to."),
) -> None:
    """Connect your Instagram professional account (one-time setup)."""
    console.print(_CONNECT_GUIDE, markup=False)
    app_id = typer.prompt("Meta App ID").strip()
    app_secret = typer.prompt("Meta App secret", hide_input=True).strip()
    short = typer.prompt("Graph API Explorer token", hide_input=True).strip()
    try:
        accounts = asyncio.run(_linked(app_id, app_secret, short))
    except (InstagramError, BadSourceLink) as exc:
        console.print(f"[red]{exc}[/red]", markup=False)
        raise typer.Exit(1)
    if not accounts:
        console.print(
            "No Facebook Page with a linked Instagram professional account was "
            "found for that token. Check that the Page is linked to your Instagram "
            "account and that the token has all the permissions listed above.",
            markup=False,
        )
        raise typer.Exit(1)
    chosen = accounts[0]
    if len(accounts) > 1:
        for n, account in enumerate(accounts, 1):
            console.print(
                f"  {n}. @{account.ig_username} (Page: {account.page_name})",
                markup=False,
            )
        pick = typer.prompt("Which account", type=int, default=1)
        if not 1 <= pick <= len(accounts):
            console.print("[red]No such account.[/red]")
            raise typer.Exit(1)
        chosen = accounts[pick - 1]
    try:
        asyncio.run(
            Instagram(chosen.page_token, chosen.ig_user_id).account(chosen.ig_username)
        )
    except (InstagramError, BadSourceLink) as exc:
        console.print(
            f"[red]The account was found, but reading it failed: {exc}[/red]",
            markup=False,
        )
        raise typer.Exit(1)
    _save_env(
        env_file,
        {
            "DAILY_AGENT_INSTAGRAM_TOKEN": chosen.page_token,
            "DAILY_AGENT_INSTAGRAM_USER_ID": chosen.ig_user_id,
        },
    )
    console.print(
        f"Connected @{chosen.ig_username} (Page: {chosen.page_name}). Saved "
        f"DAILY_AGENT_INSTAGRAM_TOKEN and DAILY_AGENT_INSTAGRAM_USER_ID to "
        f"{env_file}. Restart the feed server to follow accounts from Sources.",
        markup=False,
    )


@instagram_app.command("add")
def instagram_add(
    link: str = typer.Argument(..., help="An Instagram profile link."),
) -> None:
    """Follow an account: queue its latest 5 posts and every new one."""
    sync = _require_instagram()
    try:
        source = asyncio.run(sync.follow(link))
    except (BadSourceLink, InstagramError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1)
    console.print(
        f"Following {source.title or source.id} ({source.id}). Stories are "
        "written by the feed server, or by `daily-agent instagram sync`.",
        markup=False,
    )


@instagram_app.command("list")
def instagram_list() -> None:
    """Followed accounts and their story counts."""
    from .feed.instagram_posts import InstagramStore

    settings = get_settings()
    if not settings.instagram_enabled:
        console.print("Instagram isn't connected (`daily-agent instagram connect`).")
    statuses = InstagramStore(settings.db).statuses()
    if not statuses:
        console.print("Not following any Instagram accounts.")
    for status in statuses:
        console.print(
            f"{status.id}  {status.title}  stories={status.stories} "
            f"queued={status.queued} failed={len(status.failed)}",
            markup=False,
        )
        if status.check_error:
            console.print(f"  last check failed: {status.check_error}", markup=False)


@instagram_app.command("remove")
def instagram_remove(
    source_id: str = typer.Argument(..., help="Source id from `instagram list`."),
) -> None:
    """Stop following an account (its stories stay in the feed)."""
    from .feed.instagram_posts import InstagramStore

    store = InstagramStore(get_settings().db)
    if not store.unfollow(source_id, now=datetime.now().astimezone()):
        console.print(f"Not following {source_id}.", style="red", markup=False)
        raise typer.Exit(1)
    console.print(f"Stopped following {source_id}.", markup=False)


@instagram_app.command("sync")
def instagram_sync(
    limit: int = typer.Option(None, help="Write at most N stories (default 5)."),
    model: str = typer.Option(
        None, help="Claude Code model (default: instagram_model)."
    ),
) -> None:
    """Check followed accounts now and write stories for queued posts."""
    sync = _instagram_sync(get_settings(), model) or _require_instagram()
    report = asyncio.run(sync.run_once(limit=limit))
    console.print(
        f"Checked {report.checked} account(s), queued {report.queued} post(s), "
        f"wrote {report.written} story(ies)"
        + (f", {report.remaining} still queued" if report.remaining else "")
        + (f"; cost ${SPEND.cost_usd:.2f}" if SPEND.calls else "")
        + "."
    )
    for error in report.errors:
        console.print(f"  [dim]{error}[/dim]", markup=False)


def _prep_sync(settings, model: str | None = None) -> PrepSync:
    from .agents.concept_writer import write_explainer

    chosen = model or settings.prep_model
    return PrepSync(settings.db, lambda request: write_explainer(chosen, request))


@prep_app.command("import")
def prep_import(
    folder: Path = typer.Argument(..., help="The deep-dive folder (with README.md)."),
    all_points: bool = typer.Option(
        False, "--all", help="Also import non-job points (skills, general)."
    ),
) -> None:
    """Add the folder's interview Q&A as flashcards and queue its concepts.

    Run it again after editing the notes: changed answers are updated, removed
    questions dropped, and changed concepts rewritten.
    """
    try:
        report = _prep_sync(get_settings()).import_folder(
            folder, jobs_only=not all_points
        )
    except DeepDiveError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1)
    console.print(
        f"{report.points} resume point(s): {report.cards_added} flashcard(s) added, "
        f"{report.cards_updated} updated, {report.cards_removed} removed "
        f"({report.unanswered} question(s) have no answer yet). "
        f"{report.concepts_queued} concept explainer(s) queued"
        + (f", {report.concepts_removed} removed" if report.concepts_removed else "")
        + ". The feed server writes them in the background, or run "
        "`daily-agent prep write`.",
        markup=False,
    )


@prep_app.command("status")
def prep_status() -> None:
    """Flashcards in the feed and how far the explainers have got."""
    progress = _prep_sync(get_settings()).store.progress()
    console.print(
        f"{progress.cards} flashcard(s); explainers: {progress.explainers} written, "
        f"{progress.queued + progress.writing} to write, "
        f"{len(progress.failed)} failed.",
        markup=False,
    )
    for concept, error in progress.failed:
        console.print(f"  failed: {concept}: {error}", markup=False)


@prep_app.command("write")
def prep_write(
    limit: int = typer.Option(None, help="Write at most N explainers (default 5)."),
    model: str = typer.Option(None, help="Claude Code model (default: prep_model)."),
) -> None:
    """Write queued concept explainers now."""
    settings = get_settings()
    report = asyncio.run(_prep_sync(settings, model).run_once(limit=limit))
    console.print(
        f"Wrote {report.written} explainer(s)"
        + (f", {report.remaining} still queued" if report.remaining else "")
        + (f"; cost ${SPEND.cost_usd:.2f}" if SPEND.calls else "")
        + "."
    )
    for error in report.errors:
        console.print(f"  [dim]{error}[/dim]", markup=False)


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
        ChannelRegistry(settings.db),
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


@db_app.command("info")
def db_info() -> None:
    """Show which database the stores use and how many rows each table has."""
    settings = get_settings()
    console.print(f"Database: {display_url(settings.db)}")
    for name, count in table_counts(settings.db).items():
        console.print(f"  {name}: {'-' if count is None else count}")


@db_app.command("copy")
def db_copy(
    source: str = typer.Argument(..., help="Database to copy from: a SQLite path."),
    to: str = typer.Option(
        "",
        "--to",
        help="Database URL to copy into (default: DAILY_AGENT_DATABASE_URL).",
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Empty the target's store tables first."
    ),
) -> None:
    """Copy every store table into another database, e.g. SQLite into Postgres.

    Copy a snapshot of the live file, not the file the sweep is writing to:
    `cp daily_agent.db /tmp/` first.
    """
    target = to or get_settings().database_url
    if not target:
        console.print(
            "[red]No target: pass --to or set DAILY_AGENT_DATABASE_URL.[/red]"
        )
        raise typer.Exit(1)
    try:
        report = copy_database(source, target, replace=replace)
    except CopyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Copied into {display_url(target)}:")
    for name, count in report.copied.items():
        console.print(f"  {name}: {count}")
    if report.missing_in_source:
        console.print(
            f"Not in the source (left empty): {', '.join(report.missing_in_source)}"
        )
    if report.left_behind:
        console.print(f"Not store tables (not copied): {', '.join(report.left_behind)}")
