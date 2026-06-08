"""Telethon provisioner — offline, with an injected fake client (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daily_agent.sources.telegram_provision import (
    TelegramProvisionError,
    TelethonProvisioner,
    _marked_channel_id,
)


class _FakeClient:
    """Records the Telethon requests it's called with; returns a new channel."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request):
        self.calls.append(type(request).__name__)
        # CreateChannelRequest expects a result with .chats[0].id
        return SimpleNamespace(chats=[SimpleNamespace(id=42)])


def test_marked_channel_id():
    assert _marked_channel_id(42) == -10042
    assert _marked_channel_id(16998877) == -10016998877


def test_missing_config_raises():
    with pytest.raises(TelegramProvisionError):
        TelethonProvisioner(api_id="", api_hash="", session="s", bot_username="")


def test_injected_client_skips_config_requirement():
    # An injected client is enough (tests / advanced wiring) — no creds needed.
    TelethonProvisioner(
        api_id="", api_hash="", session="s", bot_username="", client=_FakeClient()
    )


def test_create_channel_creates_invites_promotes_and_marks_id():
    fake = _FakeClient()
    prov = TelethonProvisioner(
        api_id="1", api_hash="h", session="s", bot_username="mybot", client=fake
    )
    cid = prov.create_channel("Org Activity", "about")
    assert cid == -10042  # marked id from the fake channel id 42
    # A bot can't be invited as a member — EditAdminRequest both adds + promotes it.
    assert fake.calls == ["CreateChannelRequest", "EditAdminRequest"]


def test_delete_channel_issues_delete_request():
    fake = _FakeClient()
    prov = TelethonProvisioner(
        api_id="1", api_hash="h", session="s", bot_username="mybot", client=fake
    )
    prov.delete_channel(-10042)
    assert fake.calls == ["DeleteChannelRequest"]
