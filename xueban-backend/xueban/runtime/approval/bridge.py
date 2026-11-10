"""ACP approval bridge (extracted from xueban.runtime.acp.approval_bridge).

Bridges ACP session.requestPermission to Xueban's approval_gate mechanism.
The class is moved here verbatim; the original module re-exports it for
backward compatibility.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


class AcpApprovalBridge:
    """Bridges ACP permission requests to Xueban's approval_gate mechanism.

    When an ACP agent sends session.requestPermission, this bridge:
    1. Records the pending permission request
    2. Signals the Xueban approval system (via asyncio.Event)
    3. When the approval decision arrives, returns it to the ACP agent
    """

    def __init__(self) -> None:
        # permission_id → asyncio.Event
        self._pending_events: Dict[str, asyncio.Event] = {}
        # permission_id → granted (bool)
        self._decisions: Dict[str, bool] = {}
        # permission_id → ACP permission metadata
        self._requests: Dict[str, Dict[str, Any]] = {}

    def register_permission_request(
        self,
        permission_id: str,
        description: str,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> asyncio.Event:
        """Register a new permission request and return an event to await on."""
        event = asyncio.Event()
        self._pending_events[permission_id] = event
        self._requests[permission_id] = {
            "permissionId": permission_id,
            "sessionId": session_id,
            "description": description,
            **(metadata or {}),
        }
        return event

    def resolve_permission(self, permission_id: str, granted: bool) -> bool:
        """Record the user's decision and signal the waiting coroutine.

        Returns True if there was a pending request, False otherwise.
        """
        event = self._pending_events.pop(permission_id, None)
        if event is None:
            return False
        self._decisions[permission_id] = granted
        event.set()
        return True

    def get_decision(self, permission_id: str) -> Optional[bool]:
        """Return the stored decision, or None if not yet decided."""
        return self._decisions.get(permission_id)

    def get_request(self, permission_id: str) -> Optional[Dict[str, Any]]:
        return self._requests.get(permission_id)

    def list_pending(self) -> list:
        return list(self._pending_events.keys())
