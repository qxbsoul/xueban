"""Tests for Groups API — Story 7.3 (AC3 backend).

Covers:
  - POST /api/groups  → 201 on success
  - POST /api/groups  → 400 when name is empty
  - POST /api/groups  → 201 even for an unknown template_id (template
    requirement was intentionally dropped 2026-05-19; see groups.py History)
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_VALID_TEMPLATE_ID = "academic-paper"  # seed template present in /templates


def _body(**kwargs) -> dict:
    base = {
        "template_id": _VALID_TEMPLATE_ID,
        "group_template_id": "default-group",
        "name": "Test Group",
        "agent_ids": ["agent-a", "agent-b"],
        "member_emails": [],
        "policy_matrix": {},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateGroup:
    def test_create_success_returns_201(self, client: TestClient):
        res = client.post("/api/groups", json=_body())
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "Test Group"
        assert data["template_id"] == _VALID_TEMPLATE_ID
        assert "group_id" in data
        assert "created_at" in data
        assert data["agents"] == ["agent-a", "agent-b"]

    def test_create_returns_unique_group_ids(self, client: TestClient):
        r1 = client.post("/api/groups", json=_body(name="Group A"))
        r2 = client.post("/api/groups", json=_body(name="Group B"))
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["group_id"] != r2.json()["group_id"]

    def test_create_trims_whitespace_name(self, client: TestClient):
        res = client.post("/api/groups", json=_body(name="  Trimmed  "))
        assert res.status_code == 201
        assert res.json()["name"] == "Trimmed"

    def test_create_empty_name_returns_400(self, client: TestClient):
        res = client.post("/api/groups", json=_body(name=""))
        assert res.status_code == 400

    def test_create_whitespace_only_name_returns_400(self, client: TestClient):
        res = client.post("/api/groups", json=_body(name="   "))
        assert res.status_code == 400

    def test_create_unknown_template_still_succeeds(self, client: TestClient):
        """Template requirement was intentionally dropped on 2026-05-19 so that
        groups created ad-hoc from a run-session blueprint (no template) persist.
        An unknown template_id must therefore create the group (201), not 404 —
        the id is stored verbatim. See groups.py module History."""
        res = client.post("/api/groups", json=_body(template_id="no-such-template-xyz"))
        assert res.status_code == 201
        assert res.json()["template_id"] == "no-such-template-xyz"

    def test_create_minimal_body(self, client: TestClient):
        """Only required fields — agent_ids, member_emails, policy_matrix default to empty."""
        res = client.post(
            "/api/groups",
            json={
                "template_id": _VALID_TEMPLATE_ID,
                "group_template_id": "g1",
                "name": "Minimal",
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["agents"] == []
