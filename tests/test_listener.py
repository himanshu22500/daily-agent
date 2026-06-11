"""Inbound listener: update classification, durable offset, poll loop (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from daily_agent.feed.channels import TelegramError
from daily_agent.feed.listener import (
    Listener,
    ListenerStore,
    TelegramUpdates,
    classify_update,
)


# A bite we sent (chat -100, message 28) replied to by the maintainer (message 29).
SENT = {
    ("-100", 28): {
        "dedup_key": "chapter:comms-v3:abc",
        "subject": "initiative:comms-v3",
    }
}


def _lookup(chat_id, message_id):
    return SENT.get((str(chat_id), int(message_id)))


def _channel_post(*, message_id, chat_id=-100, reply_to=None, text="Hello"):
    post = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "channel"},
        "text": text,
    }
    if reply_to is not None:
        post["reply_to_message"] = reply_to
    return post


def _update(update_id, post):
    return {"update_id": update_id, "channel_post": post}


# --------------------------------------------------------------------------- #
# classify_update — the disambiguation core
# --------------------------------------------------------------------------- #
def test_reply_to_known_bite_is_a_followup():
    reply_to = {"message_id": 28, "text": "📦 Report generation times out…"}
    update = _update(
        1, _channel_post(message_id=29, reply_to=reply_to, text="what does that mean?")
    )
    f = classify_update(update, _lookup)
    assert f is not None
    assert f.chat_id == "-100"
    assert f.message_id == 29
    assert f.reply_to_message_id == 28
    assert f.text == "what does that mean?"
    assert f.dedup_key == "chapter:comms-v3:abc"
    assert f.subject == "initiative:comms-v3"
    assert f.bite_text.startswith("📦 Report")


def test_our_own_bite_is_ignored():
    # An incoming post whose id is one we sent is our own bite, even if it looks
    # like it replies to another of our messages — never treat it as a question.
    reply_to = {"message_id": 1, "text": "earlier bite"}
    update = _update(2, _channel_post(message_id=28, reply_to=reply_to))
    assert classify_update(update, _lookup) is None


def test_reply_to_unknown_message_is_ignored():
    reply_to = {"message_id": 999, "text": "not ours"}
    update = _update(3, _channel_post(message_id=30, reply_to=reply_to))
    assert classify_update(update, _lookup) is None


def test_non_reply_and_non_channel_post_are_ignored():
    assert classify_update(_update(4, _channel_post(message_id=31)), _lookup) is None
    assert classify_update({"update_id": 5, "message": {"text": "dm"}}, _lookup) is None


# --------------------------------------------------------------------------- #
# ListenerStore — durable offset
# --------------------------------------------------------------------------- #
def test_offset_round_trips_and_defaults_to_zero(tmp_path):
    store = ListenerStore(tmp_path / "f.db")
    assert store.get_offset() == 0
    store.set_offset(42)
    assert store.get_offset() == 42
    # A fresh store on the same file resumes from the persisted offset.
    assert ListenerStore(tmp_path / "f.db").get_offset() == 42


# --------------------------------------------------------------------------- #
# Listener.poll_once — handle follow-ups, advance offset
# --------------------------------------------------------------------------- #
class _FakeUpdates:
    def __init__(self, batches):
        self._batches = list(batches)
        self.offsets_seen = []

    def get_updates(self, offset):
        self.offsets_seen.append(offset)
        return self._batches.pop(0) if self._batches else []


def test_poll_once_handles_only_followups_and_advances_offset(tmp_path):
    batch = [
        _update(
            10, _channel_post(message_id=29, reply_to={"message_id": 28, "text": "b"})
        ),  # follow-up
        _update(11, _channel_post(message_id=28)),  # our own bite (no reply) -> ignored
        _update(
            12, _channel_post(message_id=40, reply_to={"message_id": 999})
        ),  # unknown -> ignored
    ]
    handled = []
    store = ListenerStore(tmp_path / "f.db")
    listener = Listener(_FakeUpdates([batch]), store, _lookup, handled.append)

    assert listener.poll_once() == 3
    assert [f.message_id for f in handled] == [29]  # only the real follow-up
    assert store.get_offset() == 13  # max update_id (12) + 1


def test_poll_once_uses_persisted_offset(tmp_path):
    store = ListenerStore(tmp_path / "f.db")
    store.set_offset(100)
    fake = _FakeUpdates([[]])
    Listener(fake, store, _lookup, lambda f: None).poll_once()
    assert fake.offsets_seen == [100]  # resumed from the stored offset


def test_handler_error_does_not_wedge_offset(tmp_path):
    batch = [
        _update(
            7, _channel_post(message_id=29, reply_to={"message_id": 28, "text": "b"})
        )
    ]

    def boom(_f):
        raise RuntimeError("answer failed")

    store = ListenerStore(tmp_path / "f.db")
    Listener(_FakeUpdates([batch]), store, _lookup, boom).poll_once()
    assert store.get_offset() == 8  # advanced past the failed handler


# --------------------------------------------------------------------------- #
# TelegramUpdates — getUpdates HTTP shape
# --------------------------------------------------------------------------- #
def _updates_client(handler) -> TelegramUpdates:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramUpdates("123:ABC", client=client)


def test_get_updates_sends_offset_and_allowed_updates():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    result = _updates_client(handler).get_updates(offset=5)
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/getUpdates"
    assert seen["body"]["offset"] == 5
    assert seen["body"]["allowed_updates"] == ["channel_post"]
    assert result == [{"update_id": 1}]


def test_get_updates_raises_on_api_error():
    client = _updates_client(
        lambda req: httpx.Response(409, json={"ok": False, "description": "Conflict"})
    )
    with pytest.raises(TelegramError, match="Conflict"):
        client.get_updates(offset=0)
