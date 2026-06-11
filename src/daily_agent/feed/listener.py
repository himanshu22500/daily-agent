"""Inbound listener — long-poll Telegram for replies to feed bites.

The feed is otherwise outbound-only. This is the one **persistent process**: it
long-polls ``getUpdates`` and, for each update, decides whether it's a human
follow-up to a bite we sent (issue #49). The decision is the tricky part — in a
broadcast channel both the bot's own bites and the maintainer's replies are
attributed to the channel (``sender_chat``), so we can't tell them apart by
sender. Instead we use the ``sent_messages`` set the outbox records:

- if the *incoming* message id is one we sent → it's our own bite, ignore it;
- else if it *replies to* a message id we sent → it's a human follow-up;
- else → a reply to something we didn't send → ignore.

Phase 2 (this module) is the loop + classification + durable offset. The
``handler`` is injected; grounding the answer and posting it threaded land in
later phases. The offset is persisted after every update so a crash re-processes
at most the one update that was in flight — never drops, never floods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from sqlmodel import Field, SQLModel

from ..db import create_tables, make_engine, session_scope
from .channels import TelegramError

# How long Telegram holds a long-poll open (seconds) when there's nothing new.
LONG_POLL_SECONDS = 30
# After an unexpected error, wait this long before re-polling (avoid hot-looping).
ERROR_BACKOFF_SECONDS = 5

# A lookup ``(chat_id, message_id) -> {dedup_key, subject, ...} | None`` — the
# outbox's ``sent_message``. Decoupled so classification stays pure + testable.
SentLookup = Callable[[str, int], dict | None]


@dataclass(frozen=True)
class FollowUp:
    """A human reply to a bite we sent — what the handler is asked to answer."""

    chat_id: str
    message_id: int  # the user's message (what we thread the answer under)
    text: str  # the follow-up question
    reply_to_message_id: int  # the bite they replied to
    dedup_key: str  # that bite's dedup key (encodes the initiative)
    subject: str  # that bite's subject (e.g. initiative:<key>)
    bite_text: str  # the replied-to bite's text (fallback grounding context)


class ListenerOffsetRow(SQLModel, table=True):
    """Single-row table holding the next ``getUpdates`` offset (durable)."""

    __tablename__ = "listener_offset"

    id: int = Field(default=1, primary_key=True)
    offset: int = 0


class ListenerStore:
    """Persists the long-poll offset so the listener resumes after a restart."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, ListenerOffsetRow)

    def get_offset(self) -> int:
        with session_scope(self._engine) as session:
            row = session.get(ListenerOffsetRow, 1)
            return row.offset if row else 0

    def set_offset(self, offset: int) -> None:
        with session_scope(self._engine) as session:
            row = session.get(ListenerOffsetRow, 1)
            if row is None:
                row = ListenerOffsetRow(id=1, offset=offset)
            else:
                row.offset = offset
            session.add(row)


class TelegramUpdates:
    """Thin ``getUpdates`` long-poll client (mirrors ``TelegramChannel``'s shape)."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        long_poll_seconds: int = LONG_POLL_SECONDS,
    ) -> None:
        self.token = token
        self._timeout = long_poll_seconds
        # The HTTP read must outlast the server-side long-poll window.
        self._client = client or httpx.Client(timeout=long_poll_seconds + 10)

    def get_updates(self, offset: int) -> list[dict]:
        """Fetch updates with ``update_id >= offset`` (channel posts only)."""
        resp = self._client.post(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            json={
                "offset": offset,
                "timeout": self._timeout,
                "allowed_updates": ["channel_post"],
            },
        )
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TelegramError(f"non-JSON response (HTTP {resp.status_code})")
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return data.get("result") or []

    def close(self) -> None:
        self._client.close()


def classify_update(update: dict, lookup: SentLookup) -> FollowUp | None:
    """Return a :class:`FollowUp` if ``update`` is a human reply to a known bite.

    ``None`` for anything else: not a channel post, not a reply, our own bite, or
    a reply to a message we never sent.
    """
    post = update.get("channel_post")
    if not post:
        return None
    reply_to = post.get("reply_to_message")
    if not reply_to:
        return None
    chat_id = str(post.get("chat", {}).get("id"))
    incoming_id = post.get("message_id")
    # Our own bites arrive as channel_posts too — if we sent this id, it's ours.
    if incoming_id is not None and lookup(chat_id, incoming_id) is not None:
        return None
    replied_id = reply_to.get("message_id")
    bite = lookup(chat_id, replied_id) if replied_id is not None else None
    if bite is None:
        return None
    return FollowUp(
        chat_id=chat_id,
        message_id=incoming_id,
        text=post.get("text") or "",
        reply_to_message_id=replied_id,
        dedup_key=bite["dedup_key"],
        subject=bite["subject"],
        bite_text=reply_to.get("text") or "",
    )


class Listener:
    """Drives the poll → classify → handle → advance-offset loop."""

    def __init__(
        self,
        updates: TelegramUpdates,
        offset_store: ListenerStore,
        lookup: SentLookup,
        handler: Callable[[FollowUp], None],
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._updates = updates
        self._offset_store = offset_store
        self._lookup = lookup
        self._handler = handler
        self._log = on_event or (lambda _msg: None)

    def poll_once(self) -> int:
        """Do one ``getUpdates`` round; handle follow-ups; advance the offset.

        Returns the number of updates consumed. The offset is persisted after
        each update, so a crash re-processes at most the in-flight one.
        """
        offset = self._offset_store.get_offset()
        updates = self._updates.get_updates(offset)
        for update in updates:
            followup = classify_update(update, self._lookup)
            if followup is not None:
                try:
                    self._handler(followup)
                except Exception as exc:  # noqa: BLE001 - one bad reply mustn't wedge
                    self._log(f"handler error on message {followup.message_id}: {exc}")
            # Advance past this update whether handled, ignored, or errored, so a
            # single poison reply can't wedge the queue.
            self._offset_store.set_offset(update["update_id"] + 1)
        return len(updates)

    def run_forever(self) -> None:
        """Long-poll until interrupted; back off briefly on transient errors."""
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                self._log(f"poll error: {exc}; retrying in {ERROR_BACKOFF_SECONDS}s")
                time.sleep(ERROR_BACKOFF_SECONDS)
