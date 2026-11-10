"""Tests for Approvals API — Story 7.7 (AC5).

Covers:
  - GET  /api/groups/{id}/approvals/pending → returns pending list
  - POST /api/approvals/{id}/approve → 200 / 404 on missing
  - POST /api/approvals/{id}/reject → triggers RuntimeService.reject (mocked)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.api import approvals as _approvals_api


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Minimal asyncio.Event substitute for testing."""

    def __init__(self, already_set: bool = False) -> None:
        self._set = already_set

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


class _FakeService:
    def __init__(self) -> None:
        self._approval_events: Dict[Tuple[str, str], Any] = {}
        self._approval_decisions: Dict[Tuple[str, str], Any] = {}
        self._event_bus = None
        self.reject = AsyncMock()


@pytest.fixture(autouse=True)
def reset_registries():
    """Clear approval registries between tests."""
    _approvals_api._approval_registry.clear()
    _approvals_api._reverse_registry.clear()
    yield
    _approvals_api._approval_registry.clear()
    _approvals_api._reverse_registry.clear()


# ---------------------------------------------------------------------------
# GET /api/groups/{group_id}/approvals/pending
# ---------------------------------------------------------------------------


class TestGetPendingApprovals:
    def test_returns_empty_when_no_service(self, client: TestClient):
        _approvals_api.set_runtime_service(None)
        res = client.get("/api/groups/group-abc/approvals/pending")
        assert res.status_code == 200
        assert res.json() == {"items": []}

    def test_returns_pending_items(self, client: TestClient):
        svc = _FakeService()
        svc._approval_events[("run-1", "fact_checker")] = _FakeEvent(already_set=False)
        svc._approval_events[("run-2", "editor")] = _FakeEvent(already_set=False)
        _approvals_api.set_runtime_service(svc)

        res = client.get("/api/groups/group-x/approvals/pending")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 2

    def test_excludes_already_set_events(self, client: TestClient):
        svc = _FakeService()
        svc._approval_events[("run-1", "researcher")] = _FakeEvent(already_set=False)
        svc._approval_events[("run-2", "writer")] = _FakeEvent(already_set=True)
        _approvals_api.set_runtime_service(svc)

        res = client.get("/api/groups/group-y/approvals/pending")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["gate_id"] == "researcher"

    def test_item_fields_present(self, client: TestClient):
        svc = _FakeService()
        svc._approval_events[("run-99", "approver")] = _FakeEvent(already_set=False)
        _approvals_api.set_runtime_service(svc)

        res = client.get("/api/groups/group-z/approvals/pending")
        item = res.json()["items"][0]
        for field in ("approval_id", "run_id", "gate_id", "submitter_name", "submitter_kind", "summary", "triggered_at", "waiting_seconds"):
            assert field in item, f"Missing field: {field}"
        assert item["run_id"] == "run-99"
        assert item["gate_id"] == "approver"


# ---------------------------------------------------------------------------
# POST /api/approvals/{approval_id}/approve
# ---------------------------------------------------------------------------


class TestApproveApproval:
    def _setup_pending(self, run_id: str, gate_id: str) -> tuple[_FakeService, str]:
        svc = _FakeService()
        evt = _FakeEvent(already_set=False)
        svc._approval_events[(run_id, gate_id)] = evt
        _approvals_api.set_runtime_service(svc)
        # Force registry creation
        aid = _approvals_api._get_or_create_approval_id(run_id, gate_id)
        return svc, aid

    def test_approve_returns_200(self, client: TestClient):
        svc, aid = self._setup_pending("run-a", "editor")
        res = client.post(f"/api/approvals/{aid}/approve")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "approved"
        assert body["run_id"] == "run-a"
        assert body["gate_id"] == "editor"

    def test_approve_signals_event(self, client: TestClient):
        svc, aid = self._setup_pending("run-b", "checker")
        evt = svc._approval_events[("run-b", "checker")]
        assert not evt.is_set()
        client.post(f"/api/approvals/{aid}/approve")
        assert evt.is_set()

    def test_approve_sets_decision(self, client: TestClient):
        svc, aid = self._setup_pending("run-c", "writer")
        client.post(f"/api/approvals/{aid}/approve")
        decision = svc._approval_decisions.get(("run-c", "writer"), {})
        assert decision.get("decision") == "approve"

    def test_approve_404_unknown_id(self, client: TestClient):
        _approvals_api.set_runtime_service(_FakeService())
        res = client.post("/api/approvals/nonexistent-id/approve")
        assert res.status_code == 404

    def test_approve_404_already_resolved(self, client: TestClient):
        svc = _FakeService()
        evt = _FakeEvent(already_set=True)
        svc._approval_events[("run-d", "gate")] = evt
        _approvals_api.set_runtime_service(svc)
        aid = _approvals_api._get_or_create_approval_id("run-d", "gate")
        res = client.post(f"/api/approvals/{aid}/approve")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/approvals/{approval_id}/reject
# ---------------------------------------------------------------------------


class TestRejectApproval:
    def _setup_pending(self, run_id: str, gate_id: str) -> tuple[_FakeService, str]:
        svc = _FakeService()
        evt = _FakeEvent(already_set=False)
        svc._approval_events[(run_id, gate_id)] = evt
        _approvals_api.set_runtime_service(svc)
        aid = _approvals_api._get_or_create_approval_id(run_id, gate_id)
        return svc, aid

    def test_reject_returns_200(self, client: TestClient):
        svc, aid = self._setup_pending("run-e", "fact_checker")
        res = client.post(f"/api/approvals/{aid}/reject", json={"reason": "稿件有误"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "rejected"
        assert body["run_id"] == "run-e"
        assert body["gate_id"] == "fact_checker"

    def test_reject_signals_event_with_reject_decision(self, client: TestClient):
        svc, aid = self._setup_pending("run-f", "node-a")
        client.post(f"/api/approvals/{aid}/reject", json={"reason": "reason"})
        decision = svc._approval_decisions.get(("run-f", "node-a"), {})
        assert decision.get("decision") == "reject"
        assert decision.get("reason") == "reason"

    def test_reject_calls_runtime_service_reject(self, client: TestClient):
        svc, aid = self._setup_pending("run-g", "fact_checker")
        res = client.post(f"/api/approvals/{aid}/reject", json={"reason": "错误内容"})
        assert res.status_code == 200
        # RuntimeService.reject() must have been called (Story 1.3 真驳回)
        svc.reject.assert_called_once()
        call_kwargs = svc.reject.call_args.kwargs
        assert call_kwargs["run_id"] == "run-g"
        assert call_kwargs["target_node_id"] == "fact_checker"
        assert call_kwargs["reason"] == "错误内容"

    def test_reject_404_unknown_id(self, client: TestClient):
        _approvals_api.set_runtime_service(_FakeService())
        res = client.post("/api/approvals/bad-id/reject", json={"reason": ""})
        assert res.status_code == 404

    def test_reject_succeeds_even_when_service_reject_raises(self, client: TestClient):
        svc, aid = self._setup_pending("run-h", "blocked_node")
        svc.reject.side_effect = ValueError("Unknown run_id")
        res = client.post(f"/api/approvals/{aid}/reject", json={"reason": "reason"})
        # Should still return 200 — gate was unblocked even if policy check fails
        assert res.status_code == 200
