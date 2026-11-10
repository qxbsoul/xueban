"""Hierarchical knowledge index tests — xueban/runtime/hier_index.py.

Covers:
  - 大纲建树：md 标题层级 → 嵌套 chapter 节点（含前言兜底），chunk 全覆盖
  - 非结构化退化：无标题文本 → 按 chunk 序号分页的 page 节点
  - 持久化 roundtrip：build → load_index 读回一致
  - 定位查询：先定位章节节点再取子树 chunk；hit 带层级 path；trace 记录定位
  - 无命中：空 hits + 空 located（调用方据此回落平铺检索）
  - API：GET /api/knowledge/packs/{id}/index 树浏览；POST query 层级命中 /
    层级无命中回落平铺（trace.strategy 区分）
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import xueban.api.knowledge as knowledge_api
from xueban.api.knowledge import KnowledgeService
from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource
from xueban.memory.retrieval_profiles import RetrievalProfile
from xueban.runtime import hier_index, lightrag_adapter
from xueban.server import app


client = TestClient(app)

MD_TEXT = (
    "本白皮书介绍氢能产业的基本情况。\n\n"
    "# 氢能白皮书\n\n"
    "氢能是二次能源。\n\n"
    "## 第一章 制氢\n\n"
    "电解水制氢需要大量电力，绿氢来自可再生电力。\n\n"
    "## 第二章 储运\n\n"
    "高压气态储运是当前主流方式，储运成本占比高。\n\n"
    "### 2.1 管道输氢\n\n"
    "管道输氢依赖基础设施建设。\n"
)

PLAIN_TEXT = " ".join(f"记录{i}号设备运行参数正常" for i in range(30))


def _make_pack(text: str, *, chunk_size: int = 64, overlap: int = 8) -> KnowledgePack:
    return KnowledgePack(
        name="energy-kb",
        sources=[KnowledgeSource(source_type="text", source_ref=text)],
        retrieval_profile=RetrievalProfile(chunk_size=chunk_size, overlap=overlap),
    )


def _all_chunk_indexes(node: dict) -> set:
    out = set(node["chunk_indexes"])
    for child in node["children"]:
        out |= _all_chunk_indexes(child)
    return out


# ---------------------------------------------------------------------------
# 建树
# ---------------------------------------------------------------------------


def test_build_outline_tree_from_markdown(tmp_path: Path):
    pack = _make_pack(MD_TEXT)
    index = hier_index.build_index(pack, root=tmp_path)

    assert index["pack_id"] == pack.pack_id
    assert len(index["documents"]) == 1
    doc = index["documents"][0]
    assert doc["kind"] == "outline"
    assert doc["title"] == "本白皮书介绍氢能产业的基本情况。"
    assert doc["chunks"], "chunks with offsets must be persisted"

    root = doc["node"]
    titles = [c["title"] for c in root["children"]]
    assert titles == ["(前言)", "氢能白皮书"]
    outline = root["children"][1]
    assert [c["title"] for c in outline["children"]] == ["第一章 制氢", "第二章 储运"]
    chapter2 = outline["children"][1]
    assert [c["title"] for c in chapter2["children"]] == ["2.1 管道输氢"]

    # 每个 chunk 都能从树上到达（前言兜底 + 章节区间全覆盖）
    assert _all_chunk_indexes(root) == {c["index"] for c in doc["chunks"]}


def test_build_paged_tree_for_unstructured_text(tmp_path: Path):
    pack = _make_pack(PLAIN_TEXT)
    index = hier_index.build_index(pack, root=tmp_path)

    doc = index["documents"][0]
    assert doc["kind"] == "paged"
    pages = doc["node"]["children"]
    assert all(p["kind"] == "page" for p in pages)
    n_chunks = len(doc["chunks"])
    assert len(pages) == (n_chunks + 2) // 3  # _PAGE_SIZE = 3
    assert _all_chunk_indexes(doc["node"]) == {c["index"] for c in doc["chunks"]}


def test_load_index_roundtrip(tmp_path: Path):
    pack = _make_pack(MD_TEXT)
    built = hier_index.build_index(pack, root=tmp_path)
    loaded = hier_index.load_index(pack.pack_id, root=tmp_path)
    assert loaded == built
    assert hier_index.load_index(pack.pack_id, root=tmp_path / "nowhere") is None


def test_build_skips_unparseable_source(tmp_path: Path):
    pack = KnowledgePack(
        name="mixed",
        sources=[
            KnowledgeSource(source_type="dataset", source_ref="dataset:nope@v1"),
            KnowledgeSource(source_type="text", source_ref=MD_TEXT),
        ],
        retrieval_profile=RetrievalProfile(chunk_size=64, overlap=8),
    )
    index = hier_index.build_index(pack, root=tmp_path)
    # dataset 源解析失败被跳过，text 源正常建树
    assert len(index["documents"]) == 1
    assert index["documents"][0]["kind"] == "outline"


# ---------------------------------------------------------------------------
# 定位查询
# ---------------------------------------------------------------------------


def test_query_locates_chapter_then_retrieves(tmp_path: Path):
    pack = _make_pack(MD_TEXT)
    hits, trace = hier_index.query(pack, "储运方式", top_k=5, root=tmp_path)

    assert hits, "expected hits inside the located subtree"
    assert trace["strategy"] == "hier"
    assert trace["located"], "trace must record the located nodes"
    located_paths = [n["path"] for n in trace["located"]]
    assert any("第二章 储运" in p for p in located_paths)
    for hit in hits:
        assert hit["path"].startswith(doc_title := hit["source"]) or doc_title in hit["path"]
        assert hit["node_id"]
        assert 0 < hit["score"] <= 1.0
    # 命中片段必须带有层级来源路径（doc > chapter 形式）
    assert all(" > " in h["path"] for h in hits)


def test_query_no_match_returns_empty(tmp_path: Path):
    pack = _make_pack(MD_TEXT)
    hits, trace = hier_index.query(pack, "量子引力波", top_k=5, root=tmp_path)
    assert hits == []
    assert trace["strategy"] == "hier"
    assert trace["located"] == []


def test_query_respects_top_k(tmp_path: Path):
    pack = _make_pack(MD_TEXT, chunk_size=48, overlap=8)
    hits, _trace = hier_index.query(pack, "氢能", top_k=2, root=tmp_path)
    assert 0 < len(hits) <= 2


def test_ensure_index_builds_for_old_pack(tmp_path: Path):
    """无持久化索引的老 pack：查询时现场构建并落盘。"""
    pack = _make_pack(MD_TEXT)
    assert hier_index.load_index(pack.pack_id, root=tmp_path) is None
    hits, _trace = hier_index.query(pack, "储运", top_k=5, root=tmp_path)
    assert hits
    assert hier_index.load_index(pack.pack_id, root=tmp_path) is not None


# ---------------------------------------------------------------------------
# API：GET /index 树浏览 + query 层级优先 / 回落
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_knowledge(tmp_path: Path):
    svc = KnowledgeService(storage_dir=tmp_path / "knowledge")
    knowledge_api.set_service(svc)
    try:
        yield svc, tmp_path / "knowledge"
    finally:
        knowledge_api.set_service(KnowledgeService())


def _create_pack(text: str = MD_TEXT, name: str = "kb") -> str:
    resp = client.post(
        "/api/knowledge/packs",
        json={
            "name": name,
            "sources": [{"source_type": "text", "source_ref": text}],
            "retrieval_profile": {"chunk_size": 64, "overlap": 8},
        },
    )
    assert resp.status_code == 200
    return resp.json()["data"]["pack_id"]


def test_get_pack_index_endpoint(_isolated_knowledge):
    _svc, storage = _isolated_knowledge
    pack_id = _create_pack()

    resp = client.get(f"/api/knowledge/packs/{pack_id}/index")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["pack_id"] == pack_id
    assert data["stats"]["documents"] == 1
    assert data["stats"]["nodes"] >= 4  # doc + 前言 + 白皮书 + 章节们
    assert data["stats"]["chunks"] > 0
    doc = data["documents"][0]
    assert doc["kind"] == "outline"
    assert doc["tree"]["children"], "tree must expose browsable children"
    # ingest 时已持久化到 pack 目录
    assert (storage / pack_id / "hier_index.json").exists()


def test_get_pack_index_unknown_pack_404():
    resp = client.get(f"/api/knowledge/packs/{uuid4().hex}/index")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_NOT_FOUND"


def test_get_pack_index_invalid_id_400():
    resp = client.get("/api/knowledge/packs/not-a-uuid/index")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_INVALID_ID"


def test_query_endpoint_hier_hits(monkeypatch: pytest.MonkeyPatch):
    """LightRAG 关闭时层级检索直接出多条带层级来源的 hits。"""
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: False)
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query",
        json={"query": "储运方式", "top_k": 5},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["hits"], "hier retrieval should produce hits"
    assert data["trace"]["strategy"] == "hier"
    assert data["trace"]["located"]
    for hit in data["hits"]:
        assert hit["path"]
        assert hit["node_id"]


def test_query_endpoint_falls_back_to_flat(monkeypatch: pytest.MonkeyPatch):
    """层级无命中 → 回落平铺 query_hits，trace 标记 hier-no-match。"""
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    flat_hits = [{"text": "平铺片段", "source": "docs/a.md", "score": 1.0}]
    monkeypatch.setattr(
        lightrag_adapter, "query_hits", lambda pack_id, question, **kw: list(flat_hits)
    )
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query",
        json={"query": "量子引力波", "top_k": 5},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["hits"] == flat_hits
    assert data["trace"]["strategy"] == "flat"
    assert data["trace"]["reason"] == "hier-no-match"
