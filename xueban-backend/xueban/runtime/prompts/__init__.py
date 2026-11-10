"""xueban.runtime.prompts — single source for system_prompt assembly.

Centralizes prompt composition so versioning + A/B testing become tractable.
See docs/backend-capability-map-and-upgrade-plan.md §3.1 (T1).
"""
from __future__ import annotations

from xueban.runtime.prompts.builder import (
    BuildContext,
    PromptSection,
    SystemPromptBuilder,
)

__all__ = [
    "BuildContext",
    "PromptSection",
    "SystemPromptBuilder",
]
