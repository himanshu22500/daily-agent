"""Outbox: at-least-once delivery, dedup, retry/backoff (no network, no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daily_agent.feed.outbox import MAX_ATTEMPTS, Outbox, OutboxItem, SendReceipt
from daily_agent.models import Bite


def _bite(key: str = "pr:api#1@merged", subject: str = "repo:api") -> Bite:
    return Bite(dedup_key=key, subject=subject, kind="pr_merged", content="hi")


class _Collector:
    """A channel that records what it was asked to send (returns no receipt)."""

    name = "collector"

    def __init__(self) -> None:
        self.sent: list[OutboxItem] = []

    def send(self, item: OutboxItem) -> None:
        self.sent.append(item)


class _Receipting:
    """A channel that returns a :class:`SendReceipt`, like Telegram does."""

    name = "receipting"

    def __init__(self, chat_id: str = "-100", start_id: int = 100) -> None:
        self.chat_id = chat_id
        self._next = start_id

    def send(self, item: OutboxItem) -> SendReceipt:
        receipt = SendReceipt(chat_id=self.chat_id, message_id=self._next)
        self._next += 1
        return receipt


class _Flaky:
    """Fails the first ``fail_times`` sends, then succeeds."""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def send(self, item: OutboxItem) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")


def test_enqueue_is_idempotent_by_dedup_key(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    assert ob.enqueue(_bite()) is True
    assert ob.enqueue(_bite()) is False  # same key again
    assert ob.stats()["pending"] == 1


def test_drain_delivers_then_does_not_redeliver(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    ch = _Collector()
    r1 = ob.drain(ch)
    assert r1.sent == 1 and len(ch.sent) == 1
    # Draining again sends nothing — it's already delivered.
    r2 = ob.drain(ch)
    assert r2.sent == 0 and len(ch.sent) == 1


def test_delivered_bite_cannot_be_reenqueued(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    ob.drain(_Collector())
    # The ledger blocks re-enqueue of an already-delivered key.
    assert ob.enqueue(_bite()) is False
    assert _bite().dedup_key in ob.delivered_keys()


def test_failed_send_is_retried_with_backoff_then_succeeds(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    flaky = _Flaky(fail_times=1)
    now = datetime.now(timezone.utc)

    # First drain fails -> deferred (not_before set in the future).
    r1 = ob.drain(flaky, now=now)
    assert r1.sent == 0 and r1.failed == 1
    assert ob.stats()["failed"] == 1

    # Not yet due -> still nothing sent.
    assert ob.drain(flaky, now=now).sent == 0

    # After backoff window -> retried and delivered.
    later = now + timedelta(minutes=10)
    r2 = ob.drain(flaky, now=later)
    assert r2.sent == 1
    assert ob.stats()["sent"] == 1


def test_poison_bite_goes_dead_after_max_attempts(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    flaky = _Flaky(fail_times=MAX_ATTEMPTS)
    now = datetime.now(timezone.utc)
    for i in range(MAX_ATTEMPTS):
        ob.drain(flaky, now=now + timedelta(hours=i))
    assert ob.stats()["dead"] == 1
    assert ob.stats()["pending"] == 0


def test_watermark_advances_to_latest_delivery(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite("pr:api#1@opened"))
    ob.enqueue(_bite("pr:api#2@merged"))
    t0 = datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc)
    ob.drain(_Collector(), now=t0)
    assert ob.watermark_for("repo:api") == t0


def test_limit_caps_deliveries_per_drain(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    for n in range(5):
        ob.enqueue(_bite(f"pr:api#{n}@merged"))
    ch = _Collector()
    assert ob.drain(ch, limit=2).sent == 2
    assert ob.stats()["pending"] == 3


def test_send_receipt_is_persisted_and_round_trips(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite(key="chapter:comms-v3:abc", subject="initiative:comms-v3"))
    ob.drain(_Receipting(chat_id="-100", start_id=100))

    found = ob.sent_message("-100", 100)
    assert found is not None
    assert found["dedup_key"] == "chapter:comms-v3:abc"
    assert found["subject"] == "initiative:comms-v3"
    # chat_id is matched as text, message_id as int.
    assert ob.sent_message("-100", 100) == ob.sent_message(-100, "100")


def test_no_receipt_when_channel_returns_none(tmp_path):
    # Console/file channels return None — nothing is recorded to disambiguate.
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    ob.drain(_Collector())
    assert ob.sent_message("-100", 100) is None


def test_unknown_message_lookup_returns_none(tmp_path):
    ob = Outbox(tmp_path / "f.db")
    ob.enqueue(_bite())
    ob.drain(_Receipting(chat_id="-100", start_id=100))
    # A reply to a message we never sent is not a known bite.
    assert ob.sent_message("-100", 999) is None
    assert ob.sent_message("-200", 100) is None
