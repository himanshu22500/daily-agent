"""Console, file, and Telegram delivery for personal insights."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel

from .outbox import OutboxItem


class ConsoleChannel:
    name = "console"

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, item: OutboxItem) -> None:
        self._console.print(
            Panel(item.content, title=item.subject, subtitle=item.kind, expand=False)
        )


class FileChannel:
    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, item: OutboxItem) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        block = f"\n--- {stamp} | {item.subject} | {item.kind} ---\n{item.content}\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)


class TelegramError(RuntimeError):
    pass


class TelegramChannel:
    name = "telegram"

    def __init__(
        self, token: str, chat_id: str, *, client: httpx.Client | None = None
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = client or httpx.Client(timeout=10.0)

    def _post(self, text: str) -> int | None:
        resp = self._client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TelegramError(f"non-JSON response (HTTP {resp.status_code})")
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return (data.get("result") or {}).get("message_id")

    def send(self, item: OutboxItem) -> None:
        self._post(item.content)

    def send_text(self, text: str) -> int | None:
        return self._post(text)

    def close(self) -> None:
        self._client.close()


_TRANSIENT_TELEGRAM_POST_ERRORS = (
    "bot is not a member",
    "chat not found",
)


class MultiStreamTelegramChannel:
    """Route insights to auto-provisioned Telegram channels by insight type."""

    name = "telegram-multi"

    def __init__(
        self,
        registry,
        provisioner,
        *,
        bot_factory,
        resolver,
        post_retries: int = 2,
        post_retry_seconds: float = 1.0,
    ) -> None:
        self._registry = registry
        self._provisioner = provisioner
        self._bot_factory = bot_factory
        self._resolver = resolver
        self._post_retries = post_retries
        self._post_retry_seconds = post_retry_seconds

    def _send_with_retry(self, bot, item: OutboxItem) -> None:
        for attempt in range(self._post_retries + 1):
            try:
                bot.send(item)
                return
            except TelegramError as exc:
                transient = any(
                    marker in str(exc).lower()
                    for marker in _TRANSIENT_TELEGRAM_POST_ERRORS
                )
                if not transient or attempt >= self._post_retries:
                    raise
                time.sleep(self._post_retry_seconds)

    def send(self, item: OutboxItem) -> None:
        from .channel_registry import ensure_channel

        stream_key, title = self._resolver(item)
        channel_id = ensure_channel(
            stream_key, title, registry=self._registry, provisioner=self._provisioner
        )
        bot = self._bot_factory(channel_id)
        try:
            self._send_with_retry(bot, item)
        finally:
            if hasattr(bot, "close"):
                bot.close()

    def close(self) -> None:
        return None
