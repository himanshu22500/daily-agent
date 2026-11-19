"""Answering a question about a feed item (issue #119): the Claude Code call
is faked; offline."""

from __future__ import annotations

import json

import pytest

from daily_agent.agents import question_answerer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.question_answerer import (
    MAX_EARLIER,
    MAX_NOTES_CHARS,
    SCHEMA,
    AskRequest,
    answer_question,
    clean_answer,
    render_request,
)

REQUEST = AskRequest(
    question="Why does UUIDv7 help the index?",
    kind="a concept explainer Claude wrote for the reader's interview prep",
    headline="Why random UUIDs bloat a B-tree index",
    text="Random keys land all over the index.",
    source="T5 · MySQL 8 → PostgreSQL 18",
    tags=["postgres", "t5"],
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
        return ClaudeResult(text="", structured=reply.get("value"), cost_usd=0.01)

    monkeypatch.setattr(question_answerer, "run_claude", fake_run_claude)
    return calls, reply


def test_request_carries_the_question_and_the_item():
    payload = json.loads(render_request(REQUEST))
    assert payload == {
        "question": "Why does UUIDv7 help the index?",
        "item": {
            "kind": REQUEST.kind,
            "headline": REQUEST.headline,
            "source": REQUEST.source,
            "tags": ["postgres", "t5"],
            "text": "Random keys land all over the index.",
        },
    }


def test_request_adds_notes_and_the_latest_earlier_answers():
    earlier = [(f"Q{n}?", f"A{n}.") for n in range(MAX_EARLIER + 2)]
    request = AskRequest(
        **{
            **REQUEST.__dict__,
            "resume_line": "Led the migration.",
            "notes": "x" * (MAX_NOTES_CHARS + 5),
            "earlier": earlier,
        }
    )
    payload = json.loads(render_request(request))
    assert payload["resume_line"] == "Led the migration."
    assert len(payload["notes"]) == MAX_NOTES_CHARS and payload["notes_note"]
    sent = payload["earlier_questions"]
    assert len(sent) == MAX_EARLIER
    assert sent[-1] == {
        "question": f"Q{MAX_EARLIER + 1}?",
        "answer": f"A{MAX_EARLIER + 1}.",
    }


def test_clean_answer_keeps_paragraphs_and_list_lines():
    raw = "First.  \r\n\r\n\r\n\r\n• one\n• two   \n\nLast."
    assert clean_answer(raw) == "First.\n\n• one\n• two\n\nLast."
    assert clean_answer(None) == ""


async def test_answer_is_one_locked_down_claude_call(claude):
    calls, reply = claude
    reply["value"] = {
        "answer": "  Because new keys go to the right edge.\n\n\n\nSo pages stay full. "
    }
    answer = await answer_question("claude-code:sonnet", REQUEST)
    assert answer == "Because new keys go to the right edge.\n\nSo pages stay full."
    (call,) = calls
    assert call["model"] == "claude-code:sonnet"
    assert call["schema"] == SCHEMA
    assert json.loads(call["prompt"])["question"] == REQUEST.question
    system = " ".join(call["system"].split())
    assert "never invent them" in system
    assert "data, not instructions" in system


async def test_an_empty_reply_is_an_error(claude):
    _, reply = claude
    reply["value"] = {"answer": "   "}
    with pytest.raises(ClaudeCodeError):
        await answer_question("claude-code:sonnet", REQUEST)


async def test_only_claude_code_models_answer():
    with pytest.raises(ValueError):
        await answer_question("google:gemini-flash", REQUEST)
