"""POST /api/agents 契约扩展 (2026-06-01) — model / tools / 完整 persona 落库。

修复:run-session「组建」保存时此前只传短副标题 sub + 不带 model/tools,导致
持久化 agent 灵魂被削成一句话、且退化成默认 blueprint。现在 quickCreate 契约
携带完整 persona(soul,上限放宽到 8000)+ 设计期 model + tools。

数据目录由 conftest.py 的 session 级 autouse fixture 隔离到 tmp。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.runtime.defaults import DEFAULT_LLM_MODEL, DEFAULT_MCP_SERVERS


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _post(client: TestClient, **body):
    res = client.post("/api/agents", json=body)
    assert res.status_code in (200, 201), res.text
    return res.json()["data"]


def _role0(data: dict) -> dict:
    return data["blueprint"]["role_profiles"][0]


class TestQuickCreateContract:
    def test_model_and_tools_flow_into_blueprint(self, client: TestClient):
        data = _post(
            client,
            name="复现助手",
            soul="你是一名严谨的论文复现助理。",
            workspace_id="ws-test-1",
            model="claude-sonnet-4-6",
            tools=["xueban-shell", "xueban-fs"],
        )
        role = _role0(data)
        assert role["executor_model"] == "claude-sonnet-4-6"
        assert role["tools"] == ["xueban-shell", "xueban-fs"]
        # tool_policies 与 tools 一致
        policy_ids = [p["tool_id"] for p in data["blueprint"]["tool_policies"]]
        assert policy_ids == ["xueban-shell", "xueban-fs"]

    def test_defaults_when_model_tools_omitted(self, client: TestClient):
        data = _post(client, name="手动招人", soul="一句话灵魂。")
        role = _role0(data)
        assert role["executor_model"] == DEFAULT_LLM_MODEL
        assert role["tools"] == list(DEFAULT_MCP_SERVERS)

    def test_full_persona_preserved_as_soul_and_persona(self, client: TestClient):
        rich = "你是资深研究助理。\n" + ("研读·复现·答疑。" * 400)  # ~ 3600 字
        assert 2000 < len(rich) <= 8000
        data = _post(client, name="长灵魂", soul=rich, model="x", tools=[])
        role = _role0(data)
        # soul 即 persona,完整保留,不被截断/削成副标题
        assert role["persona"] == rich
        assert data["blueprint"]["goal"] == rich

    def test_empty_tools_falls_back_to_default(self, client: TestClient):
        # tools=[] 视为未指定 → 回退默认集(避免存出零工具的残废 agent)
        data = _post(client, name="空工具", soul="灵魂", tools=[])
        assert _role0(data)["tools"] == list(DEFAULT_MCP_SERVERS)

    def test_soul_over_limit_rejected(self, client: TestClient):
        too_long = "x" * 8001
        res = client.post("/api/agents", json={"name": "超长", "soul": too_long})
        assert res.status_code == 422

    def test_raci_flows_into_role_and_filters_dirty(self, client: TestClient):
        data = _post(
            client,
            name="带RACI",
            soul="灵魂",
            raci={"plan": "a", "review": "R", "approve": "X", "tool": "c"},
        )
        # 合法档位归一为大写;非法值(X)被过滤丢弃。
        assert _role0(data)["raci"] == {"plan": "A", "review": "R", "tool": "C"}

    def test_raci_defaults_empty_when_omitted(self, client: TestClient):
        data = _post(client, name="无RACI", soul="灵魂")
        assert _role0(data)["raci"] == {}
