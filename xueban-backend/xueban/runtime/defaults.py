"""Default configuration constants for Quick Agent Create — Story 12.1."""
from __future__ import annotations

from typing import List

# Default MCP Server tool IDs automatically assigned to every new Quick-Hire agent.
DEFAULT_MCP_SERVERS: List[str] = [
    "xueban-shell",
    "xueban-fs",
    "xueban-web",
]

DEFAULT_LLM_PROVIDER = "anthropic"
DEFAULT_LLM_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_EXECUTOR_KIND = "api"
