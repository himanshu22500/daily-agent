"""Cache behavior: TTL expiry, permanent entries, clear/stats, disabled mode."""

from __future__ import annotations

import time

from daily_agent.cache import Cache


def test_set_get_roundtrip(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.set("k", {"a": 1})
    assert c.get("k", ttl=60) == {"a": 1}


def test_ttl_expiry(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.set("k", "v")
    assert c.get("k", ttl=0.05) == "v"
    time.sleep(0.08)
    assert c.get("k", ttl=0.05) is None  # expired


def test_permanent_ignores_ttl(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.set("done-issue", {"status": "Done"}, permanent=True)
    time.sleep(0.05)
    assert c.get("done-issue", ttl=0.01) == {"status": "Done"}  # never expires


def test_miss_returns_none(tmp_path):
    c = Cache(tmp_path / "c.db")
    assert c.get("nope", ttl=60) is None


def test_clear_and_stats(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.set("a", 1)
    c.set("b", 2, permanent=True)
    assert c.stats() == (2, 1)
    assert c.clear() == 2
    assert c.stats() == (0, 0)


def test_disabled_is_noop(tmp_path):
    c = Cache(tmp_path / "c.db", enabled=False)
    c.set("k", "v")
    assert c.get("k", ttl=60) is None
    assert c.stats() == (0, 0)
