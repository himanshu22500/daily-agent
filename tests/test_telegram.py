"""TelegramChannel: posts bites, surfaces API errors as retryable (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from daily_agent.feed.channels import TelegramChannel, TelegramError
from daily_agent.feed.outbox import Outbox, OutboxItem
from daily_agent.models import Bite


def _item(content: str = "PR merged in api: #1 Add billing") -> OutboxItem:
    return OutboxItem(id=1, dedup_key="pr:api#1@merged", subject="repo:api",
                      kind="pr_merged", content=content, attempts=0)


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
    assert "Add billing" in seen["body"]["text"]


def test_api_ok_false_raises_with_description():
    ch = _channel(
        lambda req: httpx.Response(400, json={"ok": False, "description": "chat not found"})
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
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True})

    from datetime import datetime, timedelta, timezone

    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(Bite(dedup_key="pr:api#1@merged", subject="repo:api",
                    kind="pr_merged", content="hi"))
    ch = _channel(handler)
    now = datetime.now(timezone.utc)
    assert ob.drain(ch, now=now).failed == 1
    assert ob.drain(ch, now=now + timedelta(minutes=10)).sent == 1
    assert ob.stats()["sent"] == 1
