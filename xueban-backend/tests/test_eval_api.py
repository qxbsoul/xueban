"""Eval API tests — Story 9.5 AC6.

Covers:
  - POST /evals/profiles → 201 + profile in response
  - GET  /evals/profiles → list (empty and non-empty)
  - GET  /evals/profiles/{id} → profile data
  - GET  /evals/profiles/{id} → 404 for unknown id
  - PATCH /evals/profiles/{id} → updated profile
  - DELETE /evals/profiles/{id} → 204
  - DELETE /evals/profiles/{id} → 404 for unknown id
  - POST /evals/run/{blueprint_id}?profile_id={id} → 202 + result_id + status=running
  - GET  /evals/results/{result_id} → result data
  - GET  /evals/results/{unknown_id} → 404
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.eval_service import EvalService, set_service


@pytest.fixture(autouse=True)
def isolated_eval_service(tmp_path: Path):
    """Replace the singleton EvalService with a tmp-path-backed one."""
    svc = EvalService(
        profiles_dir=tmp_path / "profiles",
        results_dir=tmp_path / "results",
    )
    set_service(svc)
    yield svc
    # Reset to None so tests don't bleed state
    set_service(None)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def client():
    from xueban.server import app
    return TestClient(app)


def _make_profile_payload(**overrides) -> dict:
    payload = {
        "name": "api-test-profile",
        "test_prompts": ["Summarize this paper"],
        "success_metrics": [
            {
                "metric_id": "aabbccdd00112233445566778899aabb",
                "name": "task pass",
                "metric_type": "task_completion",
                "threshold": 0.8,
                "weight": 1.0,
            }
        ],
        "expected_artifacts": [],
        "citation_checks": False,
        "latency_budget_ms": 0,
        "failure_thresholds": {"max_failed_metrics": 1, "blocking_metrics": []},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /evals/profiles
# ---------------------------------------------------------------------------


def test_create_profile_returns_201(client):
    resp = client.post("/evals/profiles", json=_make_profile_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["name"] == "api-test-profile"
    assert body["data"]["profile_id"]


def test_create_profile_invalid_missing_prompts(client):
    payload = _make_profile_payload()
    payload["test_prompts"] = []
    resp = client.post("/evals/profiles", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /evals/profiles
# ---------------------------------------------------------------------------


def test_list_profiles_empty(client):
    resp = client.get("/evals/profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0


def test_list_profiles_after_create(client):
    client.post("/evals/profiles", json=_make_profile_payload(name="P1"))
    client.post("/evals/profiles", json=_make_profile_payload(name="P2"))
    resp = client.get("/evals/profiles")
    body = resp.json()
    assert body["meta"]["total"] == 2
    names = {p["name"] for p in body["data"]}
    assert names == {"P1", "P2"}


# ---------------------------------------------------------------------------
# GET /evals/profiles/{profile_id}
# ---------------------------------------------------------------------------


def test_get_profile(client):
    create_resp = client.post("/evals/profiles", json=_make_profile_payload())
    profile_id = create_resp.json()["data"]["profile_id"]
    resp = client.get(f"/evals/profiles/{profile_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["profile_id"] == profile_id


def test_get_profile_not_found(client):
    resp = client.get(f"/evals/profiles/{'a' * 32}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /evals/profiles/{profile_id}
# ---------------------------------------------------------------------------


def test_patch_profile(client):
    create_resp = client.post("/evals/profiles", json=_make_profile_payload())
    profile_id = create_resp.json()["data"]["profile_id"]
    resp = client.patch(f"/evals/profiles/{profile_id}", json={"name": "patched-name"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "patched-name"


# ---------------------------------------------------------------------------
# DELETE /evals/profiles/{profile_id}
# ---------------------------------------------------------------------------


def test_delete_profile(client):
    create_resp = client.post("/evals/profiles", json=_make_profile_payload())
    profile_id = create_resp.json()["data"]["profile_id"]
    del_resp = client.delete(f"/evals/profiles/{profile_id}")
    assert del_resp.status_code == 204
    # Subsequent GET → 404
    assert client.get(f"/evals/profiles/{profile_id}").status_code == 404


def test_delete_profile_not_found(client):
    resp = client.delete(f"/evals/profiles/{'b' * 32}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /evals/run/{blueprint_id}
# ---------------------------------------------------------------------------


def test_run_smoke_eval_returns_202(client):
    create_resp = client.post("/evals/profiles", json=_make_profile_payload())
    profile_id = create_resp.json()["data"]["profile_id"]
    resp = client.post(f"/evals/run/bp-test?profile_id={profile_id}")
    assert resp.status_code == 202
    body = resp.json()
    assert body["data"]["result_id"]
    assert body["data"]["status"] == "running"


def test_run_smoke_eval_missing_profile_id(client):
    resp = client.post("/evals/run/bp-test")
    assert resp.status_code == 422


def test_run_smoke_eval_unknown_profile(client):
    resp = client.post(f"/evals/run/bp-test?profile_id={'c' * 32}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /evals/results/{result_id}
# ---------------------------------------------------------------------------


def test_get_result_running(client):
    create_resp = client.post("/evals/profiles", json=_make_profile_payload())
    profile_id = create_resp.json()["data"]["profile_id"]
    run_resp = client.post(f"/evals/run/bp-test?profile_id={profile_id}")
    result_id = run_resp.json()["data"]["result_id"]
    # Result should be accessible (status running or completed depending on timing)
    resp = client.get(f"/evals/results/{result_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["result_id"] == result_id
    assert data["status"] in ("running", "completed")


def test_get_result_not_found(client):
    resp = client.get(f"/evals/results/{'d' * 32}")
    assert resp.status_code == 404
