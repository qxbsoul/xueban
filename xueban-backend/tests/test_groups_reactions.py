"""Tests for Stream M — message-level actions (reactions + pin).

Covers POST /api/groups/{gid}/messages/{mid}/reactions (toggle by user) and
POST /api/groups/{gid}/messages/{mid}/pin (toggle / set).

NOTE — unlike the older test_groups_api.py, this suite **isolates storage**
by monkeypatching ``groups._GROUPS_DIR`` to a tmp_path. The legacy tests
wrote straight into the repo's real ``.xueban/groups/`` directory, which
is exactly why the dev app accumulated dozens of "Test Group" / "Group A"
fixtures. New tests should never touch the live data dir.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.api import groups


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # Redirect group persistence into the test's tmp dir so we never pollute
    # the real .xueban/groups/ directory.
    monkeypatch.setattr(groups, "_GROUPS_DIR", tmp_path / "groups")
    return TestClient(app)


def _group_with_message(client: TestClient) -> tuple[str, str]:
    """Create a group + one agent-authored message; return (group_id, message_id)."""
    g = client.post(
        "/api/groups",
        json={
            "template_id": "academic-paper",
            "group_template_id": "default-group",
            "name": "反应测试群",
            "agent_ids": ["agent-a"],
            "member_emails": [],
            "policy_matrix": {},
            "workspace_id": "default",
        },
    )
    assert g.status_code == 201, g.text
    gid = g.json()["group_id"]
    # sender_kind='agent' so we don't trigger the user→agent chat bridge.
    m = client.post(
        f"/api/groups/{gid}/messages",
        json={"content": "测反应/置顶", "sender_name": "测试员", "sender_kind": "agent"},
    )
    assert m.status_code == 201, m.text
    mid = m.json()["message_id"]
    assert mid, "每条消息应带稳定 message_id"
    return gid, mid


class TestReactions:
    def test_add_then_toggle_off(self, client: TestClient):
        gid, mid = _group_with_message(client)

        r1 = client.post(
            f"/api/groups/{gid}/messages/{mid}/reactions",
            json={"emoji": "thumbs-up", "user_id": "u1"},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["data"]["reactions"] == {"thumbs-up": ["u1"]}

        # Same user + same emoji → toggles the reaction off (and prunes empty bucket).
        r2 = client.post(
            f"/api/groups/{gid}/messages/{mid}/reactions",
            json={"emoji": "thumbs-up", "user_id": "u1"},
        )
        assert r2.status_code == 200
        assert r2.json()["data"]["reactions"] == {}

    def test_distinct_users_accumulate(self, client: TestClient):
        gid, mid = _group_with_message(client)
        client.post(f"/api/groups/{gid}/messages/{mid}/reactions", json={"emoji": "heart", "user_id": "a"})
        r = client.post(f"/api/groups/{gid}/messages/{mid}/reactions", json={"emoji": "heart", "user_id": "b"})
        assert sorted(r.json()["data"]["reactions"]["heart"]) == ["a", "b"]

    def test_reaction_persists_on_message(self, client: TestClient):
        gid, mid = _group_with_message(client)
        client.post(f"/api/groups/{gid}/messages/{mid}/reactions", json={"emoji": "flame", "user_id": "z"})
        msgs = client.get(f"/api/groups/{gid}/messages?limit=10").json()["messages"]
        target = next(m for m in msgs if m["message_id"] == mid)
        assert target["reactions"] == {"flame": ["z"]}

    def test_message_not_found_returns_404(self, client: TestClient):
        gid, _ = _group_with_message(client)
        r = client.post(
            f"/api/groups/{gid}/messages/does-not-exist/reactions",
            json={"emoji": "thumbs-up"},
        )
        assert r.status_code == 404


class TestPin:
    def test_toggle_pin(self, client: TestClient):
        gid, mid = _group_with_message(client)

        p1 = client.post(f"/api/groups/{gid}/messages/{mid}/pin", json={})
        assert p1.status_code == 200, p1.text
        assert p1.json()["data"]["pinned"] is True

        p2 = client.post(f"/api/groups/{gid}/messages/{mid}/pin", json={})
        assert p2.json()["data"]["pinned"] is False

    def test_explicit_set_pin(self, client: TestClient):
        gid, mid = _group_with_message(client)
        p = client.post(f"/api/groups/{gid}/messages/{mid}/pin", json={"pinned": True})
        assert p.json()["data"]["pinned"] is True
        # Re-reading the message should reflect the pinned flag.
        msgs = client.get(f"/api/groups/{gid}/messages?limit=10").json()["messages"]
        target = next(m for m in msgs if m["message_id"] == mid)
        assert target["pinned"] is True

    def test_pin_message_not_found_returns_404(self, client: TestClient):
        gid, _ = _group_with_message(client)
        r = client.post(f"/api/groups/{gid}/messages/nope/pin", json={})
        assert r.status_code == 404
