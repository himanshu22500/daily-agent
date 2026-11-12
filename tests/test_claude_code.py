"""Claude Code backend (issue #95): locked-down `claude -p` calls, all offline.

A fake `claude` script stands in for the CLI: it records how it was called and
prints a canned result.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import claude_code, cover_illustrator, headline_writer
from daily_agent.agents.claude_code import (
    ClaudeCodeError,
    inline_refs,
    is_claude_code,
    model_alias,
    run_claude,
)
from daily_agent.agents.headline_writer import write_headlines
from daily_agent.agents.insight_extractor import extract_insights
from daily_agent.feed.headlines import generate_headlines
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.transcripts import TranscriptMessage
from daily_agent.models import Insight, InsightExtraction

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

FAKE = """\
import json, os, sys, time
record = {
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "cwd": os.getcwd(),
    "cwd_files": os.listdir(os.getcwd()),
    "env_claudecode": os.environ.get("CLAUDECODE"),
    "env_api_keys": [os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")],
}
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as fh:
    json.dump(record, fh)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "sleep":
    time.sleep(30)
if mode == "error":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "api_error_status": 429, "result": "Usage limit reached\\nmore"}))
    sys.exit(1)
if mode == "crash":
    print("boom: not signed in", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": os.environ.get("FAKE_CLAUDE_TEXT", "done"),
                  "structured_output": json.loads(os.environ.get("FAKE_CLAUDE_JSON", "null")),
                  "total_cost_usd": 0.25}))
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE}")
    script.chmod(0o755)
    log = tmp_path / "call.json"
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CLAUDECODE", "1")  # as if run from a Claude session's hook
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key-from-dotenv")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-token")
    monkeypatch.chdir(tmp_path)

    def call() -> dict:
        return json.loads(log.read_text())

    return call


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_calls_are_locked_down_and_isolated(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", '{"word": "ok"}')
    schema = {"type": "object", "properties": {"word": {"type": "string"}}}
    result = await run_claude(
        "secret transcript text",
        model="claude-code:sonnet",
        system_prompt="Be brief.",
        json_schema=schema,
    )
    assert result.structured == {"word": "ok"} and result.cost_usd == 0.25
    call = fake()
    argv = call["argv"]
    assert argv[0] == "-p" and _flag(argv, "--model") == "sonnet"
    assert _flag(argv, "--tools") == ""  # no tools at all
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert json.loads(_flag(argv, "--settings")) == {"disableAllHooks": True}
    assert "--no-session-persistence" in argv
    assert _flag(argv, "--system-prompt") == "Be brief."
    assert json.loads(_flag(argv, "--json-schema")) == schema
    assert _flag(argv, "--output-format") == "json"
    # The prompt goes in on stdin, never on the command line.
    assert call["stdin"] == "secret transcript text"
    assert all("secret" not in arg for arg in argv)
    # Fresh top-level run, from an empty directory (no project CLAUDE.md).
    assert call["env_claudecode"] is None
    # Claude Code's own sign-in, never an API key from .env.
    assert call["env_api_keys"] == [None, None]
    assert call["cwd_files"] == [] and "daily-agent-claude-" in call["cwd"]
    assert not os.path.exists(call["cwd"])  # cleaned up afterwards


async def test_errors_surface_as_claude_code_errors(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(ClaudeCodeError, match="429: Usage limit reached$"):
        await run_claude("x", model="claude-code:sonnet", system_prompt="s")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    with pytest.raises(ClaudeCodeError, match="exited 2: boom: not signed in"):
        await run_claude("x", model="claude-code:sonnet", system_prompt="s")


async def test_missing_structured_output_is_an_error(fake):
    with pytest.raises(ClaudeCodeError, match="no structured output"):
        await run_claude(
            "x", model="claude-code:sonnet", system_prompt="s", json_schema={}
        )


async def test_a_hung_call_is_killed(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    with pytest.raises(ClaudeCodeError, match="timed out"):
        await run_claude("x", model="claude-code:sonnet", system_prompt="s", timeout=1)


async def test_missing_cli_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(ClaudeCodeError, match="claude CLI not found"):
        await run_claude("x", model="claude-code:sonnet", system_prompt="s")


def test_model_strings_and_schema_inlining():
    assert is_claude_code("claude-code:fable") and not is_claude_code("google:x")
    assert model_alias("claude-code: opus ") == "opus"
    with pytest.raises(ValueError):
        model_alias("claude-code:")
    flat = json.dumps(inline_refs(InsightExtraction.model_json_schema()))
    assert "$ref" not in flat and "$defs" not in flat and "canonical_key" in flat


async def test_extraction_runs_through_claude_code(monkeypatch):
    seen = {}

    async def fake_run(prompt, **kwargs):
        seen.update(kwargs, prompt=prompt)
        return claude_code.ClaudeResult(
            text="",
            structured={
                "candidates": [
                    {
                        "text": "Use X.",
                        "canonical_key": "use-x",
                        "type": "technique",
                        "tags": ["x"],
                        "score": 0.7,
                    }
                ]
            },
            cost_usd=0.0,
        )

    monkeypatch.setattr("daily_agent.agents.insight_extractor.run_claude", fake_run)
    message = TranscriptMessage("s", "main", "2026-11-11T10:00:00Z", "user", "hello")
    found = await extract_insights("claude-code:sonnet", [message])
    assert [c.canonical_key for c in found] == ["use-x"]
    assert seen["model"] == "claude-code:sonnet" and "hello" in seen["prompt"]
    assert "candidates" in json.dumps(seen["json_schema"])

    async def bad_run(prompt, **kwargs):
        return claude_code.ClaudeResult("", {"candidates": "nope"}, 0.0)

    monkeypatch.setattr("daily_agent.agents.insight_extractor.run_claude", bad_run)
    with pytest.raises(ClaudeCodeError, match="unexpected extraction shape"):
        await extract_insights("claude-code:sonnet", [message])


async def test_covers_are_drawn_through_claude_code(monkeypatch):
    async def fake_run(prompt, **kwargs):
        assert kwargs["model"] == "claude-code:fable" and "json_schema" not in kwargs
        assert "rejected by the validator" in prompt and "- bad size" in prompt
        return claude_code.ClaudeResult("Sure:\n<svg>art</svg>\n", None, 0.0)

    monkeypatch.setattr(cover_illustrator, "run_claude", fake_run)
    svg = await cover_illustrator.draw_cover(
        "claude-code:fable", {"key": "k", "text": "t"}, ["bad size"]
    )
    assert svg == "<svg>art</svg>"


async def test_headlines_keep_only_requested_keys(monkeypatch):
    async def fake_run(prompt, **kwargs):
        sent = json.loads(prompt)
        assert [r["key"] for r in sent] == ["insight:a", "insight:b"]
        return claude_code.ClaudeResult(
            "",
            {
                "headlines": [
                    {"key": "insight:a", "headline": "A headline"},
                    {"key": "insight:zzz", "headline": "Not asked for"},
                    {"key": "insight:b", "headline": "  "},
                ]
            },
            0.0,
        )

    monkeypatch.setattr(headline_writer, "run_claude", fake_run)
    records = [{"key": "insight:a", "text": "A."}, {"key": "insight:b", "text": "B."}]
    assert await write_headlines("claude-code:sonnet", records) == {
        "insight:a": "A headline"
    }
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_headlines("google:gemini-3.8-flash", records)


def _store(tmp_path, *keys):
    store = InsightStore(tmp_path / "h.db")
    for n, key in enumerate(keys):
        store.add(
            Insight(key=f"insight:{key}", text=f"{key}.", captured_at=T0, score=n)
        )
    return store


async def test_generate_headlines_batches_and_isolates_failures(tmp_path):
    store = _store(tmp_path, "a", "b", "c", "d", "e")
    store.set_title("insight:e", "Kept as is")
    batches: list[list[str]] = []

    async def write(batch):
        keys = [r["key"] for r in batch]
        batches.append(keys)
        if "insight:c" in keys:
            raise RuntimeError("claude error 529: overloaded\ntrace")
        return {k: f"Title {k[-1]}" for k in keys}

    run = await generate_headlines(store, write, batch_size=2)
    assert sorted(k for b in batches for k in b) == [
        "insight:a",
        "insight:b",
        "insight:c",
        "insight:d",
    ]
    assert run.written == 2 and run.failed == ["claude error 529: overloaded"]
    assert store.get("insight:e").title == "Kept as is"
    assert len(store.missing_titles()) == 2  # the failed batch waits for next run

    limited = await generate_headlines(store, write, limit=1)
    assert limited.written + len(limited.failed) == 1


def test_titles_generate_command(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    InsightStore(db).add(Insight(key="insight:a", text="A.", captured_at=T0))

    async def fake(model, batch):
        assert model == "claude-code:haiku"
        return {r["key"]: "Short and specific" for r in batch}

    monkeypatch.setattr(cli, "write_headlines", fake)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(db))
    cli.get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            cli.app, ["insights", "titles-generate", "--model", "claude-code:haiku"]
        )
    finally:
        cli.get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert "Wrote 1 headline(s) with claude-code:haiku" in result.output
    assert InsightStore(db).get("insight:a").title == "Short and specific"
