"""MTProto channel provisioner — the live implementation of ``Provisioner``.

A Telegram **bot** cannot create channels, so the multi-stream feed uses a
Telethon **user-client** (the maintainer's account) to create/delete channels and
add the bot as admin so it can post. This is the one piece that needs a real
account session — it can't run in CI or on a cloud agent.

Security: the session file + api credentials are effectively the account login.
Keep them gitignored and out of CI. Telethon is an optional dependency
(`uv sync --extra telegram`) and is imported lazily so the package works without it.

Conforms structurally to ``daily_agent.feed.channel_registry.Provisioner``.
"""

from __future__ import annotations


class TelegramProvisionError(RuntimeError):
    """MTProto provisioning failed (not configured, not authorized, or API error)."""


def _marked_channel_id(raw_id: int) -> int:
    """Convert a raw channel id to the Bot-API form the bot posts with (-100…)."""
    return int(f"-100{raw_id}")


class TelethonProvisioner:
    """Creates/deletes Telegram channels via a Telethon user-client.

    ``client`` is injectable for tests; in production it's built lazily from
    config and must already be authorized (run ``daily-agent telegram-auth`` once).
    """

    def __init__(
        self,
        *,
        api_id: str,
        api_hash: str,
        session: str,
        bot_username: str,
        client=None,
    ) -> None:
        if client is None and not (api_id and api_hash and bot_username):
            raise TelegramProvisionError(
                "MTProto not configured. Set DAILY_AGENT_TELEGRAM_API_ID, "
                "DAILY_AGENT_TELEGRAM_API_HASH and DAILY_AGENT_TELEGRAM_BOT_USERNAME, "
                "then run `daily-agent telegram-auth`."
            )
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._bot = bot_username
        self._client = client

    def _conn(self):
        if self._client is not None:
            return self._client
        from telethon.sync import TelegramClient  # lazy: optional dep

        client = TelegramClient(self._session, int(self._api_id), self._api_hash)
        client.connect()
        if not client.is_user_authorized():
            raise TelegramProvisionError(
                "Telegram session not authorized — run `daily-agent telegram-auth` first."
            )
        self._client = client
        return client

    def create_channel(self, title: str, about: str = "") -> int:
        """Create a broadcast channel, add the bot as a posting admin, return its id."""
        from telethon.tl.functions.channels import CreateChannelRequest, EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        client = self._conn()
        result = client(
            CreateChannelRequest(
                title=title, about=about or "", broadcast=True, megagroup=False
            )
        )
        channel = result.chats[0]
        # A bot can't be invited to a channel as a member — promoting it to admin
        # is what adds it (and lets it post). One EditAdminRequest does both.
        client(
            EditAdminRequest(
                channel,
                self._bot,
                ChatAdminRights(
                    post_messages=True, edit_messages=True, delete_messages=True
                ),
                rank="daily-agent",
            )
        )
        return _marked_channel_id(channel.id)

    def delete_channel(self, channel_id: int) -> None:
        from telethon.tl.functions.channels import DeleteChannelRequest

        self._conn()(DeleteChannelRequest(channel_id))
