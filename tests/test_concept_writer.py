"""Concept explainer writer (issue #108): the Claude Code call is faked; offline."""

from __future__ import annotations

import json

import pytest

from daily_agent.agents import concept_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.concept_writer import (
    MAX_NOTES_CHARS,
    SCHEMA,
    ConceptRequest,
    render_request,
    write_explainer,
)

REQUEST = ConceptRequest(
    concept="UUIDv4 vs UUIDv7: B-tree locality, page splits",
    point_id="T5",
    point_name="MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7",
    group="TranZact",
    resume_line="Led the production MySQL 8 → PostgreSQL 18 migration.",
    notes="## 3. Numbers to defend\n- random UUIDv4 +134.7% storage",
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

    monkeypatch.setattr(concept_writer, "run_claude", fake_run_claude)
    return calls, reply


def test_request_carries_the_concept_and_the_notes():
    payload = json.loads(render_request(REQUEST))
    assert payload == {
        "concept": REQUEST.concept,
        "resume_point": "T5: MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7",
        "job": "TranZact",
        "resume_line": REQUEST.resume_line,
        "notes": REQUEST.notes,
    }
    long = ConceptRequest(**{**REQUEST.__dict__, "notes": "x" * (MAX_NOTES_CHARS + 5)})
    payload = json.loads(render_request(long))
    assert len(payload["notes"]) == MAX_NOTES_CHARS and payload["notes_note"]


async def test_writes_a_clean_explainer(claude):
    calls, reply = claude
    reply["value"] = {
        "headline": '"Why random UUIDs bloat a B-tree index."',
        "article": ("A random key lands on a random leaf page.   " * 12)
        + "\n\n\n\nIn your benchmark random UUIDs cost **+134.7%** storage.",
        "tags": ["#B-Tree Indexes", "postgres"],
    }
    story = await write_explainer("claude-code:sonnet", REQUEST)
    assert story.headline == "Why random UUIDs bloat a B-tree index"
    assert story.text.endswith("random UUIDs cost **+134.7%** storage.")
    assert "\n\n" in story.text and "   " not in story.text
    assert story.tags == ["b-tree-indexes", "postgres"]
    [call] = calls
    assert call["schema"] is SCHEMA and call["model"] == "claude-code:sonnet"
    system = " ".join(call["system"].split())
    assert "never invent numbers" in system and "⬜ OPEN" in system
    assert json.loads(call["prompt"])["concept"] == REQUEST.concept


async def test_rejects_other_models_and_thin_replies(claude):
    _, reply = claude
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_explainer("google:gemini-3.8-flash", REQUEST)
    reply["value"] = {
        "headline": "A fine headline",
        "article": "Too short.",
        "tags": [],
    }
    with pytest.raises(ClaudeCodeError, match="article"):
        await write_explainer("claude-code:sonnet", REQUEST)
    reply["value"] = {"headline": "", "article": "x " * 300, "tags": []}
    with pytest.raises(ClaudeCodeError, match="headline"):
        await write_explainer("claude-code:sonnet", REQUEST)
