"""Multi-stream routing — bites go to the right per-type channel (offline)."""

from __future__ import annotations

from daily_agent.feed.channel_registry import ChannelRegistry
from daily_agent.feed.channels import MultiStreamTelegramChannel, stream_for
from daily_agent.feed.outbox import OutboxItem


def _item(kind: str, content: str = "hi", n: int = 1) -> OutboxItem:
    return OutboxItem(
        id=n, dedup_key=f"k{n}", subject="s", kind=kind, content=content, attempts=0
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
    """Records (channel_id, content) it was asked to post."""

    posted: list[tuple[int, str]] = []

    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id

    def send(self, item: OutboxItem) -> None:
        _FakeBot.posted.append((self.channel_id, item.content))

    def close(self) -> None: ...


def _channel(tmp_path):
    _FakeBot.posted = []
    reg = ChannelRegistry(tmp_path / "c.db")
    prov = _FakeProvisioner()
    ch = MultiStreamTelegramChannel(reg, prov, bot_factory=_FakeBot)
    return ch, reg, prov


def test_default_resolver_maps_chapter_to_org_activity():
    assert stream_for(_item("chapter"))[0] == "org-activity"
    assert stream_for(_item("something-else"))[0] == "org-activity"  # default


def test_same_stream_provisions_once_and_posts_to_one_channel(tmp_path):
    ch, reg, prov = _channel(tmp_path)
    ch.send(_item("chapter", "a", 1))
    ch.send(_item("chapter", "b", 2))
    # One channel provisioned, both posts to it.
    assert len(prov.created) == 1
    chan = reg.get("org-activity").channel_id
    assert _FakeBot.posted == [(chan, "a"), (chan, "b")]


def test_distinct_streams_route_to_distinct_channels(tmp_path):
    _FakeBot.posted = []
    reg = ChannelRegistry(tmp_path / "c.db")
    prov = _FakeProvisioner()
    # Custom resolver: route by kind to two streams.
    resolver = lambda item: (  # noqa: E731
        ("insights", "Insights") if item.kind == "insight" else ("org-activity", "Org")
    )
    ch = MultiStreamTelegramChannel(reg, prov, bot_factory=_FakeBot, resolver=resolver)
    ch.send(_item("chapter", "activity", 1))
    ch.send(_item("insight", "learned", 2))
    assert len(prov.created) == 2
    act = reg.get("org-activity").channel_id
    ins = reg.get("insights").channel_id
    assert act != ins
    assert (act, "activity") in _FakeBot.posted
    assert (ins, "learned") in _FakeBot.posted
