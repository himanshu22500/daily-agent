"""Model settings and retry behavior (offline)."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from daily_agent.agents.model import cache_settings, run_with_backoff


class _FlakyAgent:
    def __init__(self, failures: list[Exception], result: object = "ok") -> None:
        self.failures = failures
        self.result = result
        self.calls = 0

    async def run(self, *args, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


def test_cache_settings_for_anthropic():
    s = cache_settings("anthropic:claude-sonnet-4-6")
    assert s is not None
    assert s.get("anthropic_cache_instructions") is True
    assert s.get("anthropic_cache_tool_definitions") is True


def test_cache_settings_none_for_other_providers():
    assert cache_settings("openai:gpt-4o") is None
    assert cache_settings("") is None


async def test_run_with_backoff_recovers_from_transient_http_errors():
    agent = _FlakyAgent(
        [
            ModelHTTPError(503, "gemini", {"error": "busy"}),
            ModelHTTPError(429, "gemini", {"error": "rate limited"}),
        ]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await run_with_backoff(agent, "prompt", sleep=fake_sleep)

    assert result == "ok"
    assert agent.calls == 3
    assert delays == [2.0, 4.0]


async def test_run_with_backoff_retries_transport_errors():
    agent = _FlakyAgent([ModelAPIError("gemini", "connection reset")])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    assert await run_with_backoff(agent, sleep=fake_sleep) == "ok"
    assert delays == [2.0]


async def test_run_with_backoff_does_not_retry_permanent_http_error():
    error = ModelHTTPError(401, "gemini", {"error": "bad key"})
    agent = _FlakyAgent([error])

    with pytest.raises(ModelHTTPError) as raised:
        await run_with_backoff(agent)

    assert raised.value is error
    assert agent.calls == 1


async def test_run_with_backoff_stops_after_attempt_limit():
    errors = [ModelHTTPError(503, "gemini") for _ in range(3)]
    agent = _FlakyAgent(errors)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(ModelHTTPError):
        await run_with_backoff(agent, attempts=3, sleep=fake_sleep)

    assert agent.calls == 3
    assert delays == [2.0, 4.0]
