"""Local automation guardrails for automatic insight flushing.

launchd can watch the transcript directory, but it does not know when a Claude
session has formally closed. These helpers make that trigger safe: one flush at a
time, skip very recent runs, and optionally wait until transcript files stop
changing before collect/feed starts.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def sidecar_path(db_path: str | Path, suffix: str) -> Path:
    return Path(f"{db_path}.{suffix}")


@dataclass(frozen=True)
class FlushPaths:
    lock: Path
    stamp: Path

    @classmethod
    def for_db(cls, db_path: str | Path) -> "FlushPaths":
        return cls(
            lock=sidecar_path(db_path, "insights-flush.lock"),
            stamp=sidecar_path(db_path, "insights-flush.stamp"),
        )


class RunLock:
    """Tiny cross-process lock based on atomic file creation."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._is_stale():
                return False
            self.path.unlink(missing_ok=True)
            return self.acquire()
        os.write(self._fd, f"{os.getpid()}\n".encode())
        return True

    def _is_stale(self) -> bool:
        if self.ttl_seconds <= 0:
            return False
        try:
            return self._now() - self.path.stat().st_mtime > self.ttl_seconds
        except FileNotFoundError:
            return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def recently_ran(
    stamp_path: str | Path,
    *,
    debounce_seconds: int,
    now: Callable[[], float] | None = None,
) -> bool:
    if debounce_seconds <= 0:
        return False
    stamp = Path(stamp_path)
    if not stamp.exists():
        return False
    try:
        last = float(stamp.read_text().strip())
    except ValueError:
        last = stamp.stat().st_mtime
    return (now or time.time)() - last < debounce_seconds


def mark_ran(stamp_path: str | Path, *, now: Callable[[], float] | None = None) -> None:
    stamp = Path(stamp_path)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str((now or time.time)()))


def _fingerprint(path: Path) -> tuple[tuple[str, int, int], ...]:
    if not path.exists():
        return ()
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return tuple(
        sorted((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    )


def wait_for_quiet_path(
    path: str | Path,
    *,
    quiet_seconds: float,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 300.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    fingerprint: Callable[[Path], tuple] | None = None,
) -> bool:
    """Return True once ``path`` has been stable for ``quiet_seconds``.

    False means the timeout elapsed first. ``quiet_seconds <= 0`` returns
    immediately, which keeps unit tests and manual one-shots fast.
    """
    if quiet_seconds <= 0:
        return True
    clock = monotonic or time.monotonic
    nap = sleep or time.sleep
    fp = fingerprint or _fingerprint
    target = Path(path)
    start = clock()
    stable_since = start
    last = fp(target)
    while clock() - start < timeout_seconds:
        nap(poll_seconds)
        current = fp(target)
        now = clock()
        if current != last:
            last = current
            stable_since = now
            continue
        if now - stable_since >= quiet_seconds:
            return True
    return False
