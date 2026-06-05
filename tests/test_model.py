"""Model resolution + prompt-cache settings + fast-model fallback (offline)."""

from __future__ import annotations

from daily_agent.agents.model import cache_settings
from daily_agent.config import Settings


def test_cache_settings_for_anthropic():
    s = cache_settings("anthropic:claude-sonnet-4-6")
    assert s is not None
    assert s.get("anthropic_cache_instructions") is True
    assert s.get("anthropic_cache_tool_definitions") is True


def test_cache_settings_none_for_other_providers():
    assert cache_settings("openai:gpt-4o") is None
    assert cache_settings("") is None


def test_bulk_model_falls_back_to_model():
    s = Settings(model="anthropic:claude-sonnet-4-6", fast_model="")
    assert s.bulk_model == "anthropic:claude-sonnet-4-6"


def test_bulk_model_prefers_fast_model():
    s = Settings(model="anthropic:claude-sonnet-4-6", fast_model="anthropic:claude-haiku-4-5")
    assert s.bulk_model == "anthropic:claude-haiku-4-5"
