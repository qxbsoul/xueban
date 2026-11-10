"""Schedules API tests — Story 14.2 AC3/AC4 + iCal-style oneshot upgrade.

Covers:
  - POST /schedules creates a recurring (cron) schedule (201)
  - POST /schedules creates a one-shot (start_at) schedule (201)
  - GET /schedules returns list
  - GET /schedules?group_id filters correctly
  - DELETE /schedules/{id} removes schedule (204)
  - GET /schedules/{id}/runs returns run history
  - 422 on invalid cron expression
  - 422 on task_description > 500 chars
  - 422 on start_at in the past
  - 422 when neither cron_expression nor start_at supplied
  - 404 on unknown schedule_id in DELETE
  - Multiple schedules per group_id are allowed (iCal-style)
  - Scheduler startup/shutdown hooks via app startup
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import xueban.api.schedules as schedules_api
from xueban.server import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_schedules(tmp_path, monkeypatch):
    """Redirect schedule storage to a temp dir for test isolation."""
    tmp_dir = tmp_path / "schedules"
    tmp_dir.mkdir()
    monkeypatch.setattr(schedules_api, "_SCHEDULES_DIR", tmp_dir)
    from xueban.services import schedule_secrets
    monkeypatch.setattr(schedule_secrets, "_KEY_PATH", tmp_path / "secret-key")
    response = client.post("/api/xueban/auth/guest")
    assert response.status_code == 200
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    yield
    client.headers.pop("Authorization", None)
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _create(group_id="grp-test", cron="0 8 * * *", agent_id="agent-1", desc="Daily brief"):
    return client.post("/api/schedules", json={
        "group_id": group_id,
        "cron_expression": cron,
        "agent_id": agent_id,
        "task_description": desc,
    })


def _create_oneshot(group_id="grp-once", agent_id="agent-1", desc="One-shot task",
                    delta_min=60, duration_min=30):
    """Create a one-shot schedule that fires `delta_min` from now."""
    start_at = datetime.now(timezone.utc) + timedelta(minutes=delta_min)
    return client.post("/api/schedules", json={
        "group_id": group_id,
        "start_at": start_at.isoformat(),
        "agent_id": agent_id,
        "task_description": desc,
        "duration_min": duration_min,
    })


# ---------------------------------------------------------------------------
# POST /schedules
# ---------------------------------------------------------------------------

def test_create_schedule_returns_201():
    res = _create()
    assert res.status_code == 201
    body = res.json()
    assert "data" in body
    data = body["data"]
    assert data["group_id"] == "grp-test"
    assert data["cron_expression"] == "0 8 * * *"
    assert data["agent_id"] == "agent-1"
    assert data["schedule_id"]


def test_create_schedule_stores_task_description():
    res = _create(desc="Morning standup")
    assert res.status_code == 201
    assert res.json()["data"]["task_description"] == "Morning standup"


def test_schedule_secrets_never_leak_to_api_or_plaintext_disk():
    token = client.headers["Authorization"]
    response = client.post("/api/schedules", headers={"X-MiMo-Key": "sk-test-never-persist"}, json={
        "group_id": "grp-secret", "cron_expression": "0 8 * * *", "task_description": "密钥隔离",
    })
    assert response.status_code == 201
    assert "_secrets" not in response.text
    assert "sk-test-never-persist" not in response.text
    listed = client.get("/api/schedules")
    assert "_secrets" not in listed.text
    record = schedules_api._load_schedule(response.json()["data"]["schedule_id"])
    import json
    stored = json.dumps(record)
    assert token not in stored
    assert "sk-test-never-persist" not in stored
    from xueban.services.schedule_secrets import decrypt_schedule_secrets
    decrypted = decrypt_schedule_secrets(record["_secrets"])
    assert decrypted["x-mimo-key"] == "sk-test-never-persist"
    assert decrypted["authorization"].startswith("Bearer ")
    assert decrypted["authorization"] != token  # 独立服务会话，不复用浏览器登录令牌


def test_create_schedule_invalid_cron_returns_422():
    res = client.post("/api/schedules", json={
        "group_id": "grp-bad",
        "cron_expression": "* * * *",  # only 4 fields — invalid
        "agent_id": "a1",
        "task_description": "",
    })
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["error"] == "invalid_cron"
    assert "example" in detail


def test_create_schedule_task_description_too_long_returns_422():
    res = client.post("/api/schedules", json={
        "group_id": "grp-long",
        "cron_expression": "0 8 * * *",
        "agent_id": "a1",
        "task_description": "x" * 501,
    })
    assert res.status_code == 422


def test_create_schedule_allows_multiple_per_group():
    # iCal-style: a group can host multiple events. The MVP 1-per-group
    # constraint has been lifted; both creates should return 201.
    r1 = _create(group_id="grp-multi")
    r2 = _create(group_id="grp-multi", cron="0 9 * * *", desc="Second event")
    assert r1.status_code == 201
    assert r2.status_code == 201
    list_res = client.get("/api/schedules?group_id=grp-multi")
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]) == 2


def test_create_oneshot_schedule_returns_201():
    res = _create_oneshot(group_id="grp-once")
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["group_id"] == "grp-once"
    assert data["start_at"] is not None
    assert data["cron_expression"] is None
    assert data["duration_min"] == 30
    assert data["completed"] is False


def test_create_oneshot_in_past_returns_422():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    res = client.post("/api/schedules", json={
        "group_id": "grp-past",
        "start_at": past.isoformat(),
        "task_description": "Too late",
    })
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "start_at_in_past"


def test_create_schedule_requires_trigger_returns_422():
    # Neither cron_expression nor start_at supplied → Pydantic validator rejects
    res = client.post("/api/schedules", json={
        "group_id": "grp-empty",
        "task_description": "No trigger",
    })
    assert res.status_code == 422


def test_create_schedule_agent_id_optional():
    # agent_id is now optional — events without an assigned agent are allowed
    res = client.post("/api/schedules", json={
        "group_id": "grp-noagent",
        "cron_expression": "0 8 * * *",
        "task_description": "No agent",
    })
    assert res.status_code == 201
    assert res.json()["data"]["agent_id"] is None


# ---------------------------------------------------------------------------
# GET /schedules
# ---------------------------------------------------------------------------

def test_list_schedules_returns_all():
    _create(group_id="grp-a")
    _create(group_id="grp-b")
    res = client.get("/api/schedules")
    assert res.status_code == 200
    data = res.json()["data"]
    ids = {r["group_id"] for r in data}
    assert {"grp-a", "grp-b"}.issubset(ids)


def test_list_schedules_filtered_by_group_id():
    _create(group_id="grp-x")
    _create(group_id="grp-y")
    res = client.get("/api/schedules?group_id=grp-x")
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data) == 1
    assert data[0]["group_id"] == "grp-x"


def test_list_schedules_empty():
    res = client.get("/api/schedules")
    assert res.status_code == 200
    assert res.json()["data"] == []


# ---------------------------------------------------------------------------
# DELETE /schedules/{id}
# ---------------------------------------------------------------------------

def test_delete_schedule_returns_204():
    created = _create().json()["data"]
    sid = created["schedule_id"]
    res = client.delete(f"/api/schedules/{sid}")
    assert res.status_code == 204
    # Verify gone
    res2 = client.get("/api/schedules")
    ids = [r["schedule_id"] for r in res2.json()["data"]]
    assert sid not in ids


def test_delete_schedule_not_found_returns_404():
    res = client.delete("/api/schedules/nonexistent-id-99")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# GET /schedules/{id}/runs
# ---------------------------------------------------------------------------

def test_get_runs_empty_for_new_schedule():
    created = _create().json()["data"]
    sid = created["schedule_id"]
    res = client.get(f"/api/schedules/{sid}/runs")
    assert res.status_code == 200
    assert res.json()["data"] == []


def test_get_runs_not_found_returns_404():
    res = client.get("/api/schedules/no-such-id/runs")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /schedules/{id} — enable/disable toggle
# ---------------------------------------------------------------------------

def test_create_schedule_defaults_enabled():
    res = _create()
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["enabled"] is True
    assert data["next_run_time"] is not None


def test_patch_disable_clears_next_run_time():
    created = _create().json()["data"]
    sid = created["schedule_id"]
    res = client.patch(f"/api/schedules/{sid}", json={"enabled": False})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled"] is False
    assert data["next_run_time"] is None
    # Persisted — a fresh list shows the same state.
    listed = client.get("/api/schedules").json()["data"]
    stored = next(r for r in listed if r["schedule_id"] == sid)
    assert stored["enabled"] is False
    assert stored["next_run_time"] is None


def test_patch_enable_recomputes_next_run_time():
    created = _create().json()["data"]
    sid = created["schedule_id"]
    client.patch(f"/api/schedules/{sid}", json={"enabled": False})
    res = client.patch(f"/api/schedules/{sid}", json={"enabled": True})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled"] is True
    assert data["next_run_time"] is not None


def test_patch_completed_oneshot_not_resurrected():
    created = _create_oneshot(group_id="grp-once-toggle").json()["data"]
    sid = created["schedule_id"]
    # Simulate a fired one-shot by marking the record completed on disk.
    record = schedules_api._load_schedule(sid)
    record["completed"] = True
    record["next_run_time"] = None
    schedules_api._save_schedule(record)
    res = client.patch(f"/api/schedules/{sid}", json={"enabled": True})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled"] is True
    assert data["completed"] is True
    assert data["next_run_time"] is None


def test_patch_not_found_returns_404():
    res = client.patch("/api/schedules/no-such-id", json={"enabled": False})
    assert res.status_code == 404


def test_patch_invalid_id_returns_400():
    res = client.patch("/api/schedules/bad id!", json={"enabled": False})
    assert res.status_code == 400


def test_patch_updates_general_fields_and_trigger():
    created = _create().json()["data"]
    sid = created["schedule_id"]
    response = client.patch(f"/api/schedules/{sid}", json={
        "cron_expression": "30 10 * * *", "agent_id": "agent-2",
        "task_description": "Updated brief", "duration_min": 45,
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["cron_expression"] == "30 10 * * *"
    assert data["agent_id"] == "agent-2"
    assert data["task_description"] == "Updated brief"
    assert data["duration_min"] == 45
    assert data["enabled"] is True
    assert data["next_run_time"] != created["next_run_time"]


def test_run_now_invokes_real_bridge_and_exposes_run_history(monkeypatch):
    calls = []
    monkeypatch.setattr(schedules_api, "_RUN_EXECUTOR_BRIDGE", lambda record: calls.append(record["schedule_id"]))
    sid = _create().json()["data"]["schedule_id"]
    response = client.post(f"/api/schedules/{sid}/run")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "succeeded"
    assert calls == [sid]
    history = client.get(f"/api/schedules/{sid}/runs")
    assert history.status_code == 200
    assert [r["run_id"] for r in history.json()["data"]] == [response.json()["data"]["run_id"]]


def test_run_now_records_executor_failure(monkeypatch):
    def fail(_record):
        raise RuntimeError("real execution failed")
    monkeypatch.setattr(schedules_api, "_RUN_EXECUTOR_BRIDGE", fail)
    sid = _create().json()["data"]["schedule_id"]
    response = client.post(f"/api/schedules/{sid}/run")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "failed"
    assert "real execution failed" in response.json()["data"]["error"]


def test_run_now_without_executor_is_not_fake_success(monkeypatch):
    monkeypatch.setattr(schedules_api, "_RUN_EXECUTOR_BRIDGE", None)
    sid = _create().json()["data"]["schedule_id"]
    response = client.post(f"/api/schedules/{sid}/run")
    assert response.status_code == 503
    assert client.get(f"/api/schedules/{sid}/runs").json()["data"] == []


def test_schedules_are_isolated_by_bearer_company():
    own_token = client.headers["Authorization"]
    sid = _create(group_id="shared-group-name").json()["data"]["schedule_id"]
    other = TestClient(app)
    registered = other.post("/api/xueban/auth/guest")
    other.headers.update({"Authorization": f"Bearer {registered.json()['token']}"})
    assert other.get("/api/schedules").json()["data"] == []
    assert other.get("/api/schedules?group_id=shared-group-name").json()["data"] == []
    for method, path, payload in (
        ("get", f"/api/schedules/{sid}/runs", None),
        ("patch", f"/api/schedules/{sid}", {"task_description": "stolen"}),
        ("post", f"/api/schedules/{sid}/run", {}),
        ("delete", f"/api/schedules/{sid}", None),
    ):
        response = getattr(other, method)(path, json=payload) if payload is not None else getattr(other, method)(path)
        assert response.status_code == 404, (path, response.text)
    client.headers.update({"Authorization": own_token})
    assert client.get(f"/api/schedules/{sid}/runs").status_code == 200
