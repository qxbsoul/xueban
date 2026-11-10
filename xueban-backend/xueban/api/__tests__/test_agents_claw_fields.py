"""ClawHub（2026-06）— Agent 新字段（claw_id / avatar_style / avatar_seed）契约测试。

覆盖（架构文档 T03 验收标准）：
  - POST /api/agents 写入三个新字段并回读
  - PATCH /api/agents/:id 局部更新（单字段）
  - PATCH "" 清空绑定（None 语义）
  - 非法格式 422（claw_id 大写/斜杠、avatar_style 空格/大写）
  - 老 JSON 记录缺新字段时读取/更新不报错（优雅降级）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import xueban.api.agents as agents_mod

    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", tmp_path / "agents")
    return TestClient(app)


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "测试 Agent", "soul": "研究助手"}
    payload.update(overrides)
    r = client.post("/api/agents", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]


class TestCreateWithClawFields:
    def test_create_with_all_three_fields(self, client):
        """POST 带全三个新字段 → record 持久化且响应透出。"""
        data = _create(
            client,
            claw_id="autoresearchclaw",
            avatar_style="bottts-neutral",
            avatar_seed="my-seed-42",
        )
        assert data["claw_id"] == "autoresearchclaw"
        assert data["avatar_style"] == "bottts-neutral"
        assert data["avatar_seed"] == "my-seed-42"

        # 回读（GET /api/agents/:id）验证落盘
        agent_id = data["agent_id"]
        r = client.get(f"/api/agents/{agent_id}")
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["claw_id"] == "autoresearchclaw"
        assert rec["avatar_style"] == "bottts-neutral"
        assert rec["avatar_seed"] == "my-seed-42"

    def test_create_without_fields_defaults_none(self, client):
        """不传新字段 → 存 None（老行为零变化）。"""
        data = _create(client)
        assert data["claw_id"] is None
        assert data["avatar_style"] is None
        assert data["avatar_seed"] is None

    def test_create_empty_string_treated_as_none(self, client):
        """POST "" → 清空语义（存 None）。"""
        data = _create(client, claw_id="", avatar_style="", avatar_seed="")
        assert data["claw_id"] is None
        assert data["avatar_style"] is None
        assert data["avatar_seed"] is None

    def test_create_invalid_claw_id_422(self, client):
        """claw_id 大写 → 422（pattern ^[a-z0-9][a-z0-9-]*$）。"""
        r = client.post(
            "/api/agents",
            json={"name": "x", "soul": "y", "claw_id": "BadClaw"},
        )
        assert r.status_code == 422

    def test_create_invalid_claw_id_slash_422(self, client):
        """claw_id 带斜杠 → 422。"""
        r = client.post(
            "/api/agents",
            json={"name": "x", "soul": "y", "claw_id": "a/b"},
        )
        assert r.status_code == 422

    def test_create_invalid_avatar_style_422(self, client):
        """avatar_style 大写/空格 → 422。"""
        r1 = client.post(
            "/api/agents",
            json={"name": "x", "soul": "y", "avatar_style": "Bottts"},
        )
        assert r1.status_code == 422
        r2 = client.post(
            "/api/agents",
            json={"name": "x", "soul": "y", "avatar_style": "has space"},
        )
        assert r2.status_code == 422


class TestPatchClawFields:
    def test_patch_sets_claw_id(self, client):
        data = _create(client)
        agent_id = data["agent_id"]
        r = client.patch(
            f"/api/agents/{agent_id}",
            json={"claw_id": "openclaw"},
        )
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["claw_id"] == "openclaw"
        # 其他字段不受影响
        assert rec["name"] == "测试 Agent"

    def test_patch_sets_avatar(self, client):
        data = _create(client)
        agent_id = data["agent_id"]
        r = client.patch(
            f"/api/agents/{agent_id}",
            json={"avatar_style": "pixel-art", "avatar_seed": "abc123"},
        )
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["avatar_style"] == "pixel-art"
        assert rec["avatar_seed"] == "abc123"

    def test_patch_empty_string_clears_binding(self, client):
        """PATCH "" → 清空（None）。"""
        data = _create(client, claw_id="openclaw", avatar_style="bottts", avatar_seed="s1")
        agent_id = data["agent_id"]
        r = client.patch(
            f"/api/agents/{agent_id}",
            json={"claw_id": "", "avatar_style": "", "avatar_seed": ""},
        )
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["claw_id"] is None
        assert rec["avatar_style"] is None
        assert rec["avatar_seed"] is None

    def test_patch_invalid_format_422(self, client):
        data = _create(client)
        agent_id = data["agent_id"]
        r = client.patch(
            f"/api/agents/{agent_id}",
            json={"claw_id": "UPPER"},
        )
        assert r.status_code == 422

    def test_patch_partial_only_touches_given_fields(self, client):
        """只 PATCH avatar_seed 不动 claw_id。"""
        data = _create(client, claw_id="openclaw", avatar_seed="old")
        agent_id = data["agent_id"]
        r = client.patch(f"/api/agents/{agent_id}", json={"avatar_seed": "new"})
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["claw_id"] == "openclaw"
        assert rec["avatar_seed"] == "new"


class TestLegacyRecordsDegradation:
    def test_legacy_record_without_fields_loads_and_patches(self, client, tmp_path):
        """老 JSON 记录缺三个新 key → GET/PATCH 不报错，PATCH 后补齐。"""
        import xueban.api.agents as agents_mod

        legacy = {
            "agent_id": "agent-legacy01",
            "name": "老记录",
            "soul": "old soul",
            "workspace_id": "default",
            "blueprint": {},
            "status": "idle",
            "source": "quick_hire",
            "created_at": "2026-01-01T00:00:00+00:00",
            "avatar_color": None,
        }
        agents_dir = agents_mod._AGENTS_DIR
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "agent-legacy01.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )

        # GET 不报错
        r = client.get("/api/agents/agent-legacy01")
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["name"] == "老记录"
        # 缺 key 用 .get 优雅降级 → 不存在即 None
        assert rec.get("claw_id") is None

        # PATCH 补上新字段
        r = client.patch(
            "/api/agents/agent-legacy01",
            json={"claw_id": "autoresearchclaw", "avatar_style": "adventurer"},
        )
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["claw_id"] == "autoresearchclaw"
        assert rec["avatar_style"] == "adventurer"

    def test_list_includes_new_fields(self, client):
        _create(client, claw_id="openclaw", avatar_style="micah")
        r = client.get("/api/agents")
        assert r.status_code == 200
        rows = r.json()["data"]
        with_claw = [row for row in rows if row.get("claw_id") == "openclaw"]
        assert len(with_claw) == 1
        assert with_claw[0]["avatar_style"] == "micah"
