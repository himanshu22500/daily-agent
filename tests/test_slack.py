"""SlackChannel: posts bites, surfaces API errors as retryable (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from daily_agent.feed.channels import SlackChannel, SlackError
from daily_agent.feed.outbox import Outbox, OutboxItem
from daily_agent.models import Bite


def _item(content: str = "PR merged in api: #1 Add billing") -> OutboxItem:
    return OutboxItem(id=1, dedup_key="pr:api#1@merged", subject="repo:api",
                      kind="pr_merged", content=content, attempts=0)


def _channel(handler) -> SlackChannel:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SlackChannel("xoxb-test", "U123", client=client)


def test_send_posts_to_chat_postmessage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    ch = _channel(handler)
    ch.send(_item())
    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert seen["auth"] == "Bearer xoxb-test"
    assert seen["body"]["channel"] == "U123"
    assert "Add billing" in seen["body"]["text"]


def test_api_ok_false_raises_slackerror():
    ch = _channel(lambda req: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(SlackError, match="channel_not_found"):
        ch.send(_item())


def test_http_error_raises():
    ch = _channel(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        ch.send(_item())


def test_outbox_retries_a_failed_slack_send():
    """A failing Slack send is deferred by the outbox, not lost — wired together."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"ok": False, "error": "ratelimited"})
        return httpx.Response(200, json={"ok": True})

    import tempfile
    from datetime import datetime, timedelta, timezone

    with tempfile.TemporaryDirectory() as d:
        ob = Outbox(f"{d}/f.db")
        ob.enqueue(Bite(dedup_key="pr:api#1@merged", subject="repo:api",
                        kind="pr_merged", content="hi"))
        ch = _channel(handler)
        now = datetime.now(timezone.utc)
        assert ob.drain(ch, now=now).failed == 1      # first attempt rejected -> deferred
        assert ob.drain(ch, now=now + timedelta(minutes=10)).sent == 1  # retried -> sent
        assert ob.stats()["sent"] == 1
