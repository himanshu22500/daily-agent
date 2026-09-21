"""TelegramChannel: posts bites, surfaces API errors as retryable (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from daily_agent.feed.channels import TelegramChannel, TelegramError
from daily_agent.feed.outbox import Outbox, OutboxItem
from daily_agent.models import Bite


def _item(content: str = "Use a database-enforced deduplication key") -> OutboxItem:
    return OutboxItem(
        id=1,
        dedup_key="insight:deduplication",
        subject="insight:deduplication",
        kind="insight",
        content=content,
        attempts=0,
    )


def _channel(handler) -> TelegramChannel:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramChannel("123:ABC", "42", client=client)


def test_send_posts_to_sendmessage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    _channel(handler).send(_item())
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert seen["body"]["chat_id"] == "42"
    assert "deduplication" in seen["body"]["text"]


def test_send_text_posts_plain_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

    message_id = _channel(handler).send_text("connected")

    assert message_id == 8
    assert seen["body"]["text"] == "connected"


def test_send_ignores_message_id():
    handler = lambda req: httpx.Response(  # noqa: E731
        200, json={"ok": True, "result": {"message_id": 7}}
    )
    assert _channel(handler).send(_item()) is None


def test_send_returns_none_when_no_message_id():
    handler = lambda req: httpx.Response(200, json={"ok": True})  # noqa: E731
    assert _channel(handler).send(_item()) is None


def test_api_ok_false_raises_with_description():
    ch = _channel(
        lambda req: httpx.Response(
            400, json={"ok": False, "description": "chat not found"}
        )
    )
    with pytest.raises(TelegramError, match="chat not found"):
        ch.send(_item())


def test_non_json_5xx_raises():
    ch = _channel(lambda req: httpx.Response(502, text="bad gateway"))
    with pytest.raises(Exception):  # HTTPStatusError or TelegramError
        ch.send(_item())


def test_outbox_retries_a_failed_telegram_send(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, json={"ok": False, "description": "Too Many Requests"}
            )
        return httpx.Response(200, json={"ok": True})

    from datetime import datetime, timedelta, timezone

    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(
        Bite(
            dedup_key="insight:retry",
            subject="insight:retry",
            kind="insight",
            content="hi",
        )
    )
    ch = _channel(handler)
    now = datetime.now(timezone.utc)
    assert ob.drain(ch, now=now).failed == 1
    assert ob.drain(ch, now=now + timedelta(minutes=10)).sent == 1
    assert ob.stats()["sent"] == 1
