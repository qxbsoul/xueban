"""Agent Pack Manifest contracts — Story 12.5 AC1.

Defines the Pydantic models that back `agent-manifest.yaml` files,
analogous to VSCode's `package.json` for extensions.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ManifestCapabilities(BaseModel):
    tools: List[str] = Field(
        default_factory=lambda: ["xueban-shell", "xueban-fs", "xueban-web"]
    )
    llm_provider: str = "claude"
    streaming: bool = False
    approval_required: bool = False
    session_resume: bool = False
    tool_calls: bool = False


class ManifestSignature(BaseModel):
    algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    value: str


class AgentPackManifest(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    version: str  # semver string, e.g. "1.2.0"
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    author: str = Field("", max_length=120)
    soul: str = Field(..., min_length=1, max_length=4000)
    kind: Literal["api", "cli", "mcp", "acp"] = "api"
    capabilities: ManifestCapabilities = Field(default_factory=ManifestCapabilities)
    install_cmd: Optional[str] = None
    signature: Optional[ManifestSignature] = None  # None → unverified pack
