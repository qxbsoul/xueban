"""ACP (Agent Client Protocol) module for Xueban.

Story 2.3: ACP Client — Xueban connects outbound to external agents.
Story 2.10: ACP Capability Handshake — AgentCapabilityManifest + AgentRegistry.
Story 2.11: ACP Server — external agents connect inbound to Xueban.
"""

from xueban.runtime.acp.client import AcpClient
from xueban.runtime.acp.messages import (
    AcpMessage,
    InitializeRequest,
    InitializeResponse,
    SessionNewRequest,
    SessionPromptRequest,
    SessionUpdateNotification,
    SessionRequestPermissionNotification,
    SessionPermissionResultRequest,
)
from xueban.runtime.acp.transport import AcpTransport, AcpSessionTerminated
from xueban.runtime.acp.registry import (
    AgentCapabilityManifest,
    AgentRegistryEntry,
    AgentRegistry,
    ToolCapability,
    MemoryCapability,
    TaskRoutingLog,
    get_registry,
)
from xueban.runtime.acp.server import (
    AgentSession,
    ACPConnectionManager,
    get_manager,
)

__all__ = [
    # Client (Story 2.3)
    "AcpClient",
    "AcpMessage",
    "AcpTransport",
    "AcpSessionTerminated",
    "InitializeRequest",
    "InitializeResponse",
    "SessionNewRequest",
    "SessionPromptRequest",
    "SessionUpdateNotification",
    "SessionRequestPermissionNotification",
    "SessionPermissionResultRequest",
    # Registry (Story 2.10)
    "AgentCapabilityManifest",
    "AgentRegistryEntry",
    "AgentRegistry",
    "ToolCapability",
    "MemoryCapability",
    "TaskRoutingLog",
    "get_registry",
    # Server (Story 2.11)
    "AgentSession",
    "ACPConnectionManager",
    "get_manager",
]
