"""Channel registry + ensure/reap orchestration (offline, fake provisioner)."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_agent.feed.channel_registry import (
    ChannelRegistry,
    ensure_channel,
    reap_stale,
)


class _FakeProvisioner:
    """Hands out incrementing channel ids; records create/delete calls."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[int] = []
        self._next = 1000

    def create_channel(self, title: str, about: str = "") -> int:
        self.created.append(title)
        self._next += 1
        return self._next

    def delete_channel(self, channel_id: int) -> None:
        self.deleted.append(channel_id)


def _at(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc)


def test_registry_roundtrip(tmp_path):
    r = ChannelRegistry(tmp_path / "c.db")
    assert r.get("org-activity") is None
    r.put("org-activity", 555, "Activity", now=_at(1))
    rec = r.get("org-activity")
    assert rec.channel_id == 555 and rec.title == "Activity"
    r.delete("org-activity")
    assert r.get("org-activity") is None


def test_ensure_provisions_once_then_caches(tmp_path):
    r = ChannelRegistry(tmp_path / "c.db")
    p = _FakeProvisioner()

    cid1 = ensure_channel("insights", "Insights", registry=r, provisioner=p, now=_at(1))
    cid2 = ensure_channel("insights", "Insights", registry=r, provisioner=p, now=_at(2))

    assert cid1 == cid2  # same channel reused
    assert len(p.created) == 1  # provisioned exactly once
    # last_used advanced on the second (cached) call
    assert r.get("insights").last_used_at == _at(2).isoformat()


def test_ensure_separate_streams_get_separate_channels(tmp_path):
    r = ChannelRegistry(tmp_path / "c.db")
    p = _FakeProvisioner()
    a = ensure_channel("org-activity", "Activity", registry=r, provisioner=p)
    b = ensure_channel("insights", "Insights", registry=r, provisioner=p)
    assert a != b
    assert len(p.created) == 2


def test_reap_deletes_only_stale(tmp_path):
    r = ChannelRegistry(tmp_path / "c.db")
    p = _FakeProvisioner()
    # 'old' last used day 1; 'fresh' last used day 9.
    ensure_channel("old", "Old", registry=r, provisioner=p, now=_at(1))
    ensure_channel("fresh", "Fresh", registry=r, provisioner=p, now=_at(9))

    reaped = reap_stale(r, p, max_idle_days=3, now=_at(10))
    assert reaped == ["old"]
    assert r.get("old") is None
    assert r.get("fresh") is not None
    assert len(p.deleted) == 1
