"""RagExecutor (kind="rag") — knowledge-grounded retrieval in the execution flow.

Contract:
- LIGHTRAG disabled → graceful degradation (empty context + note) unless required=true
- enabled → lightrag_adapter.query passthrough (answer/contexts)
- missing pack_id/query → hard error regardless of degradation policy
"""

from __future__ import annotations

import asyncio

import pytest

from xueban.runtime import lightrag_adapter
from xueban.runtime.executors import RagExecutor


def _exec(config: dict, payload: dict | None = None):
    return asyncio.run(RagExecutor().execute(config=config, payload=payload or {}))


def test_degrades_to_stub_when_lightrag_disabled(monkeypatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    result = _exec({"pack_id": "p1", "query": "what is river memory?"})
    assert result["executor"]["backend"] == "stub"
    assert result["answer"] == ""
    assert "LIGHTRAG_ENABLED" in result["note"]


def test_required_true_fails_when_disabled(monkeypatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="LIGHTRAG_ENABLED"):
        _exec({"pack_id": "p1", "query": "q", "required": True})


def test_missing_query_raises_even_when_disabled(monkeypatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    with pytest.raises(ValueError, match="non-empty query"):
        _exec({"pack_id": "p1", "query": "   "})


def test_query_passthrough_when_enabled(monkeypatch):
    monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
    captured = {}

    def fake_query(pack_id, question, *, mode=None):
        captured.update({"pack_id": pack_id, "question": question, "mode": mode})
        return "River Memory 是跨 run 的分层记忆。"

    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(lightrag_adapter, "backend_label", lambda: "lightrag")
    monkeypatch.setattr(lightrag_adapter, "query", fake_query)

    payload = {"pack_id": "p9", "prompt": "什么是 River Memory？"}
    result = _exec({"mode": "local"}, payload)
    assert captured == {"pack_id": "p9", "question": "什么是 River Memory？", "mode": "local"}
    assert result["answer"] == "River Memory 是跨 run 的分层记忆。"
    assert result["executor"]["backend"] == "lightrag"


def test_query_failure_degrades_unless_required(monkeypatch):
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(lightrag_adapter, "backend_label", lambda: "lightrag")
    monkeypatch.setattr(
        lightrag_adapter,
        "query",
        lambda *a, **kw: (_ for _ in ()).throw(lightrag_adapter.LightRagQueryError("boom")),
    )

    degraded = _exec({"pack_id": "p1", "query": "q"})
    assert degraded["answer"] == ""
    assert "boom" in degraded["note"]

    with pytest.raises(lightrag_adapter.LightRagQueryError):
        _exec({"pack_id": "p1", "query": "q", "required": True})
