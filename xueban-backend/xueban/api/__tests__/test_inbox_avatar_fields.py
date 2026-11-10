"""Inbox 侧栏一次成型批（2026-11-06）契约测试。

覆盖 GET /api/inbox additive 字段：
  - AgentDMItem 附带 avatar_style/seed/image/color（agent 记录透传）
  - GroupItem 附带 members 成员头像快照（agent_id/name/avatar_*）
  - agent 记录缺失时 members 回退只含 agent_id（不 500）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import xueban.api.agents as agents_mod
import xueban.api.groups as groups_mod
from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(groups_mod, "_GROUPS_DIR", tmp_path / "groups")
    return TestClient(app)


def _mk_agent(client: TestClient, **overrides) -> str:
    payload = {"name": "研究员小艾", "soul": "研究"}
    payload.update(overrides)
    r = client.post("/api/agents", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]["agent_id"]


def _mk_group(client: TestClient, agent_ids: list, name: str = "项目群") -> str:
    r = client.post(
        "/api/groups",
        json={
            "template_id": "new-task",
            "group_template_id": "",
            "name": name,
            "agent_ids": agent_ids,
            "member_emails": [],
            "policy_matrix": {},
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["group_id"]


class TestInboxAvatarFields:
    def test_dm_carries_avatar_fields(self, client):
        """agent_dms 行附带头像四件套（与 agent 记录一致）。"""
        aid = _mk_agent(
            client,
            avatar_style="bottts-neutral",
            avatar_seed="seed-42",
            avatar_color="#112233",
        )
        r = client.get("/api/inbox")
        assert r.status_code == 200
        dms = r.json()["data"]["agent_dms"]
        row = next(d for d in dms if d["agentId"] == aid)
        assert row["avatar_style"] == "bottts-neutral"
        assert row["avatar_seed"] == "seed-42"
        assert row["avatar_color"] == "#112233"

    def test_group_carries_members_snapshot(self, client):
        """groups 行 members = 成员头像快照（name + avatar 字段）。"""
        aid = _mk_agent(client, avatar_style="bottts-neutral", avatar_seed="s1")
        gid = _mk_group(client, [aid])
        r = client.get("/api/inbox")
        assert r.status_code == 200
        groups = r.json()["data"]["groups"]
        g = next(g for g in groups if g["id"] == gid)
        assert len(g["members"]) == 1
        m = g["members"][0]
        assert m["agent_id"] == aid
        assert m["name"] == "研究员小艾"
        assert m["avatar_style"] == "bottts-neutral"

    def test_members_fallback_when_agent_missing(self, client, tmp_path, monkeypatch):
        """agent 记录被删（目录清空）后群 members 回退只含 agent_id，不 500。"""
        aid = _mk_agent(client)
        gid = _mk_group(client, [aid])
        # 清空 agents 目录模拟记录丢失
        for p in (tmp_path / "agents").glob("*.json"):
            p.unlink()
        r = client.get("/api/inbox")
        assert r.status_code == 200
        groups = r.json()["data"]["groups"]
        g = next(g for g in groups if g["id"] == gid)
        assert g["members"] == [{"agent_id": aid, "name": "", "avatar_style": None,
                                 "avatar_seed": None, "avatar_image": None,
                                 "avatar_color": None}]
