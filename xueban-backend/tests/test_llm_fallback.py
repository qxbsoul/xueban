"""Tests for FallbackProvider (Story 3.5 T5)."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from xueban.llm.base import LLMConfig, LLMProvider, LLMResponse, ProviderType
from xueban.llm.fallback import AllProvidersFailed, FallbackProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(timeout: int = 30) -> LLMConfig:
    return LLMConfig(model="test-model", timeout=timeout)


def _make_provider(
    provider_type: ProviderType,
    *,
    raises: BaseException | None = None,
    response_text: str = "ok",
) -> LLMProvider:
    provider = MagicMock(spec=LLMProvider)
    provider.config = _make_config()
    provider.provider_type = provider_type

    if raises is not None:
        provider.generate = AsyncMock(side_effect=raises)
        provider.chat = AsyncMock(side_effect=raises)
    else:
        resp = LLMResponse(
            content=response_text,
            model="test-model",
            provider=provider_type,
            metadata={},
        )
        provider.generate = AsyncMock(return_value=resp)
        provider.chat = AsyncMock(return_value=resp)

    return provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_primary_success_no_fallback():
    """Primary succeeds — fallback never called, response tagged resolved_provider."""
    primary = _make_provider(ProviderType.CLAUDE)
    fallback = _make_provider(ProviderType.OPENAI)
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0)

    resp = await fp.generate("hello")
    assert resp.content == "ok"
    assert resp.metadata["resolved_provider"] == "claude"
    assert resp.metadata["fallback_used"] is False
    fallback.generate.assert_not_called()


@pytest.mark.asyncio
async def test_primary_timeout_switches_to_openai():
    """Claude times out (asyncio.TimeoutError) → switches to OpenAI."""
    primary = _make_provider(ProviderType.CLAUDE, raises=asyncio.TimeoutError())
    fallback = _make_provider(ProviderType.OPENAI)
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0)

    resp = await fp.generate("hello")
    assert resp.metadata["resolved_provider"] == "openai"
    assert resp.metadata["fallback_used"] is True


@pytest.mark.asyncio
async def test_all_providers_fail_raises():
    """When all providers fail, AllProvidersFailed is raised."""
    primary = _make_provider(ProviderType.CLAUDE, raises=asyncio.TimeoutError())
    fallback = _make_provider(ProviderType.OPENAI, raises=asyncio.TimeoutError())
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0)

    with pytest.raises(AllProvidersFailed) as exc_info:
        await fp.generate("hello")

    assert exc_info.value.code == "ALL_PROVIDERS_FAILED"
    assert len(exc_info.value.details["errors"]) == 2


@pytest.mark.asyncio
async def test_fallback_event_emitted():
    """provider.fallback event is emitted when switching providers."""
    emitted: List[Dict[str, Any]] = []

    async def collect(event: Dict[str, Any]) -> None:
        emitted.append(event)

    primary = _make_provider(ProviderType.CLAUDE, raises=asyncio.TimeoutError())
    fallback = _make_provider(ProviderType.OPENAI)
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0, event_emitter=collect)

    await fp.generate("hello")

    assert len(emitted) == 1
    evt = emitted[0]
    assert evt["type"] == "provider.fallback"
    assert evt["from"] == "claude"
    assert evt["to"] == "openai"


@pytest.mark.asyncio
async def test_rate_limit_status_code_triggers_fallback():
    """HTTP 429 (rate limit) on primary triggers fallback."""

    class RateLimitError(Exception):
        status_code = 429

    primary = _make_provider(ProviderType.CLAUDE, raises=RateLimitError("rate limited"))
    fallback = _make_provider(ProviderType.GEMINI)
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0)

    resp = await fp.generate("hello")
    assert resp.metadata["resolved_provider"] == "gemini"
    assert resp.metadata["fallback_used"] is True


@pytest.mark.asyncio
async def test_non_retriable_error_propagates():
    """A non-retriable error (e.g. ValueError) is NOT swallowed — re-raised immediately."""
    primary = _make_provider(ProviderType.CLAUDE, raises=ValueError("bad prompt"))
    fallback = _make_provider(ProviderType.OPENAI)
    fp = FallbackProvider(primary, [fallback], timeout_seconds=5.0)

    with pytest.raises(ValueError, match="bad prompt"):
        await fp.generate("hello")

    fallback.generate.assert_not_called()


@pytest.mark.asyncio
async def test_chat_fallback_chain():
    """chat() also respects fallback chain."""
    primary = _make_provider(ProviderType.CLAUDE, raises=asyncio.TimeoutError())
    fb1 = _make_provider(ProviderType.OPENAI, raises=asyncio.TimeoutError())
    fb2 = _make_provider(ProviderType.GEMINI)
    fp = FallbackProvider(primary, [fb1, fb2], timeout_seconds=5.0)

    resp = await fp.chat([{"role": "user", "content": "hi"}])
    assert resp.metadata["resolved_provider"] == "gemini"
    assert resp.metadata["fallback_used"] is True
