"""MCP transport configuration — parses server URI strings (Story 2.4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class McpTransportConfig:
    """Parsed representation of an MCP server URI.

    Supported URI schemes:
      stdio://hermes mcp serve   → kind="stdio", command=["hermes","mcp","serve"]
      http://localhost:8080      → kind="http",  url="http://localhost:8080"
    """

    kind: str
    command: List[str] = field(default_factory=list)
    url: str = ""

    @classmethod
    def parse(cls, server: str) -> "McpTransportConfig":
        server = server.strip()
        if server.startswith("stdio://"):
            raw = server[len("stdio://"):]
            parts = raw.split()
            if not parts:
                raise ValueError(f"stdio:// URI has no command: {server!r}")
            return cls(kind="stdio", command=parts)
        if server.startswith("http://") or server.startswith("https://"):
            return cls(kind="http", url=server)
        raise ValueError(
            f"Unknown MCP server URI scheme: {server!r}; expected 'stdio://' or 'http[s]://'"
        )
