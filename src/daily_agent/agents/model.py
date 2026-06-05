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
