"""Video story writer (issue #99): the Claude Code call is faked; offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from daily_agent.agents import video_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.video_writer import (
    MAX_TRANSCRIPT_CHARS,
    clean_article,
    clean_tags,
    render_request,
    write_story,
)
from daily_agent.sources.youtube import Transcript, Video

VIDEO = Video(
    video_id="1FurWDiK9A4",
    title="Did Anthropic just save its IPO?",
    published_at=datetime(2026, 11, 10, 18, 19, tzinfo=timezone.utc),
    description="Links and a sponsor",
    channel_id="UCsBjURrPoezykLs9EqgamOA",
    channel="Fireship",
)


@pytest.fixture
def claude(monkeypatch):
    calls: list[dict] = []
    reply: dict = {}

    async def fake_run_claude(prompt, *, model, system_prompt, json_schema=None, **_):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "system": system_prompt,
                "schema": json_schema,
            }
        )
        return ClaudeResult(text="", structured=reply.get("value"), cost_usd=0.03)

    monkeypatch.setattr(video_writer, "run_claude", fake_run_claude)
    return calls, reply


async def test_writes_a_clean_story(claude):
    calls, reply = claude
    reply["value"] = {
        "headline": '"Anthropic ships Fable 5.2 during its IPO roadshow."',
        "article": "First point.   Still first.\n\n\n\nSecond **point** with `code`."
        * 3,
        "tags": ["#AI Models", "anthropic", "ai-models", "x" * 50, 7, "a", "b", "c"],
    }
    story = await write_story(
        "claude-code:sonnet", VIDEO, Transcript(text="words", language="en")
    )
    assert story.headline == "Anthropic ships Fable 5.2 during its IPO roadshow"
    assert "\n\n\n" not in story.text and "Still first." in story.text
    assert story.tags == ["ai-models", "anthropic", "x" * 30, "a"]
    [call] = calls
    assert call["model"] == "claude-code:sonnet"
    assert call["schema"]["required"] == ["headline", "article", "tags"]
    assert "untrusted data" in call["system"]
    payload = json.loads(call["prompt"])
    assert payload["transcript"] == "words" and payload["channel"] == "Fireship"


async def test_bad_replies_raise(claude):
    _, reply = claude
    for bad in (
        None,
        {"headline": "", "article": "x" * 500},
        {"headline": "Hi", "article": "short"},
    ):
        reply["value"] = bad
        with pytest.raises(ClaudeCodeError):
            await write_story("claude-code:sonnet", VIDEO, None)


async def test_only_claude_code_writes_stories(claude):
    with pytest.raises(ValueError):
        await write_story("google:gemini-3.8-flash", VIDEO, None)
    assert claude[0] == []


def test_request_without_captions_and_with_a_long_transcript():
    bare = json.loads(render_request(VIDEO, None))
    assert bare["transcript"] is None and bare["uploaded"] == "2026-11-10"
    long = json.loads(
        render_request(VIDEO, Transcript(text="x" * (MAX_TRANSCRIPT_CHARS + 10)))
    )
    assert len(long["transcript"]) == MAX_TRANSCRIPT_CHARS
    assert "cut off" in long["transcript_note"]


def test_cleaners():
    assert clean_tags("ai") == []
    assert clean_tags(["Linux Kernel!", "linux-kernel", "--"]) == ["linux-kernel"]
    assert clean_article(None) == ""
    assert clean_article("a\n\n \n\nb") == "a\n\nb"
    assert clean_article("word " * 5000).endswith("…")
