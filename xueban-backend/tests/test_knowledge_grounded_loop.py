"""knowledge-grounded-loop example — RAG 检索节点在 control.loop 内端到端跑通。

关键契约：LIGHTRAG 未启用时 rag 节点优雅降级（空上下文 + note），Demo 无 key
也能全链路执行；启用时走真实 LightRAG 检索（此处 monkeypatch 模拟）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "runtime-contract" / "knowledge-grounded-loop.yaml"


def _load() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))


def _run(workflow: WorkflowDefinition):
    return asyncio.run(
        RuntimeService().run(RuntimeRequest(workflow=workflow, input={"goal": "knowledge-grounded demo"}))
    )


def test_example_registered_in_manifest():
    from xueban.runtime.official_examples import load_official_examples_manifest

    manifest = load_official_examples_manifest()
    assert any(ex.id == "knowledge-grounded-loop" for ex in manifest.examples)


def test_runs_end_to_end_with_rag_degraded(monkeypatch):
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    result = _run(_load())
    out = result.final_output
    assert out["loop_converged"] is True
    # retrieve 节点在 loop body 内执行过（loop body 不进顶层 steps，记录于 loop_local_visited）
    assert "retrieve" in out["loop_local_visited"]
    assert "writer" in out["loop_local_visited"]
    assert out["loop_iterations"] >= 1


def test_runs_with_lightrag_enabled(monkeypatch):
    from xueban.runtime import lightrag_adapter

    monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
    monkeypatch.setattr(lightrag_adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(lightrag_adapter, "backend_label", lambda: "lightrag")
    monkeypatch.setattr(
        lightrag_adapter, "query", lambda pack_id, question, *, mode=None: "检索到的架构决策上下文"
    )
    result = _run(_load())
    assert result.final_output["loop_converged"] is True
