"""Resolve a config model string into a Pydantic AI model.

Most strings (``provider:model``) are handed straight to Pydantic AI. The one
special case is OpenAI's **codex / gpt-5 reasoning models**, which use the
Responses API rather than Chat Completions — select those with the
``openai-responses:`` prefix, e.g. ``openai-responses:gpt-5-codex``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, Union

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model

_LOG = logging.getLogger(__name__)
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
_T = TypeVar("_T")


def build_model(model: str) -> Union[Model, str]:
    if model.startswith("openai-responses:"):
        from pydantic_ai.models.openai import OpenAIResponsesModel

        return OpenAIResponsesModel(model.split(":", 1)[1])
    return model


def cache_settings(model: str):
    """Prompt-caching model settings for Anthropic models; None otherwise.

    Caches static instructions and message prefixes. No-op for other providers.
    """
    if isinstance(model, str) and model.startswith("anthropic:"):
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
            anthropic_cache_messages=True,
        )
    return None


async def run_with_backoff(
    agent: Any,
    *args: Any,
    attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> _T:
    """Run an agent, retrying only transient provider failures.

    HTTP 429 and common transient 5xx responses are retried, as are provider
    transport failures represented by ``ModelAPIError``. Permanent HTTP errors
    such as bad credentials fail immediately. Delays are bounded so a scheduled
    run cannot wait forever on a degraded provider.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        try:
            return await agent.run(*args, **kwargs)
        except ModelHTTPError as exc:
            if exc.status_code not in _TRANSIENT_HTTP_STATUSES or attempt == attempts:
                raise
            error = exc
        except ModelAPIError as exc:
            if attempt == attempts:
                raise
            error = exc

        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
        _LOG.warning(
            "Transient model failure (%s); retrying attempt %d/%d in %.1fs",
            error,
            attempt + 1,
            attempts,
            delay,
        )
        await sleep(delay)

    raise AssertionError("retry loop exited unexpectedly")
