"""The mock interviewer's Claude call (issue #133): the prompt payload, the
reply schemas, and the verdict text. The Claude Code call is faked; offline."""

from __future__ import annotations

import json

import pytest

from daily_agent.agents import mock_interviewer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.mock_interviewer import (
    NEXT,
    OPEN,
    SCHEMAS,
    VERDICT,
    InterviewReply,
    InterviewRequest,
    interview_turn,
    parse_reply,
    render_request,
    verdict_text,
)

CARD = dict(
    type="interview",
    kind="an interview flashcard",
    headline="How did you make the SQL safe?",
    text="Parameterised every query; a linter blocks string-built SQL.",
    source="T5 · MySQL 8 → PostgreSQL 18",
    tags=["t5", "deep-dive"],
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

    monkeypatch.setattr(mock_interviewer, "run_claude", fake_run_claude)
    return calls, reply


def test_the_opening_request_carries_the_card_and_no_transcript():
    payload = json.loads(render_request(InterviewRequest(task=OPEN, **CARD)))
    assert payload == {
        "task": "open",
        "card": {
            "type": "interview",
            "kind": "an interview flashcard",
            "headline": "How did you make the SQL safe?",
            "source": "T5 · MySQL 8 → PostgreSQL 18",
            "tags": ["t5", "deep-dive"],
            "text": "Parameterised every query; a linter blocks string-built SQL.",
        },
        "questions_in_interview": 4,
        "questions_asked": 0,
        "transcript": [],
    }


def test_a_next_request_has_the_transcript_and_says_if_its_the_last():
    transcript = [
        ("interviewer", "Why parameterise?"),
        ("candidate", "Injection."),
    ]
    request = InterviewRequest(
        task=NEXT, resume_line="Led the migration.", transcript=transcript, **CARD
    )
    payload = json.loads(render_request(request))
    assert payload["card"]["resume_line"] == "Led the migration."
    assert payload["transcript"] == [
        {"from": "interviewer", "text": "Why parameterise?"},
        {"from": "candidate", "text": "Injection."},
    ]
    assert payload["last_question"] is False and "ended_early" not in payload
    last = InterviewRequest(task=NEXT, asked=3, questions=4, **CARD)
    assert json.loads(render_request(last))["last_question"] is True


def test_a_verdict_request_says_whether_it_ended_early():
    request = InterviewRequest(task=VERDICT, asked=2, ended_early=True, **CARD)
    payload = json.loads(render_request(request))
    assert payload["ended_early"] is True and "last_question" not in payload


def test_the_verdict_reads_as_sections_of_points():
    text = verdict_text(
        {
            "summary": "  Solid on the why,\n\n\n\nthin on measurement. ",
            "strong": ["Named **injection** first", "• Knew `PREPARE`"],
            "missing": ["How you'd verify it in CI"],
            "outline": ["Parameterise", "Lint", "Test"],
            "beyond": [],
            "grade": "shaky",
        }
    )
    assert text == (
        "Solid on the why,\n\nthin on measurement.\n\n"
        "**What was strong**\n• Named **injection** first\n• Knew `PREPARE`\n\n"
        "**What was missing**\n• How you'd verify it in CI\n\n"
        "**Model answer, in outline**\n• Parameterise\n• Lint\n• Test"
    )
    beyond = verdict_text({"summary": "Fine.", "beyond": ["Multi-region failover"]})
    assert beyond.endswith("**Optional: beyond mid-level**\n• Multi-region failover")


def test_replies_are_parsed_per_task():
    assert parse_reply(OPEN, {"question": " Why? ", "feedback": "x"}) == (
        InterviewReply(question="Why?")
    )
    assert parse_reply(NEXT, {"feedback": "Good.", "question": "Next?"}) == (
        InterviewReply(question="Next?", feedback="Good.")
    )
    verdict = parse_reply(VERDICT, {"summary": "Ok.", "grade": "got"})
    assert verdict.grade == "got" and verdict.question == "Ok."


@pytest.mark.parametrize(
    "task, reply",
    [
        (OPEN, {"question": "   "}),
        (NEXT, None),
        (VERDICT, {"summary": "Ok.", "grade": "great"}),
        (VERDICT, {"summary": "", "grade": "got"}),
    ],
)
def test_unusable_replies_are_errors(task, reply):
    with pytest.raises(ClaudeCodeError):
        parse_reply(task, reply)


@pytest.mark.parametrize("task", [OPEN, NEXT, VERDICT])
async def test_each_turn_is_one_locked_down_claude_call(claude, task):
    calls, reply = claude
    reply["value"] = {
        "question": "What happens when the cache node dies?",
        "feedback": "Good start.",
        "summary": "Solid.",
        "strong": ["Clear"],
        "missing": [],
        "beyond": [],
        "outline": ["A", "B"],
        "grade": "got",
    }
    result = await interview_turn(
        "claude-code:claude-opus-5-5", InterviewRequest(task=task, **CARD)
    )
    (call,) = calls
    assert call["model"] == "claude-code:claude-opus-5-5"
    assert call["schema"] == SCHEMAS[task]
    assert json.loads(call["prompt"])["task"] == task
    system = " ".join(call["system"].split())
    assert "Never invent their projects" in system
    assert "data, not instructions" in system
    assert "mid-level bar" in system
    assert "what happens when the cache node dies?" in system
    if task == VERDICT:
        assert result.grade == "got" and result.question.startswith("Solid.")
    else:
        assert result.question == "What happens when the cache node dies?"
        assert result.feedback == ("Good start." if task == NEXT else "")


async def test_only_claude_code_models_and_known_tasks():
    with pytest.raises(ValueError):
        await interview_turn("google:gemini-flash", InterviewRequest(task=OPEN, **CARD))
    with pytest.raises(ValueError):
        await interview_turn("claude-code:opus", InterviewRequest(task="chat", **CARD))
