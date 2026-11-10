"""Permission tri-state — auto / prompt / block (opencode-inspired, adoption #3).

Every executor dispatch resolves ONE permission decision per action:

- ``auto``   proceed silently (default)
- ``prompt`` park on the SAME approval machinery approval_gate nodes use
             (``_approval_events`` / ``_approval_decisions``, keyed
             ``(run_id, node_id)``) — the /api/approvals endpoints and the
             group pending list see and resolve it
- ``block``  refuse before dispatch (PermissionBlocked)

Policy sources, narrowest wins:
    node.config.permissions > workflow.defaults.permissions

    permissions:
      default: auto
      timeout_seconds: 300
      rules:
        dispatch: prompt   # the agent call itself
        network:  block
        exec:     prompt

Every decision lands in the durable run event log (``permission.checked`` /
``permission.blocked``), so the audit trail answers "who allowed what".
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

PermissionMode = Literal["auto", "prompt", "block"]

# Known actions; open set — unlisted actions fall back to `default`.
KNOWN_PERMISSION_ACTIONS = ("dispatch", "tool_call", "write", "network", "exec")


class PermissionPolicy(BaseModel):
    default: PermissionMode = "auto"
    rules: Dict[str, PermissionMode] = Field(default_factory=dict)
    timeout_seconds: float = 300.0

    def resolve(self, action: str) -> PermissionMode:
        mode = self.rules.get(action)
        if mode in ("auto", "prompt", "block"):
            return mode  # type: ignore[return-value]
        return self.default


def parse_permission_policy(raw: Any) -> Optional[PermissionPolicy]:
    if not isinstance(raw, dict):
        return None
    try:
        return PermissionPolicy.model_validate(raw)
    except ValueError:
        return None


class PermissionBlocked(Exception):
    """A `block` rule refused the action before dispatch."""

    def __init__(self, node_id: str, action: str, mode: str) -> None:
        self.node_id = node_id
        self.action = action
        self.mode = mode
        super().__init__(
            f"permission denied ({mode}): action {action!r} on node {node_id!r}"
        )


async def enforce_permission(
    svc: Any,
    *,
    run_id: str,
    node_id: str,
    action: str,
    policy: PermissionPolicy,
    summary: str = "",
) -> Dict[str, Any]:
    """Resolve one action against the policy; prompt reuses the approval flow.

    Returns {"action", "mode", "decision"}; raises PermissionBlocked on block
    rules, prompt rejections and prompt timeouts.
    """
    mode = policy.resolve(action)

    if mode == "auto":
        svc._emit_run_event(
            run_id, "permission.checked", node_id=node_id,
            payload={"action": action, "mode": "auto", "decision": "allowed"},
        )
        return {"action": action, "mode": "auto", "decision": "allowed"}

    if mode == "block":
        svc._emit_run_event(
            run_id, "permission.blocked", node_id=node_id,
            payload={"action": action, "mode": "block"},
        )
        raise PermissionBlocked(node_id, action, "block")

    # prompt: park on the shared approval registry so /api/approvals and the
    # group pending list can approve/reject it exactly like a gate node.
    key = (run_id, node_id)
    event = svc._approval_events.get(key)
    if event is None or event.is_set():
        event = asyncio.Event()
        svc._approval_events[key] = event

    if svc._event_bus is not None:
        from xueban.runtime.events import APPROVAL_PENDING

        svc._event_bus.publish(
            run_id,
            {
                "type": APPROVAL_PENDING,
                "run_id": run_id,
                "gate_id": node_id,
                "permission": {"action": action, "summary": summary[:200]},
            },
        )
    svc._emit_run_event(
        run_id, "permission.checked", node_id=node_id,
        payload={"action": action, "mode": "prompt", "decision": "pending"},
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=float(policy.timeout_seconds))
    except asyncio.TimeoutError:
        svc._approval_events.pop(key, None)
        svc._emit_run_event(
            run_id, "permission.blocked", node_id=node_id,
            payload={"action": action, "mode": "prompt", "reason": "timeout"},
        )
        raise PermissionBlocked(node_id, action, "prompt-timeout")

    decision = svc._approval_decisions.get(key, {}).get("decision")
    svc._approval_events.pop(key, None)
    svc._approval_decisions.pop(key, None)
    if decision != "approve":
        svc._emit_run_event(
            run_id, "permission.blocked", node_id=node_id,
            payload={"action": action, "mode": "prompt", "decision": decision},
        )
        raise PermissionBlocked(node_id, action, f"prompt-{decision or 'unset'}")

    svc._emit_run_event(
        run_id, "permission.checked", node_id=node_id,
        payload={"action": action, "mode": "prompt", "decision": "approved"},
    )
    return {"action": action, "mode": "prompt", "decision": "approved"}


def resolve_effective_policy(
    node_config: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[PermissionPolicy]:
    """node.config.permissions wins over the workflow-level default stashed
    in state (``state["permissions_default"]``, seeded by RuntimeService._execute)."""
    node_policy = parse_permission_policy(node_config.get("permissions"))
    if node_policy is not None:
        return node_policy
    return parse_permission_policy(state.get("permissions_default"))


__all__ = [
    "KNOWN_PERMISSION_ACTIONS",
    "PermissionBlocked",
    "PermissionMode",
    "PermissionPolicy",
    "enforce_permission",
    "parse_permission_policy",
    "resolve_effective_policy",
]
