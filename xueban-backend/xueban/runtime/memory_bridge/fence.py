"""Fence construction + UUID per-turn + integrity validation (Story 2.9 NFR7).

Rules (NFR7):
  - Every drink() result MUST carry fence="xueban-context" + fence_uuid (UUID4).
  - fence_uuid is per-turn: a single UUID is generated at the start of drink()
    and embedded in the returned DrinkResult. It must NOT be reused across turns.
  - Integrity check: validate_fence() verifies the fragment has not been tampered with.
"""

from __future__ import annotations

from typing import Any, Dict
from uuid import UUID, uuid4

from xueban.runtime.memory_bridge.types import DrinkResult

FENCE_NAME = "xueban-context"


def new_fence_uuid() -> str:
    """Generate a fresh UUID4 string for a single drink turn."""
    return str(uuid4())


def build_drink_result(text: str, fence_uuid: str | None = None) -> DrinkResult:
    """Wrap raw drink text into a fence-enclosed DrinkResult.

    Args:
        text: Raw context text from river.drink().
        fence_uuid: Pre-generated UUID for this turn. If None a fresh one is
            generated (callers that need the UUID for logging should generate
            it first via new_fence_uuid() and pass it here).

    Returns:
        DrinkResult with fence metadata set.
    """
    if fence_uuid is None:
        fence_uuid = new_fence_uuid()
    return DrinkResult(
        fence_uuid=fence_uuid,
        text=text,
        empty=not bool(text),
    )


def build_empty_drink_result(
    fence_uuid: str | None = None,
    warning: str | None = None,
) -> DrinkResult:
    """Return a fenced but empty DrinkResult (isolated mode or circuit-break)."""
    if fence_uuid is None:
        fence_uuid = new_fence_uuid()
    return DrinkResult(
        fence_uuid=fence_uuid,
        text="",
        empty=True,
        warning=warning,
    )


def validate_fence(fragment: Dict[str, Any]) -> bool:
    """Validate a DrinkResult ACP wire fragment for fence integrity.

    Checks:
    1. type == "context"
    2. fence == "xueban-context"
    3. fence_uuid is a valid UUID4 string

    Returns:
        True if the fragment passes all checks, False otherwise.
    """
    if fragment.get("type") != "context":
        return False
    if fragment.get("fence") != FENCE_NAME:
        return False
    uuid_str = fragment.get("fence_uuid", "")
    if not isinstance(uuid_str, str):
        return False
    try:
        UUID(uuid_str, version=4)
    except (ValueError, AttributeError):
        return False
    return True
