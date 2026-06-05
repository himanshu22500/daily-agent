"""Resolve a config model string into a Pydantic AI model.

Most strings (``provider:model``) are handed straight to Pydantic AI. The one
special case is OpenAI's **codex / gpt-5 reasoning models**, which use the
Responses API rather than Chat Completions — select those with the
``openai-responses:`` prefix, e.g. ``openai-responses:gpt-5-codex``.
"""

from __future__ import annotations

from typing import Union

from pydantic_ai.models import Model


def build_model(model: str) -> Union[Model, str]:
    if model.startswith("openai-responses:"):
        from pydantic_ai.models.openai import OpenAIResponsesModel

        return OpenAIResponsesModel(model.split(":", 1)[1])
    return model


def cache_settings(model: str):
    """Prompt-caching model settings for Anthropic models; None otherwise.

    Caches the static prefix (system instructions + tool definitions) and the
    conversation messages, so repeated `ask`/`chat` turns reuse the cached
    prefix — cheaper and lower-latency. No-op for non-Anthropic providers.
    """
    if isinstance(model, str) and model.startswith("anthropic:"):
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
            anthropic_cache_messages=True,
        )
    return None
