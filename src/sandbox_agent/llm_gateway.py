"""Opt-in LLM gateway routing.

When both ``LLM_GATEWAY_BASE_URL`` and ``LLM_GATEWAY_API_KEY`` are set,
Anthropic SDK and pydantic-ai OpenAI clients are routed through a LiteLLM
gateway. When either is unset, every client behaves exactly as before —
``anthropic_client_kwargs`` returns an empty dict and ``maybe_gateway_model``
returns the model string unchanged.

The Moonshot/Kimi provider path is never affected: ``maybe_gateway_model`` only
transforms models with an ``openai:`` prefix.

Env vars
--------
LLM_GATEWAY_BASE_URL
    Gateway base URL in OpenAI shape, e.g. ``http://localhost:18400/v1``.
    Passed as-is to the pydantic-ai OpenAIProvider; the trailing ``/v1`` is
    stripped for Anthropic SDK clients (the SDK appends ``/v1/messages``).
LLM_GATEWAY_API_KEY
    API key sent to the gateway for both providers.
"""

from __future__ import annotations

import os
from typing import Any

_BASE_URL = os.environ.get("LLM_GATEWAY_BASE_URL")
_API_KEY = os.environ.get("LLM_GATEWAY_API_KEY")
_ENABLED = bool(_BASE_URL and _API_KEY)


def anthropic_client_kwargs() -> dict[str, Any]:
    """Kwargs to spread into ``Anthropic()`` / ``AsyncAnthropic()``.

    Returns an empty dict when the gateway is disabled (zero behavior change).
    The Anthropic SDK appends ``/v1/messages`` itself, so a trailing ``/v1`` is
    stripped from the base URL.
    """
    if not _ENABLED:
        return {}
    base_url = _BASE_URL or ""
    if base_url.endswith("/v1"):
        base_url = base_url.removesuffix("/v1")
    return {"base_url": base_url, "api_key": _API_KEY}


def maybe_gateway_model(model: str):
    """Return a gateway-routed ``OpenAIModel``, or the model string unchanged.

    Only transforms ``openai:``-prefixed model ids (e.g. ``openai:gpt-5.4``).
    Moonshot/Kimi and any other provider prefixes pass through untouched, so
    the Kimi path is never affected by the gateway.
    """
    if not _ENABLED or not model.startswith("openai:"):
        return model
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        model.removeprefix("openai:"),
        provider=OpenAIProvider(base_url=_BASE_URL, api_key=_API_KEY),
    )
