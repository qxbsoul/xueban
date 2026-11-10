"""Memory API endpoint tests — Story 9.3 AC4 + AC6.

Covers:
  - POST /api/memory/profiles creates a profile and returns 201 + {data, meta} envelope.
  - GET  /api/memory/profiles/{profile_id} returns the profile.
  - GET  /api/memory/profiles/{bad-id} returns 400 for non-UUID ids.
  - GET  /api/memory/profiles/{unknown} returns 404.
  - PATCH /api/memory/profiles/{profile_id} updates fields and returns updated profile.
  - DELETE /api/memory/profiles/{profile_id} removes the profile (204).
  - POST /api/memory/writeback/{run_id} returns 200 with status=writeback_triggered.
  - GET  /api/memory/state/{agent_id} returns skeleton with three layers.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import xueban.api.memory as memory_api
from xueban.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_profiles():
    memory_api._profiles.clear()
    yield
    memory_api._profiles.clear()


def _create_profile(**kwargs):
    payload = {
        "working_memory_limit": 4096,
        "episodic_retention_days": 30,
        "semantic_retrieval_top_k": 5,
        "writeback_policy": "on_task_complete",
        "state_sync_policy": "lazy",
        "compression_policy": "none",
        **kwargs,
    }
    return client.post("/api/memory/profiles", json=payload)


# ---------------------------------------------------------------------------
# POST /api/memory/profiles
# ---------------------------------------------------------------------------

def test_create_profile_returns_201():
    res = _create_profile()
    assert res.status_code == 201
    body = res.json()
    assert "data" in body
    assert "meta" in body
    assert body["data"]["working_memory_limit"] == 4096
    assert body["data"]["profile_id"]


def test_create_profile_custom_values():
    res = _create_profile(working_memory_limit=8192, writeback_policy="always")
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["working_memory_limit"] == 8192
    assert data["writeback_policy"] == "always"


def test_create_profile_invalid_limit_returns_422():
    res = client.post("/api/memory/profiles", json={"working_memory_limit": 10})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/memory/profiles  — 列表端点（2026-08-23）
# ---------------------------------------------------------------------------

def test_list_profiles_empty():
    res = client.get("/api/memory/profiles")
    assert res.status_code == 200
    body = res.json()
    assert body["data"] == []
    assert body["meta"]["count"] == 0


def test_list_profiles_returns_created_profiles():
    p1 = _create_profile().json()["data"]
    p2 = _create_profile(working_memory_limit=8192).json()["data"]
    res = client.get("/api/memory/profiles")
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["count"] == 2
    ids = {p["profile_id"] for p in body["data"]}
    assert ids == {p1["profile_id"], p2["profile_id"]}
    # 返回完整画像字段，前端直接渲染
    sample = body["data"][0]
    for key in (
        "profile_id",
        "working_memory_limit",
        "episodic_retention_days",
        "semantic_retrieval_top_k",
        "writeback_policy",
        "state_sync_policy",
        "compression_policy",
        "created_at",
        "updated_at",
    ):
        assert key in sample


def test_list_profiles_excludes_deleted():
    p1 = _create_profile().json()["data"]
    _create_profile()
    client.delete(f"/api/memory/profiles/{p1['profile_id']}")
    res = client.get("/api/memory/profiles")
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["count"] == 1
    assert all(p["profile_id"] != p1["profile_id"] for p in body["data"])


# ---------------------------------------------------------------------------
# GET /api/memory/profiles/{profile_id}
# ---------------------------------------------------------------------------

def test_get_profile_returns_profile():
    created = _create_profile().json()["data"]
    pid = created["profile_id"]
    res = client.get(f"/api/memory/profiles/{pid}")
    assert res.status_code == 200
    assert res.json()["data"]["profile_id"] == pid


def test_get_profile_not_found_returns_404():
    import uuid
    fake_id = str(uuid.uuid4())
    res = client.get(f"/api/memory/profiles/{fake_id}")
    assert res.status_code == 404


def test_get_profile_invalid_id_returns_400():
    res = client.get("/api/memory/profiles/not-a-uuid!!")
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# PATCH /api/memory/profiles/{profile_id}
# ---------------------------------------------------------------------------

def test_patch_profile_updates_fields():
    created = _create_profile().json()["data"]
    pid = created["profile_id"]
    res = client.patch(f"/api/memory/profiles/{pid}", json={"working_memory_limit": 2048, "writeback_policy": "manual"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["working_memory_limit"] == 2048
    assert data["writeback_policy"] == "manual"


def test_patch_profile_not_found_returns_404():
    import uuid
    res = client.patch(f"/api/memory/profiles/{uuid.uuid4()}", json={"working_memory_limit": 2048})
    assert res.status_code == 404


def test_patch_profile_invalid_id_returns_400():
    res = client.patch("/api/memory/profiles/!!!", json={})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/memory/profiles/{profile_id}
# ---------------------------------------------------------------------------

def test_delete_profile_returns_204():
    created = _create_profile().json()["data"]
    pid = created["profile_id"]
    res = client.delete(f"/api/memory/profiles/{pid}")
    assert res.status_code == 204
    # Confirm gone
    res2 = client.get(f"/api/memory/profiles/{pid}")
    assert res2.status_code == 404


def test_delete_profile_not_found_returns_404():
    import uuid
    res = client.delete(f"/api/memory/profiles/{uuid.uuid4()}")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/memory/writeback/{run_id}
# ---------------------------------------------------------------------------

def test_manual_writeback_returns_202():
    res = client.post("/api/memory/writeback/run-abc-123")
    assert res.status_code == 202  # 202 Accepted: writeback is async
    body = res.json()
    assert body["data"]["status"] == "writeback_triggered"
    assert body["data"]["run_id"] == "run-abc-123"


# ---------------------------------------------------------------------------
# GET /api/memory/state/{agent_id}
# ---------------------------------------------------------------------------

def test_get_memory_state_returns_skeleton():
    res = client.get("/api/memory/state/agent-xyz")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["agent_id"] == "agent-xyz"
    assert "working" in data
    assert "episodic" in data
    assert "semantic" in data


# ---------------------------------------------------------------------------
# GET /api/memory/stats  — Story 14.1 placeholder mode
# ---------------------------------------------------------------------------

def test_get_memory_stats_default_values():
    res = client.get("/api/memory/stats")
    assert res.status_code == 200
    body = res.json()
    data = body["data"]
    assert data["working_memory_limit"] == 4096
    assert data["episodic_retention_days"] == 30
    assert data["semantic_skills_count"] == 0
    assert body["meta"]["mode"] == "placeholder"


def test_get_memory_stats_with_agent_id_param():
    """agent_id is accepted without 422 (ignored in placeholder mode)."""
    res = client.get("/api/memory/stats?agent_id=some-agent-id")
    assert res.status_code == 200
    assert "working_memory_limit" in res.json()["data"]


def test_writeback_response_includes_memories_recalled():
    """Story 14.1 — writeback must return memories_recalled field (stub=0)."""
    res = client.post("/api/memory/writeback/run-test-14")
    assert res.status_code == 202
    data = res.json()["data"]
    assert "memories_recalled" in data
    assert data["memories_recalled"] == 0
