"""Tests for group archive (归档群聊) — PATCH archived + list filtering.

Backend for the 群设置 modal's "归档群聊" action (2026-05-30). Data dir is
isolated to tmp by the session-scoped autouse fixture in conftest.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app

_VALID_TEMPLATE_ID = "academic-paper"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _create(client: TestClient, name: str) -> str:
    res = client.post(
        "/api/groups",
        json={"template_id": _VALID_TEMPLATE_ID, "group_template_id": "g1", "name": name},
    )
    assert res.status_code == 201
    return res.json()["group_id"]


def _list_ids(client: TestClient, *, include_archived: bool = False) -> list[str]:
    url = "/api/groups" + ("?include_archived=true" if include_archived else "")
    return [g["group_id"] for g in client.get(url).json()["data"]]


class TestArchive:
    def test_patch_sets_archived_flag(self, client: TestClient):
        gid = _create(client, "Arch Patch")
        res = client.patch(f"/api/groups/{gid}", json={"archived": True})
        assert res.status_code == 200
        assert res.json()["data"]["archived"] is True

    def test_archived_hidden_from_default_list(self, client: TestClient):
        gid = _create(client, "Arch Hidden")
        assert gid in _list_ids(client)  # visible before archiving
        client.patch(f"/api/groups/{gid}", json={"archived": True})
        assert gid not in _list_ids(client)  # gone from default list

    def test_archived_shown_when_include_archived(self, client: TestClient):
        gid = _create(client, "Arch Shown")
        client.patch(f"/api/groups/{gid}", json={"archived": True})
        assert gid in _list_ids(client, include_archived=True)

    def test_unarchive_restores_to_default_list(self, client: TestClient):
        gid = _create(client, "Arch Restore")
        client.patch(f"/api/groups/{gid}", json={"archived": True})
        assert gid not in _list_ids(client)
        client.patch(f"/api/groups/{gid}", json={"archived": False})
        assert gid in _list_ids(client)
