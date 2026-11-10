"""tests/test_kit_instantiate_endpoints.py — Round 2 patches

Smoke coverage for the 4 kit-specific instantiate endpoints
(Story 10.1 / 10.2 / 10.3 / 10.4 AC5) and the publish kit-completeness gate
(Story 10.5 AC4).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from xueban.server import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# /builder/kits/{kit}/instantiate — happy paths
# ---------------------------------------------------------------------------


def test_instantiate_research_kit_happy():
    resp = client.post(
        "/builder/kits/research/instantiate",
        json={"research_topic": "LLM evaluation methods 2025"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["kit_id"] == "research_kit"
    assert body["data"]["mode"] == "team"
    assert len(body["data"]["role_profiles"]) == 4


def test_instantiate_knowledge_assistant_kit_happy():
    resp = client.post(
        "/builder/kits/knowledge_assistant/instantiate",
        json={
            "knowledge_source": "url",
            "citation_required": True,
            "low_confidence_strategy": "escalate_human",
            "escalation_keywords": ["refund"],
            "assistant_name": "DocBot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["kit_id"] == "knowledge_assistant_kit"
    assert len(body["data"]["role_profiles"]) == 3


def test_instantiate_review_approval_kit_happy():
    resp = client.post(
        "/builder/kits/review_approval/instantiate",
        json={
            "content_type": "document",
            "approval_levels": "single_review",
            "max_reject_rounds": 2,
            "output_format": "markdown",
            "reviewer_name": "R",
            "approver_name": "A",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["kit_id"] == "review_approval_kit"
    assert len(body["data"]["role_profiles"]) == 2


def test_instantiate_persona_npc_kit_happy():
    resp = client.post(
        "/builder/kits/persona_npc/instantiate",
        json={
            "persona_name": "Aria",
            "personality": "warm, witty, calm",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["kit_id"] == "persona_npc_kit"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_instantiate_research_kit_invalid_input_returns_422():
    resp = client.post(
        "/builder/kits/research/instantiate",
        json={"research_topic": "x", "max_search_rounds": 99},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_KIT_INPUT"


def test_instantiate_persona_npc_kit_invalid_memory_retention_returns_422():
    resp = client.post(
        "/builder/kits/persona_npc/instantiate",
        json={
            "persona_name": "Aria",
            "personality": "warm",
            "memory_retention": "nonsense",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Story 10.5 AC4 — publish kit completeness gate
# ---------------------------------------------------------------------------


def test_publish_rejects_blueprint_with_empty_kit_eval_profile(monkeypatch):
    """If blueprint references a kit_id whose Kit has empty eval_profile, return 400."""
    from xueban.runtime.kits.registry import REGISTRY, KitDefinition, PolicyProfile
    from xueban.runtime.contracts_builder import (
        AgentBlueprint,
        EvalProfile,
        RoleProfile,
    )

    # Build a stub kit with an empty eval_profile and register temporarily.
    empty_eval = EvalProfile(smoke_eval_enabled=False, eval_criteria=[], regression_gate=False)
    bp_default = AgentBlueprint(
        name="empty",
        goal="empty",
        mode="single",
        role_profiles=[RoleProfile(name="x")],
        eval_profile=empty_eval,
    )
    stub_kit = KitDefinition(
        kit_id="_test_empty_eval_kit",
        display_name="Test Empty Eval",
        description="Test",
        category="research",
        supported_modes=["goal"],
        default_blueprint=bp_default,
        default_policy_profile=PolicyProfile(),
        default_eval_profile=empty_eval,
        default_result_view="research_report",
        recommended_inputs=["x"],
        icon="test",
    )
    # Bypass register() (which enforces non-empty eval_profile) — we are
    # explicitly testing the defense-in-depth gate at publish time.
    REGISTRY._kits.pop("_test_empty_eval_kit", None)
    REGISTRY._kits["_test_empty_eval_kit"] = stub_kit

    try:
        payload = {
            "blueprint": {
                "blueprint_id": "bp-publish-1",
                "version": "1.0",
                "name": "Test BP",
                "goal": "Test publish gate",
                "audience": "dev",
                "mode": "single",
                "role_profiles": [
                    {
                        "role_id": "role-aaa",
                        "name": "agent",
                        "description": "",
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
                "metadata": {"kit_id": "_test_empty_eval_kit"},
            }
        }
        resp = client.post("/builder/blueprints/publish", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["error"]["code"] == "KIT_NOT_PUBLISHABLE"
    finally:
        REGISTRY._kits.pop("_test_empty_eval_kit", None)
