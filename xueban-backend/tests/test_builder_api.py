"""tests/test_builder_api.py — Builder API endpoint 测试 (AC5)"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _minimal_blueprint_payload(name: str = "test-agent", mode: str = "single") -> dict:
    return {
        "blueprint": {
            "blueprint_id": "bp-testid12",
            "version": "1.0",
            "name": name,
            "goal": "Test the builder API",
            "audience": "developer",
            "mode": mode,
            "role_profiles": [
                {
                    "role_id": "role-aaa",
                    "name": "agent",
                    "description": "Test agent",
                    "persona": "",
                    "responsibilities": [],
                    "constraints": [],
                    "tools": [],
                    "executor_kind": "api",
                    "executor_provider": "anthropic",
                    "executor_model": "claude-sonnet-4-6",
                    "can_spawn_tasks": False,
                    "sub_agents": [],
                    "metadata": {},
                }
            ],
            "tool_policies": [],
            "knowledge_bindings": [],
            "memory_profile": {"scope": "session", "writeback_target": None, "enabled": True, "metadata": {}},
            "eval_profile": {"smoke_eval_enabled": False, "eval_criteria": [], "regression_gate": False, "metadata": {}},
            "publish_profile": {"target": "none", "visibility": "private", "publish_ref": "", "metadata": {}},
            "metadata": {},
        }
    }


# ---------------------------------------------------------------------------
# GET /builder/kits — happy path + envelope
# ---------------------------------------------------------------------------


def test_list_kits_happy_path():
    resp = client.get("/builder/kits")
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert isinstance(body["data"], list)
    assert body["meta"]["count"] == 4


def test_list_kits_stable_structure():
    resp = client.get("/builder/kits")
    kits = resp.json()["data"]
    kit_ids = {k["kit_id"] for k in kits}
    assert kit_ids == {"research_kit", "knowledge_assistant_kit", "review_approval_kit", "persona_npc_kit"}


# ---------------------------------------------------------------------------
# POST /builder/blueprints/generate
# ---------------------------------------------------------------------------


def test_generate_happy_path():
    resp = client.post("/builder/blueprints/generate", json={"goal": "Write a report"})
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert body["data"]["goal"] == "Write a report"
    assert "confidence" in body["meta"]
    # P8: AC3 requires meta.source to be present in generate response
    assert "source" in body["meta"]
    assert body["meta"]["source"] == "heuristic"


def test_generate_envelope_structure():
    resp = client.post("/builder/blueprints/generate", json={"goal": "Test goal", "mode": "team"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "meta" in body


def test_generate_missing_goal_returns_422():
    resp = client.post("/builder/blueprints/generate", json={})
    assert resp.status_code == 422


def test_generate_empty_goal_returns_422():
    """P9: Empty string goal must be rejected by field-level min_length=1 validation."""
    resp = client.post("/builder/blueprints/generate", json={"goal": ""})
    assert resp.status_code == 422


def test_generate_goal_too_long_returns_422():
    """P9: Goal exceeding max_length=2000 must be rejected."""
    resp = client.post("/builder/blueprints/generate", json={"goal": "x" * 2001})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /builder/blueprints/instantiate
# ---------------------------------------------------------------------------


def test_instantiate_happy_path():
    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/instantiate", json=payload)
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert "template_spec" in body["data"]
    assert "workflow_definition" in body["data"]
    assert "blueprint" in body["data"]


def test_instantiate_workflow_definition_is_valid():
    """workflow_definition field must be passable to WorkflowDefinition.model_validate."""
    from xueban.runtime.contracts import WorkflowDefinition

    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/instantiate", json=payload)
    assert resp.status_code == 200

    wf_raw = resp.json()["data"]["workflow_definition"]
    # Must not raise
    wf = WorkflowDefinition.model_validate(wf_raw)
    assert wf.workflow_id == "bp-testid12"


def test_instantiate_missing_blueprint_returns_422():
    resp = client.post("/builder/blueprints/instantiate", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /builder/blueprints/smoke-run
# ---------------------------------------------------------------------------


def test_smoke_run_happy_path():
    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert body["data"]["status"] in ("passed", "failed", "warning")
    assert isinstance(body["data"]["checks"], list)
    assert isinstance(body["meta"]["warnings"], list)


def test_smoke_run_response_envelope_has_required_fields():
    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert "status" in data
    assert "checks" in data
    assert "summary" in data
    assert "recommended_fix" in data
    assert "primary_blocker" in data


def test_smoke_run_check_items_have_required_fields():
    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    checks = resp.json()["data"]["checks"]

    assert len(checks) == 6
    for check in checks:
        assert "check_id" in check
        assert "label" in check
        assert "status" in check
        assert "reason" in check
        assert "target_ref" in check
        assert "failure_category" in check


def test_smoke_run_well_formed_blueprint_passes():
    payload = _minimal_blueprint_payload()
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "passed"


def test_smoke_run_citation_required_binding_triggers_citation_check():
    payload = _minimal_blueprint_payload()
    payload["blueprint"]["knowledge_bindings"] = [
        {
            "binding_id": "kb-cite01",
            "source_type": "url",
            "source_ref": "https://docs.example.com",
            "citation_required": True,
            "metadata": {},
        }
    ]
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    assert resp.status_code == 200

    checks = {c["check_id"]: c for c in resp.json()["data"]["checks"]}
    citation = checks["citation_check"]
    assert citation["status"] != "skipped"


def test_smoke_run_unspecified_knowledge_not_misclassified():
    payload = _minimal_blueprint_payload()
    payload["blueprint"]["knowledge_bindings"] = [
        {
            "binding_id": "kb-skip01",
            "source_type": "unspecified",
            "source_ref": "",
            "citation_required": False,
            "metadata": {},
        }
    ]
    resp = client.post("/builder/blueprints/smoke-run", json=payload)
    assert resp.status_code == 200

    checks = {c["check_id"]: c for c in resp.json()["data"]["checks"]}
    knowledge = checks["knowledge_accessible"]
    assert knowledge["status"] == "passed"
    assert knowledge["failure_category"] == "none"


def test_smoke_run_missing_blueprint_returns_422():
    resp = client.post("/builder/blueprints/smoke-run", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /builder/blueprints/publish — Story 8.6: real backfill
# ---------------------------------------------------------------------------


def test_publish_happy_path(tmp_path):
    """publish_blueprint happy path returns template_id / workflow_id / links envelope."""
    from unittest.mock import patch
    payload = _minimal_blueprint_payload()

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        resp = client.post("/builder/blueprints/publish", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "meta" in body

    data = body["data"]
    assert data["publish_status"] == "published"
    assert data["template_id"].startswith("bldr-")
    assert data["workflow_id"]
    assert "links" in data
    assert data["links"]["templates"] == "/templates"
    assert data["workflow_id"] in data["links"]["editor"]
    assert data["links"]["inbox"] == "/inbox"
    assert "trace_id" in body["meta"]


def test_publish_regression_blocked_format(tmp_path):
    """Publish succeeds (HTTP 200) even when RegressionService would return blocked.

    Regression gate was moved out of publish_blueprint (Story 9-6 fix):
    gate is only executed via POST /regression/{id}/run, not during publish.
    Mocking the regression service to return "blocked" has no effect on publish.
    """
    import sys
    import types
    from unittest.mock import MagicMock, patch

    mock_result = MagicMock(status="blocked", reason="baseline violated")
    mock_svc = MagicMock()
    mock_svc.gate.return_value = mock_result

    dummy_module = types.ModuleType("xueban.runtime.regression_service")
    dummy_module.RegressionService = lambda: mock_svc  # type: ignore[attr-defined]

    payload = _minimal_blueprint_payload()
    with patch.dict(sys.modules, {"xueban.runtime.regression_service": dummy_module}):
        with (
            patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
            patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
        ):
            resp = client.post("/builder/blueprints/publish", json=payload)

    # Gate is not called during publish — publish returns 200 regardless of regression state
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body


def test_publish_missing_blueprint_returns_422():
    resp = client.post("/builder/blueprints/publish", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Error envelope — invalid request
# ---------------------------------------------------------------------------


def test_bad_mode_value_returns_422():
    payload = {
        "goal": "x",
        "mode": "invalid_mode",
    }
    resp = client.post("/builder/blueprints/generate", json=payload)
    # FastAPI should reject mode enum
    assert resp.status_code == 422


def test_builder_routes_visible_in_openapi():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json().get("paths", {})
    assert "/builder/blueprints/generate" in paths
    assert "/builder/blueprints/instantiate" in paths
    assert "/builder/blueprints/smoke-run" in paths
    assert "/builder/blueprints/publish" in paths
    assert "/builder/kits" in paths
