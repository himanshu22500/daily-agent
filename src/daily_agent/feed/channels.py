"""Delivery channels.

A channel turns a queued :class:`~daily_agent.feed.outbox.OutboxItem` into an
actual delivery. It must raise on failure (so the outbox retries) and return
normally on success (so the outbox commits the delivery).

Phase 1 ships two channel-agnostic channels — a console printer and a file
appender — so the whole outbox/dedup pipeline is exercised end-to-end before any
Slack credentials exist. Slack lands in Phase 2 as just another ``Channel``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel

from .outbox import OutboxItem, SendReceipt


class ConsoleChannel:
    """Prints each bite as a panel — useful for local runs and demos."""

    name = "console"

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, item: OutboxItem) -> None:
        self._console.print(
            Panel(item.content, title=item.subject, subtitle=item.kind, expand=False)
        )


class FileChannel:
    """Appends each bite to a file as a timestamped block.

    A durable, inspectable transcript of the feed with no external dependency —
    handy for verifying dedup across runs.
    """

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, item: OutboxItem) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        block = f"\n--- {stamp} | {item.subject} | {item.kind} ---\n{item.content}\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)


class SlackError(RuntimeError):
    """A Slack API call returned ``ok: false`` or a transport error."""


class SlackChannel:
    """Delivers each bite as a Slack message via a bot token.

    ``destination`` is where to post: a user ID (``U…``/``W…``) DMs that user —
    the most reliable notification, treated like any direct message — or a
    channel ID posts to that channel. Uses ``chat.postMessage``, which opens the
    DM automatically, so the only scope needed is ``chat:write``.

    ``send`` raises on any failure (transport error, or a logical ``ok: false``
    such as ``not_in_channel`` / ``channel_not_found``) so the outbox retries
    with backoff rather than silently dropping the bite.
    """

    name = "slack"
    _URL = "https://slack.com/api/chat.postMessage"

    def __init__(
        self, token: str, destination: str, *, client: httpx.Client | None = None
    ) -> None:
        self.token = token
        self.destination = destination
        self._client = client or httpx.Client(timeout=10.0)

    def _post(self, text: str) -> None:
        resp = self._client.post(
            self._URL,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"channel": self.destination, "text": text, "mrkdwn": True},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackError(data.get("error", "unknown_error"))

    def send(self, item: OutboxItem) -> None:
        self._post(item.content)

    def send_text(self, text: str) -> None:
        """Post an arbitrary message — used for connectivity checks."""
        self._post(text)

    def close(self) -> None:
        self._client.close()


class TelegramError(RuntimeError):
    """A Telegram API call returned ``ok: false`` or a transport error."""


class TelegramChannel:
    """Delivers each bite as a Telegram message via a bot token.

    Needs no org/admin approval, so it's a good interim channel for testing the
    feed end-to-end. ``chat_id`` is your numeric Telegram user ID (the bot can
    only message you after you've sent it ``/start`` once — bots can't initiate
    conversations).

    ``send`` raises on any failure (transport error, or a logical ``ok: false``
    such as ``chat not found`` / ``bot was blocked``) so the outbox retries with
    backoff rather than dropping the bite. The token sits in the URL path per the
    Telegram Bot API.
    """

    name = "telegram"

    def __init__(
        self, token: str, chat_id: str, *, client: httpx.Client | None = None
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = client or httpx.Client(timeout=10.0)

    def _post(self, text: str) -> int | None:
        """Post ``text``; return Telegram's ``message_id`` (None if absent).

        The message_id lets a reply be threaded under this message, and is the
        identity the inbound listener stores to tell our own posts apart from
        human follow-ups (issue #49).
        """
        resp = self._client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        # Telegram returns a useful `description` even on 4xx, so read the body
        # before treating the status as fatal.
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TelegramError(f"non-JSON response (HTTP {resp.status_code})")
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return (data.get("result") or {}).get("message_id")

    def send(self, item: OutboxItem) -> SendReceipt | None:
        message_id = self._post(item.content)
        if message_id is None:
            return None
        return SendReceipt(chat_id=str(self.chat_id), message_id=message_id)

    def send_text(self, text: str) -> None:
        """Post an arbitrary message — used for connectivity checks."""
        self._post(text)

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Multi-stream routing — deliver each notification type to its own channel
# --------------------------------------------------------------------------- #
# Map a bite's `kind` to its stream: (stable stream key, channel title). New feed
# types (insights, alerts, …) add an entry; unknown kinds fall to org-activity.
_STREAMS: dict[str, tuple[str, str]] = {
    "chapter": ("org-activity", "daily-agent · Org Activity"),
}
_DEFAULT_STREAM: tuple[str, str] = ("org-activity", "daily-agent · Org Activity")


def stream_for(item: OutboxItem) -> tuple[str, str]:
    """Return the (stream_key, channel_title) a bite should be delivered to."""
    return _STREAMS.get(item.kind, _DEFAULT_STREAM)


_TRANSIENT_TELEGRAM_POST_ERRORS = (
    "bot is not a member",
    "chat not found",
)


class MultiStreamTelegramChannel:
    """Routes each bite to the Telegram channel for its stream, creating it on demand.

    Resolves the bite's stream, ensures (provisioning on first use) the channel
    that backs it, and posts there with the bot. Channel create/delete is the
    provisioner's job; ``bot_factory(channel_id)`` builds the posting channel
    (injected for tests so this stays offline-testable).
    """

    name = "telegram-multi"

    def __init__(
        self,
        registry,
        provisioner,
        *,
        bot_factory,
        resolver=stream_for,
        post_retries: int = 2,
        post_retry_seconds: float = 1.0,
    ):
        self._registry = registry
        self._provisioner = provisioner
        self._bot_factory = bot_factory
        self._resolver = resolver
        self._post_retries = post_retries
        self._post_retry_seconds = post_retry_seconds

    def _send_with_retry(self, bot, item: OutboxItem) -> SendReceipt | None:
        for attempt in range(self._post_retries + 1):
            try:
                return bot.send(item)
            except TelegramError as exc:
                transient = any(
                    marker in str(exc).lower()
                    for marker in _TRANSIENT_TELEGRAM_POST_ERRORS
                )
                if not transient or attempt >= self._post_retries:
                    raise
                time.sleep(self._post_retry_seconds)
        return None

    def send(self, item: OutboxItem) -> SendReceipt | None:
        # Imported here to avoid a module import cycle (channel_registry is a peer).
        from .channel_registry import ensure_channel

        stream_key, title = self._resolver(item)
        channel_id = ensure_channel(
            stream_key, title, registry=self._registry, provisioner=self._provisioner
        )
        bot = self._bot_factory(channel_id)
        try:
            return self._send_with_retry(bot, item)
        finally:
            if hasattr(bot, "close"):
                bot.close()

    def close(self) -> None:
        return None
