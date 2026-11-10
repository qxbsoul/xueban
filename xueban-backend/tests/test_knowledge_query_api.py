"""Knowledge query API tests — POST /api/knowledge/packs/{pack_id}/query.

Covers:
  - query_hits 返回多片段 → 端点原样透出多条 `{text, source, score}`
    （空 source 回落 pack 名；top_k 截断；此时不得再调 query()）
  - query_hits 抛错（adapter 不支持/失败）→ 回落合成答案单条 hit
  - query_hits 返回空 → 空 hits（真实无命中，不回落合成答案）
  - LightRAG disabled (not configured): empty hits, NOT 500
  - Adapter raising mid-query: empty hits, NOT 500 (chat flow must not break)
  - Empty adapter answer: empty hits
  - Unknown pack_id → 404 KNOWLEDGE_PACK_NOT_FOUND
  - Malformed pack_id → 400 KNOWLEDGE_PACK_INVALID_ID
  - top_k bounds enforced by the schema (422)
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import xueban.api.knowledge as knowledge_api
from xueban.api.knowledge import KnowledgeService
from xueban.runtime import lightrag_adapter
from xueban.server import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_knowledge(tmp_path: Path):
    svc = KnowledgeService(storage_dir=tmp_path / "knowledge")
    knowledge_api.set_service(svc)
    try:
        yield svc
    finally:
        knowledge_api.set_service(KnowledgeService())


def _create_pack(name: str = "kb") -> str:
    resp = client.post(
        "/api/knowledge/packs",
        json={
            "name": name,
            "sources": [{"source_type": "text", "source_ref": "hello world"}],
        },
    )
    assert resp.status_code == 200
    return resp.json()["data"]["pack_id"]


def _new_uuid_hex() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# happy path — 多片段透传
# ---------------------------------------------------------------------------


def _forbid_query(pack_id: str, question: str) -> str:
    raise AssertionError("query() must not be called when query_hits succeeds")


def test_query_returns_multiple_hits_from_query_hits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    hits = [
        {"text": "片段一", "source": "docs/a.md", "score": 1.0},
        {"text": "片段二", "source": "docs/b.md", "score": 0.667},
        {"text": "片段三", "source": "", "score": 0.333},
    ]
    monkeypatch.setattr(
        lightrag_adapter, "query_hits", lambda pack_id, question, **kw: list(hits)
    )
    monkeypatch.setattr(lightrag_adapter, "query", _forbid_query)
    pack_id = _create_pack("energy-kb")

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query",
        json={"query": "氢能是什么？", "top_k": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    out = body["data"]["hits"]
    assert out[:2] == [
        {"text": "片段一", "source": "docs/a.md", "score": 1.0},
        {"text": "片段二", "source": "docs/b.md", "score": 0.667},
    ]
    # 空 source 回落 pack 名
    assert out[2]["source"] == "energy-kb"
    assert body["meta"]["pack_id"] == pack_id
    assert body["meta"]["query"] == "氢能是什么？"


def test_query_multi_hits_respect_top_k(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    hits = [{"text": f"片段{i}", "source": "s", "score": 1.0} for i in range(3)]
    monkeypatch.setattr(
        lightrag_adapter, "query_hits", lambda pack_id, question, **kw: list(hits)
    )
    monkeypatch.setattr(lightrag_adapter, "query", _forbid_query)
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "x", "top_k": 2}
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]["hits"]) == 2


# ---------------------------------------------------------------------------
# 回落：query_hits 不可用 → 合成答案单条 hit（原有行为）
# ---------------------------------------------------------------------------


def test_query_returns_hit_when_lightrag_answers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)

    def _no_hits(pack_id: str, question: str, **kw):
        raise lightrag_adapter.LightRagQueryError("structured retrieval unsupported")

    monkeypatch.setattr(lightrag_adapter, "query_hits", _no_hits)
    monkeypatch.setattr(
        lightrag_adapter, "query", lambda pack_id, question: "答案是 42。"
    )
    pack_id = _create_pack("energy-kb")

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query",
        json={"query": "答案是什么？", "top_k": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    hits = body["data"]["hits"]
    assert hits == [{"text": "答案是 42。", "source": "energy-kb", "score": 1.0}]
    assert body["meta"]["pack_id"] == pack_id
    assert body["meta"]["query"] == "答案是什么？"


# ---------------------------------------------------------------------------
# empty-hit paths (never 500)
# ---------------------------------------------------------------------------


def test_query_empty_hits_when_lightrag_disabled(monkeypatch: pytest.MonkeyPatch):
    """Not configured (LIGHTRAG_ENABLED unset) → empty hits, still 200."""
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: False)
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "anything"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["hits"] == []


def test_query_empty_hits_when_query_hits_returns_empty(monkeypatch: pytest.MonkeyPatch):
    """query_hits 返回空 = 真实无命中 → 空 hits，不回落合成答案。"""
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(
        lightrag_adapter, "query_hits", lambda pack_id, question, **kw: []
    )
    monkeypatch.setattr(lightrag_adapter, "query", _forbid_query)
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "anything"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["hits"] == []


def test_query_empty_hits_when_adapter_raises(monkeypatch: pytest.MonkeyPatch):
    """Query backend failure (missing keys / network / import) → empty hits, NOT 500."""
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)

    def _boom_hits(pack_id: str, question: str, **kw):
        raise lightrag_adapter.LightRagConfigError("missing api key")

    def _boom(pack_id: str, question: str) -> str:
        raise lightrag_adapter.LightRagConfigError("missing api key")

    monkeypatch.setattr(lightrag_adapter, "query_hits", _boom_hits)
    monkeypatch.setattr(lightrag_adapter, "query", _boom)
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "anything"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["hits"] == []


def test_query_empty_hits_when_answer_blank(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)

    def _no_hits(pack_id: str, question: str, **kw):
        raise lightrag_adapter.LightRagQueryError("structured retrieval unsupported")

    monkeypatch.setattr(lightrag_adapter, "query_hits", _no_hits)
    monkeypatch.setattr(lightrag_adapter, "query", lambda pack_id, question: "  ")
    pack_id = _create_pack()

    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "anything"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["hits"] == []


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_query_unknown_pack_returns_404(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: False)
    resp = client.post(
        f"/api/knowledge/packs/{_new_uuid_hex()}/query", json={"query": "x"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_NOT_FOUND"


def test_query_invalid_id_returns_400():
    resp = client.post("/api/knowledge/packs/not-a-uuid/query", json={"query": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_INVALID_ID"


def test_query_rejects_blank_query():
    pack_id = _create_pack()
    resp = client.post(f"/api/knowledge/packs/{pack_id}/query", json={"query": ""})
    assert resp.status_code == 422


def test_query_rejects_out_of_range_top_k():
    pack_id = _create_pack()
    resp = client.post(
        f"/api/knowledge/packs/{pack_id}/query", json={"query": "x", "top_k": 0}
    )
    assert resp.status_code == 422
