"""Insight auto-flush guardrails: lock, debounce, and quiet-path wait."""

from __future__ import annotations

from pathlib import Path

from daily_agent.feed.insights_flush import (
    FlushPaths,
    RunLock,
    mark_ran,
    recently_ran,
    wait_for_quiet_path,
)


def test_flush_paths_are_sidecars_of_db():
    paths = FlushPaths.for_db("daily_agent.db")

    assert paths.lock == Path("daily_agent.db.insights-flush.lock")
    assert paths.stamp == Path("daily_agent.db.insights-flush.stamp")


def test_run_lock_prevents_concurrent_acquire_and_releases(tmp_path):
    path = tmp_path / "flush.lock"
    first = RunLock(path, ttl_seconds=300)
    second = RunLock(path, ttl_seconds=300)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_run_lock_reclaims_stale_lock(tmp_path):
    now = [1_000.0]
    path = tmp_path / "flush.lock"
    path.write_text("old")
    # Force old mtime for the stale check.
    old = now[0] - 1_000
    path.touch()
    import os

    os.utime(path, (old, old))

    lock = RunLock(path, ttl_seconds=300, now=lambda: now[0])

    assert lock.acquire() is True
    lock.release()


def test_debounce_stamp_roundtrips(tmp_path):
    stamp = tmp_path / "flush.stamp"
    now = [100.0]

    assert recently_ran(stamp, debounce_seconds=20, now=lambda: now[0]) is False
    mark_ran(stamp, now=lambda: now[0])
    assert recently_ran(stamp, debounce_seconds=20, now=lambda: now[0]) is True
    now[0] = 121.0
    assert recently_ran(stamp, debounce_seconds=20, now=lambda: now[0]) is False


def test_wait_for_quiet_path_returns_after_stable_window():
    clock = [0.0]
    fingerprints = iter([("a",), ("b",), ("b",), ("b",)])

    def monotonic():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    assert wait_for_quiet_path(
        "/tmp/irrelevant",
        quiet_seconds=2,
        poll_seconds=1,
        timeout_seconds=10,
        monotonic=monotonic,
        sleep=sleep,
        fingerprint=lambda _path: next(fingerprints),
    )


def test_wait_for_quiet_path_times_out_when_changes_continue():
    clock = [0.0]
    counter = [0]

    def monotonic():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    def changing(_path):
        counter[0] += 1
        return (counter[0],)

    assert (
        wait_for_quiet_path(
            "/tmp/irrelevant",
            quiet_seconds=2,
            poll_seconds=1,
            timeout_seconds=3,
            monotonic=monotonic,
            sleep=sleep,
            fingerprint=changing,
        )
        is False
    )
