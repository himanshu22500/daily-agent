"""System design card writer (issue #134): the Claude Code call is faked; offline."""

from __future__ import annotations

import json

import pytest

from daily_agent.agents import design_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.design_writer import (
    BLOCK_SCHEMA,
    BLOCK_STEPS,
    CASE_SCHEMA,
    CASE_STEPS,
    TRY_FIRST,
    clean_diagram,
    render_request,
    write_card,
)
from daily_agent.feed.design_topics import BY_SLUG
from daily_agent.feed.prep_tracks import split_steps

CASE_REPLY = {
    "question": "Design cart and checkout for a 10-minute grocery app.",
    "must_cover": "Atomic stock holds, idempotent checkout, hold expiry.",
    "functional": ["- Users add items to a cart", "Users check out", "1. Users pay"],
    "non_functional": ["Never oversell", "Checkout p99 < 500 ms"],
    "scale": "Assume 1.5M orders/day: ~20/s avg, ~150/s at peak.",
    "entities": [{"name": "Order", "fields": "id, user_id,\n store_id, status"}],
    "api": [{"endpoint": "`POST /v1/checkouts`", "summary": "Idempotency-Key header"}],
    "components": [{"name": "**Checkout** service", "role": "Holds stock"}],
    "flows": ["Checkout: app -> Checkout service -> Postgres"],
    "diagram": "```\nApp -> API -> Postgres\n```",
    "deep_dives": [
        {
            "probe": "How do you stop overselling?",
            "answer": "A conditional update.\n### Not a step\nRows lock briefly.",
        },
        {"probe": "Duplicate callbacks?", "answer": "Dedupe on the payment id."},
    ],
    "senior_extras": ["Hot-SKU bucket rows"],
    "recap": "Cart in Redis, holds in one Postgres transaction.",
}

BLOCK_REPLY = {
    "question": "When would you reach for consistent hashing?",
    "answer": "When the set of nodes changes: only about 1/N of keys move.",
    "how_it_works": ["Hash nodes onto a ring", "Virtual nodes even the load"],
    "diagram": "x" * 60,  # too wide for a phone: dropped
    "trade_offs": ["Hot keys still land on one node"],
    "in_interviews": [{"design": "Design a distributed cache", "use": "A ring"}],
    "follow_up": {"question": "A node crashes?", "answer": "Its arcs pass on."},
}


@pytest.fixture
def claude(monkeypatch):
    calls: list[dict] = []
    reply: dict = {}

    async def fake_run_claude(prompt, *, model, system_prompt, json_schema=None, **kw):
        calls.append(
            {"prompt": prompt, "model": model, "system": system_prompt, **kw}
            | {"schema": json_schema}
        )
        return ClaudeResult(text="", structured=reply.get("value"), cost_usd=0.1)

    monkeypatch.setattr(design_writer, "run_claude", fake_run_claude)
    return calls, reply


def test_request_names_the_topic_and_blinkit_context():
    payload = json.loads(render_request(BY_SLUG["cart-checkout"]))
    assert payload["kind"] == "case study" and payload["topic"].startswith("Design")
    assert "idempotency" in payload["building_blocks"]
    assert "dark store" in payload["company_context"]
    payload = json.loads(render_request(BY_SLUG["caching"]))
    assert payload["kind"] == "building block" and "company_context" not in payload


async def test_case_study_card_has_the_framework_steps(claude):
    calls, reply = claude
    reply["value"] = CASE_REPLY
    card = await write_card("claude-code:opus", BY_SLUG["cart-checkout"])
    assert card.title == "Design cart and checkout with stock holds"
    prompt, steps = split_steps(card.text)
    assert prompt.startswith("Design cart and checkout for a 10-minute")
    assert "**A mid-level answer covers:** Atomic" in prompt
    assert prompt.endswith(TRY_FIRST)
    assert [s.title for s in steps] == list(CASE_STEPS)
    body = {s.title: s.body for s in steps}
    # Bullets are the card's own; model text can't add a step or bold-in-bold.
    assert (
        "- Users add items to a cart\n- Users check out\n- Users pay"
        in (body["Requirements"])
    )
    assert body["Core entities"] == "- **Order:** id, user_id, store_id, status"
    assert body["API"] == "- `POST /v1/checkouts`: Idempotency-Key header"
    assert "- **Checkout service:** Holds stock" in body["High-level design"]
    assert "```\nApp -> API -> Postgres\n```" in body["High-level design"]
    assert "1. Checkout: app" in body["High-level design"]
    assert body["Deep dives"].startswith("**1. How do you stop overselling?**")
    assert "Not a step" in body["Deep dives"] and "###" not in body["Deep dives"]
    assert body["Senior extras (optional)"] == "- Hot-SKU bucket rows"
    [call] = calls
    assert call["schema"] is CASE_SCHEMA and call["model"] == "claude-code:opus"
    assert "mid level" in " ".join(call["system"].split())
    assert call["timeout"] == design_writer.TIMEOUT_SECONDS


async def test_building_block_card(claude):
    calls, reply = claude
    reply["value"] = BLOCK_REPLY
    card = await write_card("claude-code:opus", BY_SLUG["consistent-hashing"])
    assert card.title == "Consistent hashing"
    prompt, steps = split_steps(card.text)
    assert prompt.startswith("When would you reach for consistent hashing?")
    assert [s.title for s in steps] == list(BLOCK_STEPS)
    body = {s.title: s.body for s in steps}
    assert "```" not in body["How it works"]  # the too-wide diagram was dropped
    assert (
        "**Likely follow-up: A node crashes?**"
        in (body["Where it comes up in interviews"])
    )
    assert calls[0]["schema"] is BLOCK_SCHEMA


async def test_bad_replies_and_other_models_are_refused(claude):
    _, reply = claude
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_card("google:gemini-3.8-flash", BY_SLUG["caching"])
    reply["value"] = {**CASE_REPLY, "deep_dives": []}
    with pytest.raises(ClaudeCodeError, match="deep_dives"):
        await write_card("claude-code:opus", BY_SLUG["url-shortener"])
    reply["value"] = None
    with pytest.raises(ClaudeCodeError):
        await write_card("claude-code:opus", BY_SLUG["caching"])


def test_schemas_carry_the_limits():
    props = CASE_SCHEMA["properties"]
    assert props["deep_dives"]["minItems"] == 2 and props["deep_dives"]["maxItems"] == 3
    assert all(
        "maxLength" in p or "maxItems" in p
        for schema in (CASE_SCHEMA, BLOCK_SCHEMA)
        for p in schema["properties"].values()
        if p.get("type") != "object"
    )


def test_diagrams_must_fit_a_phone():
    assert clean_diagram("```\n\nA -> B\n```") == "A -> B"
    assert clean_diagram("A\n" * 20) == ""
    assert clean_diagram(None) == ""
