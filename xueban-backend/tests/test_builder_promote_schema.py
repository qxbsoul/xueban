"""Story 13.6 — Schema validation tests for the promote-to-team endpoint.

Complements `test_builder_promote_from_agent.py` (which exercises HTTP behaviour
and happy/error paths) with explicit schema-level coverage for both:

  - Request schema (`PromoteFromAgentRequest`): pydantic level rejection of
    malformed payloads + endpoint-level format gate (anchor_agent_id regex).
  - Response payload: the data dict returned in 200 must round-trip through the
    canonical `AgentBlueprint` (`xueban.runtime.contracts_builder.AgentBlueprint`)
    Pydantic model — guarding the contract that the frontend builderStore can
    consume the response unchanged.

These tests exist because earlier rounds shipped only happy/404/422 path tests,
which would have missed e.g. an ad-hoc field added to the response dict that
breaks the documented blueprint contract.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from xueban.api.builder import PromoteFromAgentRequest
from xueban.runtime.catalog_service import (
    CatalogAppDetail,
    CatalogService,
    set_service as set_catalog_service,
)
from xueban.runtime.contracts_builder import AgentBlueprint
from xueban.server import app


# --- Fixtures ---------------------------------------------------------------


def _full_role() -> Dict[str, Any]:
    return {
        "role_id": "role-orig",
        "name": "Paper Reproducer",
        "description": "Reproduces papers",
        "persona": "Methodical scientist",
        "responsibilities": ["Reproduce papers"],
        "constraints": [],
        "tools": [],
        "executor_kind": "api",
        "executor_provider": "anthropic",
        "executor_model": "claude-sonnet-4-6",
        "capabilities": [],
        "handoff_rules": [],
        "persona_traits": {},
        "state_fields": [],
        "can_spawn_tasks": False,
        "sub_agents": [],
        "metadata": {},
        "collaboration_contract": {
            "scope": "team_member_candidate",
            "accepts_from": ["literature_search"],
            "delivers_to": ["report_writer"],
            "collaboration_style": "push",
        },
    }


def _detail(app_id: str, role: Dict[str, Any]) -> CatalogAppDetail:
    snapshot: Dict[str, Any] = {
        "blueprint_id": f"bp-{app_id}",
        "version": "1.0",
        "name": "Paper Reproducer",
        "goal": "Reproduce ML papers",
        "audience": "Researchers",
        "mode": "single",
        "role_profiles": [role],
        "tool_policies": [],
        "knowledge_bindings": [],
        "memory_profile": {"scope": "session", "writeback_target": None, "enabled": True, "metadata": {}},
        "eval_profile": {"smoke_eval_enabled": False, "eval_criteria": [], "regression_gate": False, "metadata": {}},
        "publish_profile": {"target": "none", "visibility": "private", "publish_ref": "", "metadata": {}},
        "metadata": {},
    }
    return CatalogAppDetail(
        app_id=app_id,
        name="Paper Reproducer",
        goal="Reproduce ML papers",
        kit_type="custom",
        author="tester",
        published_at="2026-04-28T00:00:00Z",
        fork_count=0,
        forked_from=None,
        template_id="",
        workflow_id="",
        blueprint_id=f"bp-{app_id}",
        mode="single",
        role_names=["Paper Reproducer"],
        role_count=1,
        description="Reproduce ML papers",
        blueprint_snapshot=snapshot,
    )


@pytest.fixture
def client():
    return TestClient(app)


# --- Request schema ---------------------------------------------------------


class TestPromoteFromAgentRequestSchema:
    """Pydantic-level validation of the request body."""

    def test_accepts_valid_anchor_agent_id(self):
        req = PromoteFromAgentRequest(anchor_agent_id="app-aabbccdd1234")
        assert req.anchor_agent_id == "app-aabbccdd1234"

    def test_rejects_missing_anchor_agent_id(self):
        with pytest.raises(ValidationError):
            PromoteFromAgentRequest()  # type: ignore[call-arg]

    def test_rejects_non_string_anchor_agent_id(self):
        with pytest.raises(ValidationError):
            PromoteFromAgentRequest(anchor_agent_id=123)  # type: ignore[arg-type]

    def test_endpoint_rejects_missing_body_key(self, client: TestClient):
        # FastAPI/Pydantic should turn missing required key into 422.
        resp = client.post("/builder/teams/from-agent", json={})
        assert resp.status_code == 422

    def test_endpoint_rejects_empty_string_via_format_gate(self, client: TestClient):
        # Empty strings are rejected by Pydantic min_length=1 (FastAPI native
        # detail list); the endpoint format gate handles non-empty invalid ids.
        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": ""})
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"][0]["type"] == "string_too_short"

    def test_endpoint_rejects_anchor_id_with_path_traversal(self, client: TestClient):
        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": "../../etc/passwd"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "INVALID_ANCHOR_AGENT_ID"

    def test_endpoint_rejects_anchor_id_too_long(self, client: TestClient):
        # 65 chars > regex {1,64} upper bound
        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": "a" * 65},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "INVALID_ANCHOR_AGENT_ID"


# --- Response schema -------------------------------------------------------


class TestPromoteFromAgentResponseSchema:
    """The 200 response data dict must validate against canonical AgentBlueprint."""

    def test_response_data_round_trips_through_agent_blueprint_model(
        self, client: TestClient
    ):
        catalog_id = "app-schema-rt0"
        detail = _detail(catalog_id, _full_role())

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": catalog_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        bp_dict = body["data"]

        # The response must be a structurally-valid AgentBlueprint.
        bp = AgentBlueprint.model_validate(bp_dict)

        # Spot-checks on derived fields.
        assert bp.mode == "team"
        assert bp.blueprint_id.startswith("team-from-")
        assert len(bp.role_profiles) == 1
        anchor = bp.role_profiles[0]
        # metadata.anchor must survive the round-trip.
        assert anchor.metadata.get("anchor") is True
        assert anchor.metadata.get("imported_from") == catalog_id
        # blueprint.metadata.anchor_role_id must reference the actual role.
        assert bp.metadata.get("anchor_role_id") == anchor.role_id

    def test_response_meta_carries_required_keys(self, client: TestClient):
        catalog_id = "app-meta-keys00"
        detail = _detail(catalog_id, _full_role())
        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": catalog_id},
        )
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        # trace_id (Round-1 P12), timestamp, anchor_agent_id all required for
        # downstream observability.
        assert isinstance(meta.get("trace_id"), str) and meta["trace_id"].startswith("trace-")
        assert isinstance(meta.get("timestamp"), str) and "T" in meta["timestamp"]
        assert meta.get("anchor_agent_id") == catalog_id

    def test_anchor_role_validates_against_role_profile_model(
        self, client: TestClient
    ):
        # Snapshot has a malformed role (missing required field): endpoint
        # must reject with 422 CATALOG_BLUEPRINT_INVALID rather than emit
        # an incomplete Blueprint that fails downstream model validation.
        catalog_id = "app-bad-role00"
        broken_role = _full_role()
        # executor_kind has Literal["api","cli"] — invalid value triggers
        # ValidationError during the endpoint's RoleProfile.model_validate gate.
        broken_role["executor_kind"] = "definitely-not-a-valid-kind"
        detail = _detail(catalog_id, broken_role)

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": catalog_id},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "CATALOG_BLUEPRINT_INVALID"
