"""FallbackProvider — LLM-level provider fallback chain (Story 3.5, AR18, E1).

Wraps a primary LLMProvider and a fallback_chain list. On timeout, HTTP 5xx, or
rate-limit the next provider in the chain is tried in order. Emits
AgentEvent(type="provider.fallback") for each switch; callers pass an optional
event_queue (asyncio.Queue) to receive these events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence

from xueban.llm.base import LLMConfig, LLMProvider, LLMResponse, ProviderType
from xueban.runtime.errors import XuebanError

logger = logging.getLogger("xueban.llm.fallback")

# Errors that justify switching to the next provider
_RETRIABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)

# Status codes that trigger fallback (when using HTTP-based providers)
_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class AllProvidersFailed(XuebanError):
    """Raised when every provider in the chain fails."""

    code = "ALL_PROVIDERS_FAILED"

    def __init__(self, errors: List[str]) -> None:
        super().__init__(
            f"All providers failed after {len(errors)} attempt(s)",
            details={"errors": errors},
        )


class FallbackProvider(LLMProvider):
    """Decorator provider that tries primary, then fallback_chain in order.

    Args:
        primary: The primary LLMProvider to attempt first.
        fallback_chain: Ordered list of fallback providers.
        timeout_seconds: Per-provider call timeout (default 30s).
        event_emitter: Optional async callable(event_dict) for provider.fallback events.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback_chain: Sequence[LLMProvider],
        *,
        timeout_seconds: float = 30.0,
        event_emitter: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        # Use primary's config for the base class
        super().__init__(primary.config)
        self._primary = primary
        self._fallback_chain = list(fallback_chain)
        self._timeout = timeout_seconds
        self._event_emitter = event_emitter

    @property
    def provider_type(self) -> ProviderType:
        return self._primary.provider_type

    def _all_providers(self) -> List[LLMProvider]:
        return [self._primary, *self._fallback_chain]

    async def _emit_fallback(self, from_p: LLMProvider, to_p: LLMProvider, reason: str) -> None:
        if self._event_emitter is None:
            return
        event = {
            "type": "provider.fallback",
            "from": from_p.provider_type.value,
            "to": to_p.provider_type.value,
            "reason": reason,
        }
        try:
            result = self._event_emitter(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_emitter raised: %s", exc)

    async def _call_with_timeout(
        self,
        coro_factory: Callable[[], Any],
    ) -> Any:
        """Run coro with per-provider timeout."""
        return await asyncio.wait_for(coro_factory(), timeout=self._timeout)

    def _is_retriable(self, exc: BaseException) -> bool:
        if isinstance(exc, _RETRIABLE_EXCEPTIONS):
            return True
        # Check for HTTP status code on exception attributes
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if isinstance(status, int) and status in _RETRIABLE_STATUS_CODES:
            return True
        return False

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        errors: List[str] = []
        providers = self._all_providers()
        for i, provider in enumerate(providers):
            try:
                response = await self._call_with_timeout(lambda p=provider: p.generate(prompt, **kwargs))
                return _tag_response(response, provider, used_fallback=i > 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.provider_type.value}: {exc}")
                if not self._is_retriable(exc):
                    raise
                if i + 1 < len(providers):
                    await self._emit_fallback(providers[i], providers[i + 1], str(exc))
                    logger.info("FallbackProvider: %s → %s (%s)", providers[i].provider_type.value, providers[i + 1].provider_type.value, exc)
        raise AllProvidersFailed(errors)

    async def chat(self, messages: list, **kwargs) -> LLMResponse:
        errors: List[str] = []
        providers = self._all_providers()
        for i, provider in enumerate(providers):
            try:
                response = await self._call_with_timeout(lambda p=provider: p.chat(messages, **kwargs))
                return _tag_response(response, provider, used_fallback=i > 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.provider_type.value}: {exc}")
                if not self._is_retriable(exc):
                    raise
                if i + 1 < len(providers):
                    await self._emit_fallback(providers[i], providers[i + 1], str(exc))
                    logger.info("FallbackProvider: %s → %s (%s)", providers[i].provider_type.value, providers[i + 1].provider_type.value, exc)
        raise AllProvidersFailed(errors)

    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        # Stream doesn't use timeout wrapper — delegates to primary only; fallback on error
        errors: List[str] = []
        providers = self._all_providers()
        for i, provider in enumerate(providers):
            try:
                async for chunk in provider.stream(prompt, **kwargs):
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.provider_type.value}: {exc}")
                if not self._is_retriable(exc):
                    raise
                if i + 1 < len(providers):
                    await self._emit_fallback(providers[i], providers[i + 1], str(exc))
        raise AllProvidersFailed(errors)


def _tag_response(response: LLMResponse, provider: LLMProvider, *, used_fallback: bool) -> LLMResponse:
    """Attach resolved_provider + fallback_used to response metadata (FR18)."""
    response.metadata["resolved_provider"] = provider.provider_type.value
    response.metadata["fallback_used"] = used_fallback
    return response
