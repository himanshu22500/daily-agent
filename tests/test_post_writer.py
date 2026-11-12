"""Instagram stories written by Claude (issue #107). Offline: run_claude is faked."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from daily_agent.agents import post_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.post_writer import render_request, write_post_story
from daily_agent.sources.instagram import Post, PostImage


def make_post(
    kind: str = "CAROUSEL_ALBUM", caption: str = "5 caching mistakes"
) -> Post:
    return Post(
        shortcode="Cpost000001",
        username="bytebytego",
        caption=caption,
        media_type=kind,
        product_type="REELS" if kind == "VIDEO" else "FEED",
        permalink="https://www.instagram.com/p/Cpost000001/",
        published_at=datetime(2026, 11, 10, 8, tzinfo=timezone.utc),
        alt_texts=("A diagram of a cache",),
    )


SLIDES = [PostImage("image/jpeg", b"slide1"), PostImage("image/png", b"slide2")]


def reply(**overrides) -> dict:
    base = {
        "headline": "Five caching mistakes that slow APIs down",
        "article": "Caching **mistakes** add latency.\n\n\n  The post walks through five of them, slide by slide.",
        "tags": ["Caching", "#system design", "caching"],
    }
    base.update(overrides)
    return base


async def test_slides_and_caption_go_to_claude(monkeypatch):
    seen = {}

    async def fake_run(prompt, **kwargs):
        seen.update(kwargs, prompt=prompt)
        return ClaudeResult("", reply(), 0.03)

    monkeypatch.setattr(post_writer, "run_claude", fake_run)
    story = await write_post_story(
        "claude-code:sonnet",
        make_post(),
        "ByteByteGo",
        SLIDES + [PostImage("image/heic", b"x")],
    )
    assert story.headline == "Five caching mistakes that slow APIs down"
    assert story.text.split("\n\n") == [
        "Caching **mistakes** add latency.",
        "The post walks through five of them, slide by slide.",
    ]
    assert story.tags == ["caching", "system-design"]
    # Readable images in order; formats Claude can't read are left out.
    assert [(i.media_type, i.data) for i in seen["images"]] == [
        ("image/jpeg", b"slide1"),
        ("image/png", b"slide2"),
    ]
    payload = json.loads(seen["prompt"])
    assert payload["kind"] == "carousel" and payload["images_attached"] == 2
    assert payload["account"] == "ByteByteGo" and payload["username"] == "@bytebytego"
    assert payload["image_descriptions"] == ["A diagram of a cache"]
    assert seen["model"] == "claude-code:sonnet"
    assert seen["json_schema"]["properties"]["headline"]["maxLength"] == 80


def test_a_reel_says_only_its_caption_and_cover_are_available():
    payload = json.loads(render_request(make_post("VIDEO"), "ByteByteGo", 1))
    assert (
        payload["kind"] == "reel"
        and "Only its caption and cover frame" in payload["note"]
    )
    assert json.loads(render_request(make_post(caption=""), "", 0))["caption"] is None


async def test_bad_replies_and_bad_models_are_errors(monkeypatch):
    async def empty_article(prompt, **kwargs):
        return ClaudeResult("", reply(article="Too short."), 0.0)

    monkeypatch.setattr(post_writer, "run_claude", empty_article)
    with pytest.raises(ClaudeCodeError, match="no article"):
        await write_post_story("claude-code:sonnet", make_post(), "", SLIDES)
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_post_story("google:gemini-3.8-flash", make_post(), "", SLIDES)
    with pytest.raises(ClaudeCodeError, match="no caption and no readable images"):
        await write_post_story("claude-code:sonnet", make_post(caption=""), "", [])
