"""Story 2.3 — ACP approval bridge 测试（requestPermission → approval_gate → permissionResult）。"""

from __future__ import annotations

import asyncio

import pytest

from xueban.runtime.acp.approval_bridge import AcpApprovalBridge


class TestAcpApprovalBridge:
    def test_register_returns_event(self):
        bridge = AcpApprovalBridge()
        event = bridge.register_permission_request("p1", "Execute rm -rf", "sess-1")
        assert isinstance(event, asyncio.Event)
        assert not event.is_set()

    def test_resolve_sets_event(self):
        bridge = AcpApprovalBridge()
        event = bridge.register_permission_request("p1", "Danger", "sess-1")
        result = bridge.resolve_permission("p1", granted=True)
        assert result is True
        assert event.is_set()

    def test_resolve_unknown_permission_returns_false(self):
        bridge = AcpApprovalBridge()
        result = bridge.resolve_permission("nonexistent", granted=True)
        assert result is False

    def test_get_decision_after_resolve(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p2", "Write file", "sess-2")
        bridge.resolve_permission("p2", granted=False)
        assert bridge.get_decision("p2") is False

    def test_get_decision_before_resolve_is_none(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p3", "Read secret", "sess-3")
        assert bridge.get_decision("p3") is None

    def test_list_pending(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p4", "Action 1", "sess-4")
        bridge.register_permission_request("p5", "Action 2", "sess-4")
        pending = bridge.list_pending()
        assert "p4" in pending
        assert "p5" in pending

    def test_resolved_permission_removed_from_pending(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p6", "Action", "sess-6")
        bridge.resolve_permission("p6", granted=True)
        assert "p6" not in bridge.list_pending()

    def test_get_request_returns_metadata(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p7", "Delete file", "sess-7", metadata={"risk": "high"})
        req = bridge.get_request("p7")
        assert req is not None
        assert req["description"] == "Delete file"
        assert req["risk"] == "high"

    @pytest.mark.asyncio
    async def test_await_event_resolves_after_decision(self):
        bridge = AcpApprovalBridge()
        event = bridge.register_permission_request("p8", "Send email", "sess-8")

        async def resolve_later():
            await asyncio.sleep(0.01)
            bridge.resolve_permission("p8", granted=True)

        asyncio.create_task(resolve_later())
        await asyncio.wait_for(event.wait(), timeout=1.0)
        assert bridge.get_decision("p8") is True

    def test_multiple_resolves_same_id_idempotent(self):
        bridge = AcpApprovalBridge()
        bridge.register_permission_request("p9", "Action", "sess-9")
        r1 = bridge.resolve_permission("p9", granted=True)
        r2 = bridge.resolve_permission("p9", granted=False)
        assert r1 is True
        assert r2 is False  # second call: already removed from pending
