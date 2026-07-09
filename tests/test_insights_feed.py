"""Insight feed resurfacing: stored insights -> outbox -> per-type streams."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.feed.channel_registry import ChannelRegistry
from daily_agent.feed.channels import MultiStreamTelegramChannel
from daily_agent.feed.insights_feed import (
    enqueue_new_insights,
    insight_stream_resolver,
    insight_to_bite,
)
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.outbox import Outbox, OutboxItem
from daily_agent.models import Insight


def _insight(
    key: str = "insight:mock-transport",
    *,
    text: str = "Use httpx.MockTransport for offline source-client tests.",
    type: str = "technique",
    tags: list[str] | None = None,
    score: float = 0.8,
    status: str = "new",
) -> Insight:
    return Insight(
        key=key,
        text=text,
        type=type,
        tags=tags or ["testing", "httpx"],
        score=score,
        source_session="s1",
        git_branch="main",
        captured_at=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        status=status,
    )


class _FakeProvisioner:
    def __init__(self) -> None:
        self.created: list[str] = []
        self._next = 1000

    def create_channel(self, title: str, about: str = "") -> int:
        self.created.append(title)
        self._next += 1
        return self._next

    def delete_channel(self, channel_id: int) -> None: ...


class _FakeBot:
    posted: list[tuple[int, str]] = []

    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id

    def send(self, item: OutboxItem) -> None:
        _FakeBot.posted.append((self.channel_id, item.content))

    def close(self) -> None: ...


def test_insight_to_bite_uses_insight_subject_and_kind():
    bite = insight_to_bite(_insight())

    assert bite.dedup_key == "insight:mock-transport"
    assert bite.subject == "insight:mock-transport"
    assert bite.kind == "insight"
    assert "Use httpx.MockTransport" in bite.content
    assert "type: technique" in bite.content
    assert "tags: testing, httpx" in bite.content
    assert "branch: main" in bite.content


def test_enqueue_new_insights_queues_only_new_items_and_marks_queued(tmp_path):
    db = tmp_path / "f.db"
    store = InsightStore(db)
    outbox = Outbox(db)
    store.add(_insight(key="insight:a", score=0.3))
    store.add(_insight(key="insight:b", score=0.9))
    store.add(_insight(key="insight:old", status="queued"))

    assert enqueue_new_insights(store, outbox) == 2
    assert [i.key for i in store.by_status("new")] == []
    assert {i.key for i in store.by_status("queued")} == {
        "insight:a",
        "insight:b",
        "insight:old",
    }
    assert outbox.stats()["pending"] == 2

    assert enqueue_new_insights(store, outbox) == 0
    assert outbox.stats()["pending"] == 2


def test_multistream_resolver_routes_insights_by_type(tmp_path):
    db = tmp_path / "f.db"
    store = InsightStore(db)
    outbox = Outbox(db)
    store.add(_insight(key="insight:mock-transport", type="technique"))
    store.add(
        _insight(
            key="insight:sqlite-schema",
            text="SQLite create_all does not add missing columns.",
            type="gotcha",
            tags=["sqlite"],
            score=0.7,
        )
    )
    enqueue_new_insights(store, outbox)

    _FakeBot.posted = []
    registry = ChannelRegistry(db)
    provisioner = _FakeProvisioner()
    channel = MultiStreamTelegramChannel(
        registry,
        provisioner,
        bot_factory=_FakeBot,
        resolver=insight_stream_resolver(store),
    )

    result = outbox.drain(channel, kind="insight")

    assert result.sent == 2
    assert provisioner.created == [
        "daily-agent · Insights · Technique",
        "daily-agent · Insights · Gotcha",
    ]
    technique = registry.get("insight:technique").channel_id
    gotcha = registry.get("insight:gotcha").channel_id
    assert technique != gotcha
    assert (technique, store.get("insight:mock-transport").text + "\n\n"
            "type: technique | tags: testing, httpx | branch: main") in _FakeBot.posted
    assert any(channel_id == gotcha for channel_id, _ in _FakeBot.posted)
