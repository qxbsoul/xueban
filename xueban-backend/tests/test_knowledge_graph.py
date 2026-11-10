"""Knowledge graph export endpoint — GET /api/knowledge/packs/{pack_id}/graph.

2026-08-19 配合 /knowledge/graph 图谱可视化页（从 Shadow 搬迁）新增。

覆盖：
  - stub 模式（LIGHTRAG_ENABLED 未开）→ 200 空图 + meta.rag_backend='stub'
  - LightRAG 模式 → 解析 working_dir/graph_*.graphml 为 {nodes, links}
  - GraphML 的 key/attr.name 映射、实体类型归一（type='tag' + raw_type 保留）、度数 val
  - 未知 pack → 404；非法 id → 400（既有守卫）
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from xueban.server import app

client = TestClient(app)

_GRAPHML = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="d0" for="node" attr.name="entity_id"/>
  <key id="d1" for="node" attr.name="entity_type"/>
  <key id="d2" for="node" attr.name="description"/>
  <key id="e0" for="edge" attr.name="relation"/>
  <key id="e1" for="edge" attr.name="weight"/>
  <graph id="G" edgedefault="undirected">
    <node id="n1">
      <data key="d0">Alice</data>
      <data key="d1">person</data>
      <data key="d2">The alice entity</data>
    </node>
    <node id="n2">
      <data key="d0">BobCorp</data>
      <data key="d1">organization</data>
    </node>
    <node id="n3">
      <data key="d0">ProjectX</data>
      <data key="d1">project</data>
    </node>
    <edge source="n1" target="n2">
      <data key="e0">works_for</data>
    </edge>
    <edge source="n1" target="n3">
      <data key="e0">leads</data>
    </edge>
  </graph>
</graphml>
"""


def _create_pack(name: str) -> str:
    resp = client.post(
        "/api/knowledge/packs",
        json={"name": name, "sources": [{"source_type": "text", "source_ref": "x"}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["pack_id"]


def test_graph_stub_mode_returns_empty(monkeypatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack(f"graph-stub-{uuid4().hex[:8]}")
    try:
        resp = client.get(f"/api/knowledge/packs/{pack_id}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == {"nodes": [], "links": []}
        assert body["meta"]["rag_backend"] == "stub"
        assert body["meta"]["node_count"] == 0
        assert body["meta"]["pack_id"] == pack_id
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_graph_parses_graphml(monkeypatch, tmp_path):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    from xueban.runtime import lightrag_adapter

    pack_id = _create_pack(f"graph-ml-{uuid4().hex[:8]}")
    workdir = tmp_path / "lightrag" / pack_id
    workdir.mkdir(parents=True)
    (workdir / "graph_chunk_entity_relation.graphml").write_text(_GRAPHML, encoding="utf-8")

    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
    try:
        resp = client.get(f"/api/knowledge/packs/{pack_id}/graph")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        nodes = {n["id"]: n for n in body["data"]["nodes"]}
        assert set(nodes) == {"n1", "n2", "n3"}
        # attr.name 映射：entity_id → name；entity_type 归一为 type='tag' + raw_type
        assert nodes["n1"]["name"] == "Alice"
        assert nodes["n1"]["type"] == "tag"
        assert nodes["n1"]["raw_type"] == "person"
        assert nodes["n1"]["text"] == "The alice entity"
        # 度数 → val：n1 有 2 条边，val=3（degree+1）
        assert nodes["n1"]["val"] == 3
        assert nodes["n3"]["val"] == 2
        links = body["data"]["links"]
        assert {(l["source"], l["target"], l["relation"]) for l in links} == {
            ("n1", "n2", "works_for"),
            ("n1", "n3", "leads"),
        }
        assert body["meta"]["node_count"] == 3
        assert body["meta"]["link_count"] == 2
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_graph_unknown_pack_404():
    resp = client.get("/api/knowledge/packs/{pack_id}/graph".replace("{pack_id}", "0" * 32))
    assert resp.status_code == 404
