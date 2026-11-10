"""Tests for Stream K — group settings endpoints.

Covers:
  - PATCH /api/groups/{id} — group-level metadata (name + announcement)
  - GET/PUT /api/groups/{id}/user-settings — per-user preferences

Per-user preferences are stored under ``record["user_settings"][user_id]``;
the ``user_id`` is a query param defaulting to ``"anonymous"`` until auth
lands (TODO at the bottom of xueban/api/groups.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def no_byok_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable LLM auto-replies — these tests only touch persistence."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)


def _create_group(client: TestClient, name: str = "Settings Test Group") -> str:
    res = client.post(
        "/api/groups",
        json={
            "template_id": "",
            "group_template_id": "",
            "name": name,
            "agent_ids": [],
            "member_emails": [],
            "policy_matrix": {},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["group_id"]


@pytest.fixture
def fresh_group(client: TestClient, no_byok_env) -> str:
    """A freshly-created group with no extra messages or settings."""
    return _create_group(client, "Stream-K-Fresh")


DEFAULT_USER_SETTING = {
    "muted": False,
    "pinned": False,
    "folded": False,
    "show_nickname": True,
    "my_nickname": None,
}


# ---------------------------------------------------------------------------
# PATCH /api/groups/{id} — metadata
# ---------------------------------------------------------------------------


class TestPatchGroup:
    def test_patch_group_name(self, client: TestClient, fresh_group: str):
        res = client.patch(f"/api/groups/{fresh_group}", json={"name": "新群名"})
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert data["name"] == "新群名"
        # And it survives a re-GET
        res2 = client.get(f"/api/groups/{fresh_group}")
        assert res2.status_code == 200
        assert res2.json()["data"]["name"] == "新群名"

    def test_patch_group_announcement(
        self, client: TestClient, fresh_group: str
    ):
        announce = "每日 09:00 同步进度，遇阻 @张明"
        res = client.patch(
            f"/api/groups/{fresh_group}", json={"announcement": announce}
        )
        assert res.status_code == 200, res.text
        assert res.json()["data"]["announcement"] == announce

    def test_patch_group_both_fields(
        self, client: TestClient, fresh_group: str
    ):
        res = client.patch(
            f"/api/groups/{fresh_group}",
            json={"name": "Q2 冲刺组", "announcement": "本周冲 KPI"},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["name"] == "Q2 冲刺组"
        assert data["announcement"] == "本周冲 KPI"

    def test_patch_group_empty_body_bumps_updated_at(
        self, client: TestClient, fresh_group: str
    ):
        before = client.get(f"/api/groups/{fresh_group}").json()["data"]
        res = client.patch(f"/api/groups/{fresh_group}", json={})
        assert res.status_code == 200
        after = res.json()["data"]
        # No metadata changed
        assert after["name"] == before["name"]
        # updated_at is now present (and >= no value before, since record had none)
        assert "updated_at" in after

    def test_patch_group_404(self, client: TestClient):
        res = client.patch(
            "/api/groups/nonexistent-zzz", json={"name": "x"}
        )
        assert res.status_code == 404
        assert res.json()["detail"]["error"]["code"] == "GROUP_NOT_FOUND"

    def test_patch_group_name_too_long_400(
        self, client: TestClient, fresh_group: str
    ):
        # Pydantic validation should reject names > 120 chars
        res = client.patch(
            f"/api/groups/{fresh_group}", json={"name": "x" * 200}
        )
        assert res.status_code == 422

    def test_patch_group_team_id_link_and_clear(
        self, client: TestClient, fresh_group: str
    ):
        # 2026-11-06 编队补链：建群即建编队 —— 自由群 find-or-create 同名团队
        # 后回填挂链；空串 = 清除（与 conversation_id 同语义）。
        res = client.patch(
            f"/api/groups/{fresh_group}", json={"team_id": "team-heal-1"}
        )
        assert res.status_code == 200, res.text
        assert res.json()["data"]["team_id"] == "team-heal-1"
        res2 = client.get(f"/api/groups/{fresh_group}")
        assert res2.json()["data"]["team_id"] == "team-heal-1"
        # 空串清链 → None
        res3 = client.patch(
            f"/api/groups/{fresh_group}", json={"team_id": ""}
        )
        assert res3.status_code == 200
        assert res3.json()["data"]["team_id"] is None


# ---------------------------------------------------------------------------
# GET/PUT /api/groups/{id}/user-settings
# ---------------------------------------------------------------------------


class TestUserGroupSettings:
    def test_get_user_settings_default(
        self, client: TestClient, fresh_group: str
    ):
        res = client.get(f"/api/groups/{fresh_group}/user-settings")
        assert res.status_code == 200, res.text
        assert res.json()["data"] == DEFAULT_USER_SETTING

    def test_get_user_settings_404_when_group_missing(
        self, client: TestClient
    ):
        res = client.get("/api/groups/nope-xyz/user-settings")
        assert res.status_code == 404
        assert res.json()["detail"]["error"]["code"] == "GROUP_NOT_FOUND"

    def test_put_user_settings_round_trip(
        self, client: TestClient, fresh_group: str
    ):
        body = {
            "muted": True,
            "pinned": True,
            "folded": False,
            "show_nickname": True,
            "my_nickname": "张明",
        }
        res = client.put(
            f"/api/groups/{fresh_group}/user-settings", json=body
        )
        assert res.status_code == 200, res.text
        assert res.json()["data"] == body
        # Re-GET should give the same shape
        res2 = client.get(f"/api/groups/{fresh_group}/user-settings")
        assert res2.status_code == 200
        assert res2.json()["data"] == body

    def test_put_user_settings_replaces_whole_shape(
        self, client: TestClient, fresh_group: str
    ):
        # First PUT: everything on
        body1 = {
            "muted": True,
            "pinned": True,
            "folded": True,
            "show_nickname": False,
            "my_nickname": "A",
        }
        client.put(
            f"/api/groups/{fresh_group}/user-settings", json=body1
        )
        # Second PUT: everything off (whole-record replace, no merging)
        body2 = {
            "muted": False,
            "pinned": False,
            "folded": False,
            "show_nickname": True,
            "my_nickname": None,
        }
        res = client.put(
            f"/api/groups/{fresh_group}/user-settings", json=body2
        )
        assert res.json()["data"] == body2
        assert (
            client.get(f"/api/groups/{fresh_group}/user-settings").json()["data"]
            == body2
        )

    def test_user_settings_isolated_per_user(
        self, client: TestClient, fresh_group: str
    ):
        body_a = {
            "muted": True,
            "pinned": False,
            "folded": False,
            "show_nickname": True,
            "my_nickname": "A",
        }
        body_b = {
            "muted": False,
            "pinned": True,
            "folded": True,
            "show_nickname": False,
            "my_nickname": "B",
        }
        client.put(
            f"/api/groups/{fresh_group}/user-settings?user_id=alice",
            json=body_a,
        )
        client.put(
            f"/api/groups/{fresh_group}/user-settings?user_id=bob",
            json=body_b,
        )

        res_a = client.get(
            f"/api/groups/{fresh_group}/user-settings?user_id=alice"
        )
        res_b = client.get(
            f"/api/groups/{fresh_group}/user-settings?user_id=bob"
        )
        assert res_a.json()["data"] == body_a
        assert res_b.json()["data"] == body_b

        # And an unknown user still gets defaults — not bleed-over from alice/bob
        res_c = client.get(
            f"/api/groups/{fresh_group}/user-settings?user_id=carol"
        )
        assert res_c.json()["data"] == DEFAULT_USER_SETTING

    def test_put_user_settings_404_when_group_missing(
        self, client: TestClient
    ):
        res = client.put(
            "/api/groups/nope-xyz/user-settings",
            json=DEFAULT_USER_SETTING,
        )
        assert res.status_code == 404
        assert res.json()["detail"]["error"]["code"] == "GROUP_NOT_FOUND"
