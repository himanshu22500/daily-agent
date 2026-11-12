"""Timed sweep over every project's transcripts (issue #91), all offline."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents.insight_extractor import (
    REQUEST_TIMEOUT_SECONDS,
    build_insight_extractor,
)
from daily_agent.feed.covers import CoverStore
from daily_agent.feed.insights_capture import collect_insights
from daily_agent.feed.insights_flush import FlushPaths, RunLock
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.insights_sweep import pending_transcripts, sweep_insights
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.transcripts import all_session_files
from daily_agent.models import InsightCandidate

COVER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 400">'
    '<rect width="800" height="400" fill="#274690"/>'
    + '<rect x="1" y="1" width="2" height="2" fill="#123456"/>' * 12
    + "</svg>"
)
NOW = time.time()  # real clock: cursors record real read times
SETTLE = 600


def _say(text: str, role: str = "assistant") -> str:
    record = {"type": role, "sessionId": "s", "timestamp": "2026-11-11T10:00:00Z"}
    if role == "user":
        record["message"] = {"content": text}
    else:
        record["message"] = {"content": [{"type": "text", "text": text}]}
    return json.dumps(record)


def _transcript(root: Path, project: str, name: str, *lines: str, age: float) -> Path:
    path = root / project / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (NOW - age, NOW - age))
    return path


def _append(path: Path, *lines: str, age: float) -> None:
    with path.open("a") as fh:
        fh.write("\n".join(lines) + "\n")
    os.utime(path, (NOW - age, NOW - age))


def _extractor(calls: list[str], *, fail: set[str] = frozenset()):
    async def extract(messages):
        text = messages[-1].text
        calls.append(text)
        if text in fail:
            raise RuntimeError(f"status_code: 429, quota for {text}\ntrace")
        return [
            InsightCandidate(
                text=f"Lesson from {text}.",
                canonical_key=f"lesson-{text}",
                type="gotcha",
                tags=["t"],
                score=0.8,
            )
        ]

    return extract


async def _sweep(store, root, extractor, **kwargs):
    return await sweep_insights(
        store, root, "insight:", extractor, settle_seconds=SETTLE, now=NOW, **kwargs
    )


def test_all_session_files_spans_projects_but_not_subagents(tmp_path):
    a = _transcript(tmp_path, "-p-one", "s1", _say("x"), age=0)
    b = _transcript(tmp_path, "-p-two", "s2", _say("y"), age=0)
    nested = tmp_path / "-p-one" / "s1" / "subagents" / "a.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text(_say("z"))
    assert all_session_files(tmp_path) == [a, b]
    assert all_session_files(tmp_path / "missing") == []


async def test_reads_settled_transcripts_in_every_project(tmp_path):
    root = tmp_path / "projects"
    _transcript(root, "-zed-only", "a", _say("alpha"), age=3600)
    _transcript(root, "-terminal", "b", _say("insight: marked", "user"), age=900)
    _transcript(root, "-zed-only", "live", _say("live"), age=60)  # mid-conversation
    store = InsightStore(tmp_path / "db.sqlite")
    calls: list[str] = []

    result = await _sweep(store, root, _extractor(calls))

    assert result.read == 2 and result.waiting == 1 and not result.failed
    assert calls == ["alpha", "insight: marked"]  # oldest write first
    assert result.captured.marked == 1 and result.captured.extracted == 2
    assert {i.key for i in store.all()} >= {"insight:lesson-alpha"}
    # The live transcript was left alone; it's read once it settles.
    assert store.cursor(str(root / "-zed-only" / "live.jsonl")) == 0


async def test_unchanged_transcripts_are_not_read_again(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    path = _transcript(root, "-p", "a", _say("one"), age=3600)
    store = InsightStore(tmp_path / "db.sqlite")
    calls: list[str] = []
    await _sweep(store, root, _extractor(calls))

    # Next sweep: nothing written since it was read, so the file isn't opened.
    import daily_agent.feed.insights_capture as capture

    def boom(_path):
        raise AssertionError("read an unchanged transcript")

    monkeypatch.setattr(capture, "read_lines", boom)
    later = await sweep_insights(
        store,
        root,
        "insight:",
        _extractor(calls),
        settle_seconds=SETTLE,
        now=NOW + 1800,
    )
    assert later.read == 0 and calls == ["one"]
    monkeypatch.undo()

    # A resumed conversation is read from where the last sweep stopped.
    _append(path, _say("two"), age=-1000)  # written after the recorded read
    again = await sweep_insights(
        store,
        root,
        "insight:",
        _extractor(calls),
        settle_seconds=SETTLE,
        now=NOW + 3000,
    )
    assert again.read == 1 and calls == ["one", "two"]
    assert again.captured.scanned == 1


async def test_a_failing_transcript_is_retried_later_and_does_not_stop_others(tmp_path):
    root = tmp_path / "projects"
    bad = _transcript(root, "-p", "bad", _say("bad"), age=3000)
    _transcript(root, "-p", "good", _say("good"), age=2000)
    store = InsightStore(tmp_path / "db.sqlite")
    calls: list[str] = []

    result = await _sweep(store, root, _extractor(calls, fail={"bad"}))

    assert result.read == 1 and not result.stopped_early
    assert result.failed == {str(bad): "status_code: 429, quota for bad"}
    assert store.cursor(str(bad)) == 0  # retried next sweep
    assert store.get("insight:lesson-good") is not None


async def test_repeated_failures_end_the_run_early(tmp_path):
    root = tmp_path / "projects"
    for n in range(5):
        _transcript(root, "-p", f"s{n}", _say(f"s{n}"), age=5000 - n)
    store = InsightStore(tmp_path / "db.sqlite")
    calls: list[str] = []
    every = {f"s{n}" for n in range(5)}

    result = await _sweep(
        store, root, _extractor(calls, fail=every), max_consecutive_failures=2
    )

    assert result.stopped_early and len(result.failed) == 2 and calls == ["s0", "s1"]


async def test_progress_hook_runs_before_each_transcript(tmp_path):
    root = tmp_path / "projects"
    _transcript(root, "-p", "a", _say("a"), age=3000)
    _transcript(root, "-p", "b", _say("b"), age=2000)
    ticks: list[int] = []
    await _sweep(
        InsightStore(tmp_path / "db.sqlite"),
        root,
        _extractor([]),
        on_progress=lambda: ticks.append(1),
    )
    assert len(ticks) == 2


async def test_sweep_and_project_flush_share_cursors(tmp_path):
    root = tmp_path / "projects"
    _transcript(root, "-p", "a", _say("a"), age=3000)
    store = InsightStore(tmp_path / "db.sqlite")
    calls: list[str] = []
    await collect_insights(store, root / "-p", "insight:", _extractor(calls))
    result = await _sweep(store, root, _extractor(calls))
    assert result.read == 0 and calls == ["a"]


def test_pending_lists_only_files_written_since_last_read(tmp_path):
    root = tmp_path / "projects"
    _transcript(root, "-p", "old", _say("o"), age=3000)
    _transcript(root, "-p", "new", _say("n"), age=30)
    store = InsightStore(tmp_path / "db.sqlite")
    pending = pending_transcripts(store, root, settle_seconds=SETTLE, now=NOW)
    assert [(p.path.stem, p.settled) for p in pending] == [
        ("old", True),
        ("new", False),
    ]


def test_run_lock_refresh_keeps_a_long_run_from_looking_stale(tmp_path):
    path = tmp_path / "flush.lock"
    lock = RunLock(path, ttl_seconds=300)
    assert lock.acquire()
    os.utime(path, (1, 1))  # looks abandoned
    lock.refresh()
    assert RunLock(path, ttl_seconds=300).acquire() is False
    lock.release()


def test_extractor_has_a_request_timeout():
    agent = build_insight_extractor("test")
    assert agent.model_settings["timeout"] == REQUEST_TIMEOUT_SECONDS


# --- CLI --------------------------------------------------------------------- #


def _cli(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DAILY_AGENT_INSIGHTS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DAILY_AGENT_MODEL", "test")
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, ["insights", "sweep", *args])
    finally:
        cli.get_settings.cache_clear()


def _real_transcripts(tmp_path):
    root = tmp_path / "projects"
    for name, age in (("a", 3600), ("b", 0)):
        path = root / "-p" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_say(name) + "\n")
        mtime = Path(path).stat().st_mtime - age
        os.utime(path, (mtime, mtime))


def test_dry_run_reports_without_calling_the_model(monkeypatch, tmp_path):
    _real_transcripts(tmp_path)

    async def never(*_args):
        raise AssertionError("dry run called the model")

    monkeypatch.setattr(cli, "extract_insights", never)
    result = _cli(monkeypatch, tmp_path, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "1 transcript(s) to read, 1 message(s)" in result.output
    assert "1 still being written" in result.output
    assert (
        InsightStore(tmp_path / "cli.db").cursor(
            str(tmp_path / "projects" / "-p" / "a.jsonl")
        )
        == 0
    )


def test_sweep_publishes_and_queues_but_never_delivers(monkeypatch, tmp_path):
    _real_transcripts(tmp_path)
    models: list[str] = []
    titled: list[list[str]] = []
    drawn: list[str] = []

    async def fake_extract(model, messages):
        models.append(model)
        return await _extractor([])(messages)

    async def fake_headlines(model, batch):
        models.append(model)
        titled.append([r["key"] for r in batch])
        return {r["key"]: "A written headline" for r in batch}

    async def fake_draw(model, record, feedback=None):
        models.append(model)
        drawn.append(record["key"])
        return COVER

    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    monkeypatch.setattr(cli, "write_headlines", fake_headlines)
    monkeypatch.setattr(cli, "draw_cover", fake_draw)
    monkeypatch.setenv("DAILY_AGENT_HEADLINE_MODEL", "claude-code:h")
    monkeypatch.setenv("DAILY_AGENT_COVER_MODEL", "claude-code:c")
    result = _cli(monkeypatch, tmp_path, "--settle-minutes", "10")
    assert result.exit_code == 0, result.output
    assert "Sweep read 1 transcript(s): captured 1 new" in result.output
    assert "queued 1 for Telegram; 1 still being written" in result.output
    assert "Headlines: wrote 1" in result.output and "Covers: drew 1" in result.output
    assert models == ["test", "claude-code:h", "claude-code:c"]
    assert titled == [["insight:lesson-a"]] and drawn == ["insight:lesson-a"]
    db = tmp_path / "cli.db"
    assert InsightStore(db).get("insight:lesson-a").title == "A written headline"
    assert CoverStore(db).get("insight:lesson-a") == COVER
    stats = Outbox(db).stats()
    assert stats["pending"] == 1 and stats.get("sent", 0) == 0
    assert not FlushPaths.for_db(db).lock.exists()


def test_sweep_caps_of_zero_skip_headlines_and_covers(monkeypatch, tmp_path):
    _real_transcripts(tmp_path)

    async def fake_extract(model, messages):
        return await _extractor([])(messages)

    async def never(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    monkeypatch.setattr(cli, "write_headlines", never)
    monkeypatch.setattr(cli, "draw_cover", never)
    result = _cli(monkeypatch, tmp_path, "--max-titles", "0", "--max-covers", "0")
    assert result.exit_code == 0, result.output
    assert "Headlines" not in result.output and "Covers" not in result.output


def test_sweep_skips_while_a_flush_holds_the_lock(monkeypatch, tmp_path):
    _real_transcripts(tmp_path)
    lock = RunLock(FlushPaths.for_db(tmp_path / "cli.db").lock, ttl_seconds=600)
    assert lock.acquire()
    try:
        result = _cli(monkeypatch, tmp_path)
    finally:
        lock.release()
    assert result.exit_code == 0 and "already running" in result.output


def test_sweep_exits_nonzero_when_it_stops_early(monkeypatch, tmp_path):
    _real_transcripts(tmp_path)

    async def down(model, messages):
        raise RuntimeError("status_code: 503")

    monkeypatch.setattr(cli, "extract_insights", down)
    result = _cli(monkeypatch, tmp_path, "--max-failures", "1")
    assert result.exit_code == 1
    assert (
        "1 failed" in result.output
        and "Stopped after repeated failures" in result.output
    )
