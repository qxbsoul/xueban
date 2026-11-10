"""PackGraph tests — xueban/memory/pack_graph.py + API 接线。

覆盖：
  - 确定性实体抽取：wiki-link / Markdown 标题（h1-h3）/ 英文大写多词短语
  - 边：mentioned_in（标题包含）、co_occurs（同 chunk 无向加权）、links_to（相邻 wiki-link 链）
  - 内容级溯源：节点/边携带 {source_id, chunk_index, checksum}
  - 跨 source 合并：同 slug 合并、表面形式记 aliases
  - 冲突标记：同一 wiki-link 别名指向不同目标 → conflicts[]（不覆盖）
  - 持久化 round-trip + pack_id  mismatch → None
  - to_graph_data：前端 GraphData 形状（type='tag' / raw_type / val / sources）
  - query_context：query 提及实体 → 1 跳邻居按权重排序
  - API：stub 模式 /graph 回退 pack_graph；/query 响应带 graph_context
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from xueban.memory import pack_graph
from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource
from xueban.memory.retrieval_profiles import RetrievalProfile
from xueban.server import app


client = TestClient(app)


def _src(text: str) -> KnowledgeSource:
    return KnowledgeSource(source_type="text", source_ref=text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 实体抽取
# ---------------------------------------------------------------------------


def test_extracts_wikilinks_headings_and_en_phrases():
    text = (
        "# Graph Layer\n\n"
        "[[Context Graph]] 是核心。Context Graph 连接 Knowledge Base。\n"
    )
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=512, overlap=64)
    payload = builder.build("p1")

    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["graph-layer"]["kind"] == "heading"
    assert by_id["context-graph"]["kind"] == "concept"
    assert by_id["knowledge-base"]["kind"] == "concept"
    # wiki-link 与正文短语指向同一 slug → 合并为一个节点，mentions 累加
    assert by_id["context-graph"]["mentions"] == 2


def test_sentence_start_stopwords_filtered():
    text = "The Quick Brown 跳过。In Shadow Flow 不应入图。"
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=512, overlap=64)
    payload = builder.build("p1")
    ids = {n["id"] for n in payload["nodes"]}
    # "The Quick" / "In Shadow" 首词是停用词 → 过滤；"Quick Brown" 保留
    assert "the-quick" not in ids
    assert "in-shadow" not in ids


# ---------------------------------------------------------------------------
# 边与权重
# ---------------------------------------------------------------------------


def test_edges_mentioned_in_co_occurs_links_to():
    text = (
        "# 图谱层\n\n"
        "[[Alpha]] 与 [[Beta]] 相关。Gamma Delta 出现。"
    )
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=512, overlap=64)
    payload = builder.build("p1")

    edges = {(e["source"], e["target"], e["relation"]) for e in payload["edges"]}
    # 标题包含
    assert ("alpha", "图谱层", "mentioned_in") in edges
    # 同 chunk 共现（无向、端点排序）
    assert ("alpha", "beta", "co_occurs") in edges
    assert ("alpha", "gamma-delta", "co_occurs") in edges
    # 相邻 wiki-link 叙事链（有向）
    assert ("alpha", "beta", "links_to") in edges

    cooc = next(
        e for e in payload["edges"]
        if e["relation"] == "co_occurs" and {e["source"], e["target"]} == {"alpha", "beta"}
    )
    assert cooc["weight"] == 1


def test_co_occurs_weight_accumulates_across_chunks():
    # 小 chunk 强制同一对实体在多个 chunk 共现 → 权重累加
    text = "[[Alpha]] [[Beta]] " * 8
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=64, overlap=0)
    payload = builder.build("p1")
    cooc = next(
        e for e in payload["edges"]
        if e["relation"] == "co_occurs" and {e["source"], e["target"]} == {"alpha", "beta"}
    )
    assert cooc["weight"] >= 2


# ---------------------------------------------------------------------------
# 溯源（内容级血缘）
# ---------------------------------------------------------------------------


def test_provenance_carries_source_chunk_and_checksum():
    text = "[[Context Graph]] 语义层。"
    src = _src(text)
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(src, text, chunk_size=512, overlap=64)
    payload = builder.build("p1")

    node = next(n for n in payload["nodes"] if n["id"] == "context-graph")
    assert node["sources"] == [
        {"source_id": src.source_id, "chunk_index": 0, "checksum": _sha(text)}
    ]


def test_cross_source_merge_records_aliases_and_both_sources():
    src_a = _src("[[Context Graph]]")
    src_b = _src("[[context graph]]")
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(src_a, "[[Context Graph]]", chunk_size=512, overlap=64)
    builder.add_source(src_b, "[[context graph]]", chunk_size=512, overlap=64)
    payload = builder.build("p1")

    node = next(n for n in payload["nodes"] if n["id"] == "context-graph")
    assert node["name"] == "Context Graph"
    assert node["aliases"] == ["context graph"]
    assert {s["source_id"] for s in node["sources"]} == {src_a.source_id, src_b.source_id}


# ---------------------------------------------------------------------------
# 冲突标记（不静默覆盖）
# ---------------------------------------------------------------------------


def test_alias_clash_flagged_not_overwritten():
    text = "[[Alice Chen|CTO]] 与 [[Bob Li|CTO]] 都是 CTO。"
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=512, overlap=64)
    payload = builder.build("p1")

    conflicts = payload["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["field"] == "wikilink_alias"
    assert c["entity"] == "cto"
    assert c["values"] == ["alice-chen", "bob-li"]
    assert c["severity"] == "medium"
    # 两个目标节点都保留 —— 没有任何覆盖
    ids = {n["id"] for n in payload["nodes"]}
    assert {"alice-chen", "bob-li"} <= ids


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def test_build_graph_persist_and_load_roundtrip(tmp_path: Path):
    pack = KnowledgePack(
        name="kb",
        sources=[_src("# 语义层\n\n[[Context Graph]] 连接 Knowledge Base。")],
        retrieval_profile=RetrievalProfile(chunk_size=512, overlap=64),
    )
    payload = pack_graph.build_graph(pack, root=tmp_path)
    assert (tmp_path / pack.pack_id / "graph.json").exists()

    loaded = pack_graph.load_graph(pack.pack_id, root=tmp_path)
    assert loaded is not None
    assert loaded["pack_id"] == pack.pack_id
    assert loaded["schema"] == "pack_graph/2"
    assert {n["id"] for n in loaded["nodes"]} >= {"context-graph", "knowledge-base", "语义层"}
    # stats 一致
    assert loaded["stats"]["nodes"] == len(loaded["nodes"])
    assert loaded["stats"]["conflicts"] == len(loaded["conflicts"])


def test_load_graph_missing_and_mismatch(tmp_path: Path):
    assert pack_graph.load_graph("0" * 32, root=tmp_path) is None

    pack = KnowledgePack(name="kb", sources=[_src("[[Alpha]]")])
    pack_graph.build_graph(pack, root=tmp_path)
    assert pack_graph.load_graph("f" * 32, root=tmp_path) is None


def test_unparseable_source_skipped(tmp_path: Path):
    bad = KnowledgeSource(source_type="file", source_ref="nonexistent.md")
    pack = KnowledgePack(name="kb", sources=[bad, _src("[[Alpha]]")])
    payload = pack_graph.build_graph(pack, root=tmp_path)
    assert {n["id"] for n in payload["nodes"]} == {"alpha"}


# ---------------------------------------------------------------------------
# 导出与查询扩展
# ---------------------------------------------------------------------------


def test_to_graph_data_frontend_shape(tmp_path: Path):
    pack = KnowledgePack(
        name="kb",
        sources=[_src("# 图谱层\n\n[[Alpha]] 与 [[Beta]] 相关。")],
    )
    payload = pack_graph.build_graph(pack, root=tmp_path)
    data = pack_graph.to_graph_data(payload)

    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["alpha"]["type"] == "tag"
    assert nodes["alpha"]["raw_type"] == "concept"
    assert nodes["图谱层"]["raw_type"] == "heading"
    # 度数 → val（min(degree+1, 10)），与 GraphML 导出同一归一
    assert nodes["alpha"]["val"] >= 2
    # 内容级血缘透出给溯源 UI
    assert nodes["alpha"]["sources"][0]["checksum"]
    link_rels = {(l["source"], l["target"], l["relation"]) for l in data["links"]}
    assert ("alpha", "beta", "co_occurs") in link_rels
    assert data["conflicts"] == []


def test_query_context_matched_and_related(tmp_path: Path):
    pack = KnowledgePack(
        name="kb",
        sources=[_src("[[Context Graph]] 连接 Knowledge Base。")],
    )
    pack_graph.build_graph(pack, root=tmp_path)

    ctx = pack_graph.query_context(pack.pack_id, "Context Graph 怎么工作？", root=tmp_path)
    assert ctx is not None
    assert [m["id"] for m in ctx["matched"]] == ["context-graph"]
    related = {r["id"]: r for r in ctx["related"]}
    assert "knowledge-base" in related
    assert related["knowledge-base"]["via"] == "context-graph"
    assert related["knowledge-base"]["name"] == "Knowledge Base"

    # 无命中 → None（调用方省略 graph_context）
    assert pack_graph.query_context(pack.pack_id, "完全无关的问题", root=tmp_path) is None
    # 图不存在 → None
    assert pack_graph.query_context("0" * 32, "anything", root=tmp_path) is None


# ---------------------------------------------------------------------------
# API 接线（默认 storage，与 tests/test_knowledge_graph.py 同一惯例）
# ---------------------------------------------------------------------------


def _create_pack(text: str) -> str:
    resp = client.post(
        "/api/knowledge/packs",
        json={"name": f"pg-{uuid4().hex[:8]}", "sources": [{"source_type": "text", "source_ref": text}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["pack_id"]


def test_graph_endpoint_falls_back_to_pack_graph(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack("# 语义层\n\n[[Context Graph]] 连接 Knowledge Base。")
    try:
        resp = client.get(f"/api/knowledge/packs/{pack_id}/graph")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["meta"]["graph_backend"] == "pack_graph"
        assert body["meta"]["conflict_count"] == 0
        ids = {n["id"] for n in body["data"]["nodes"]}
        assert {"context-graph", "knowledge-base", "语义层"} <= ids
        assert body["meta"]["node_count"] == len(body["data"]["nodes"])
        assert body["meta"]["link_count"] == len(body["data"]["links"])
        assert body["meta"]["link_count"] > 0
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_graph_endpoint_none_backend_when_graph_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack("x")  # 无 wiki-link / 标题 / 大写短语 → 空图
    try:
        resp = client.get(f"/api/knowledge/packs/{pack_id}/graph")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"] == {"nodes": [], "links": []}
        assert body["meta"]["graph_backend"] == "none"
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_query_response_carries_graph_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack("[[Context Graph]] 连接 Knowledge Base，提供确定性检索。")
    try:
        resp = client.post(
            f"/api/knowledge/packs/{pack_id}/query",
            json={"query": "Context Graph 如何检索？", "top_k": 5},
        )
        assert resp.status_code == 200, resp.text
        ctx = resp.json()["data"]["graph_context"]
        assert ctx is not None
        assert [m["id"] for m in ctx["matched"]] == ["context-graph"]
        assert any(r["id"] == "knowledge-base" for r in ctx["related"])
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_query_graph_context_null_when_no_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack("[[Context Graph]] 连接 Knowledge Base。")
    try:
        resp = client.post(
            f"/api/knowledge/packs/{pack_id}/query",
            json={"query": "zz 完全无关 qq", "top_k": 5},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["graph_context"] is None
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


# ---------------------------------------------------------------------------
# Shadow 设计移植：构建指纹 / 图健康 / 子图 / 最短路 / relationFamily
# ---------------------------------------------------------------------------


def test_fingerprint_fresh_and_stale(tmp_path: Path):
    text = "[[Alpha]] [[Beta]]"
    src = _src(text).model_copy(update={"checksum": _sha(text)})
    pack = KnowledgePack(name="kb", sources=[src])

    # 未构建 → 不新鲜；构建后 → 新鲜
    assert not pack_graph.is_graph_fresh(pack, root=tmp_path)
    pack_graph.build_graph(pack, root=tmp_path)
    assert pack_graph.is_graph_fresh(pack, root=tmp_path)

    # 内容变化（checksum 变）→ 过期
    changed_src = src.model_copy(update={"checksum": _sha(text + " changed")})
    changed = KnowledgePack(name="kb", pack_id=pack.pack_id, sources=[changed_src])
    assert not pack_graph.is_graph_fresh(changed, root=tmp_path)

    # 切块参数变化（chunk 级溯源随之失效）→ 过期
    rechunked = KnowledgePack(
        name="kb",
        pack_id=pack.pack_id,
        sources=[src],
        retrieval_profile=RetrievalProfile(chunk_size=256, overlap=32),
    )
    assert not pack_graph.is_graph_fresh(rechunked, root=tmp_path)

    # schema 过期（模拟构图逻辑升级前的旧图）→ 强制重建
    stale_payload = pack_graph.load_graph(pack.pack_id, root=tmp_path)
    assert stale_payload is not None
    pack_graph.persist_graph({**stale_payload, "schema": "pack_graph/1"}, root=tmp_path)
    assert not pack_graph.is_graph_fresh(pack, root=tmp_path)


def test_graph_health_metrics():
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src("[[Alpha]] [[Beta]]"), "[[Alpha]] [[Beta]]", chunk_size=512, overlap=64)
    builder.add_source(_src("[[Solo]]"), "[[Solo]]", chunk_size=512, overlap=64)
    payload = builder.build("p1")

    health = pack_graph.graph_health(payload["nodes"], payload["edges"])
    assert health["nodes"] == 3
    # alpha-beta 的 co_occurs + links_to 是同一点对 → 无向计 1 条
    assert health["edges"] == 1
    assert health["density"] == round(2 / 6, 4)
    assert health["components"] == 2
    assert health["isolated"] == ["solo"]
    assert health["isolated_count"] == 1
    assert {h["id"] for h in health["hubs"]} == {"alpha", "beta"}


def test_graph_health_empty():
    health = pack_graph.graph_health([], [])
    assert health["nodes"] == 0
    assert health["density"] == 0.0
    assert health["hubs"] == []


def test_subgraph_and_shortest_path():
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src("[[Aaa]] [[Bbb]]"), "[[Aaa]] [[Bbb]]", chunk_size=512, overlap=64)
    builder.add_source(_src("[[Bbb]] [[Ccc]]"), "[[Bbb]] [[Ccc]]", chunk_size=512, overlap=64)
    payload = builder.build("p1")

    one_hop = pack_graph.subgraph(payload, "aaa", hops=1)
    assert {n["id"] for n in one_hop["nodes"]} == {"aaa", "bbb"}
    assert all(
        e["source"] in {"aaa", "bbb"} and e["target"] in {"aaa", "bbb"}
        for e in one_hop["edges"]
    )

    two_hop = pack_graph.subgraph(payload, "aaa", hops=2)
    assert {n["id"] for n in two_hop["nodes"]} == {"aaa", "bbb", "ccc"}

    assert pack_graph.shortest_path(payload, "aaa", "ccc") == ["aaa", "bbb", "ccc"]
    assert pack_graph.shortest_path(payload, "aaa", "aaa") == ["aaa"]
    assert pack_graph.shortest_path(payload, "aaa", "zzz") is None


def test_to_graph_data_relation_family(tmp_path: Path):
    pack = KnowledgePack(
        name="kb",
        sources=[_src("# 图谱层\n\n[[Alpha]] 与 [[Beta]] 相关。")],
    )
    payload = pack_graph.build_graph(pack, root=tmp_path)
    data = pack_graph.to_graph_data(payload)
    families = {(l["source"], l["target"], l["relation"]): l["relationFamily"] for l in data["links"]}
    assert families[("alpha", "beta", "co_occurs")] == "semantic"
    assert families[("alpha", "图谱层", "mentioned_in")] == "structural"
    assert families[("alpha", "beta", "links_to")] == "sequential"


def test_graph_endpoint_meta_health(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    pack_id = _create_pack("[[Alpha]] 与 [[Beta]] 相关。")
    try:
        resp = client.get(f"/api/knowledge/packs/{pack_id}/graph")
        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]
        assert meta["graph_backend"] == "pack_graph"
        health = meta["health"]
        assert health is not None
        assert health["nodes"] == meta["node_count"]
        assert health["isolated_count"] == 0
        assert {h["id"] for h in health["hubs"]} == {"alpha", "beta"}
    finally:
        client.delete(f"/api/knowledge/packs/{pack_id}")


def test_heading_hierarchy_part_of_edges():
    text = "# 顶层\n\n## 二级\n\n### 三级\n\n## 另一个二级\n"
    builder = pack_graph.PackGraphBuilder()
    builder.add_source(_src(text), text, chunk_size=512, overlap=64)
    payload = builder.build("p1")

    part_of = {
        (e["source"], e["target"]) for e in payload["edges"] if e["relation"] == "part_of"
    }
    assert ("二级", "顶层") in part_of
    assert ("三级", "二级") in part_of
    # 同级标题回退到最近上级（栈语义）
    assert ("另一个二级", "顶层") in part_of
    # 无孤岛：所有标题节点都接入了图
    health = pack_graph.graph_health(payload["nodes"], payload["edges"])
    assert health["isolated_count"] == 0
    assert health["components"] == 1
