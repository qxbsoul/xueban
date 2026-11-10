"""Tests for GAP :14/:15 — artifact pin 端点 + filecard 副标题 summary（2026-08-22）.

Covers:
  - POST /api/artifacts/{id}/pin（toggle 语义 + 持久化 + id 校验）
  - RuntimeService._build_artifacts 落 metadata.summary（首 ~120 字符纯文本）
  - export_artifact_lineage 把 pinned 标进 artifact 节点 metadata

Storage isolation: monkeypatch ``artifact_pins._PINS_DIR`` to tmp_path so tests
never touch the real ``.xueban/artifacts/`` directory (same discipline as
test_message_feedback.py).
"""

from __future__ import annotations

import asyncio
import json as _json

import pytest
from fastapi.testclient import TestClient

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime import artifact_pins
from xueban.runtime.service import _plain_text_summary
from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(artifact_pins, "_PINS_DIR", tmp_path / "artifacts")
    return TestClient(app)


class TestPinEndpoint:
    def test_pin_toggle_on_then_off(self, client: TestClient):
        r = client.post("/api/artifacts/artifact-abc123/pin")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["artifact_id"] == "artifact-abc123"
        assert data["pinned"] is True
        assert data["pinned_at"]

        r2 = client.post("/api/artifacts/artifact-abc123/pin")
        assert r2.json()["data"]["pinned"] is False
        assert r2.json()["data"]["pinned_at"] is None

    def test_pins_are_independent_per_artifact(self, client: TestClient):
        client.post("/api/artifacts/a1/pin")
        r = client.post("/api/artifacts/a2/pin")
        assert r.json()["data"]["pinned"] is True
        # a1 仍然置顶（未被 a2 影响）
        assert "a1" in artifact_pins.pinned_ids()

    def test_invalid_artifact_id_rejected(self, client: TestClient):
        r = client.post("/api/artifacts/bad%20id/pin")
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "INVALID_ARTIFACT_ID"

    def test_persists_to_disk(self, client: TestClient, tmp_path):
        client.post("/api/artifacts/a9/pin")
        stored = _json.loads(
            (tmp_path / "artifacts" / "artifact_pins.json").read_text("utf-8")
        )
        assert list(stored) == ["a9"]
        # toggle off → 键被移除
        client.post("/api/artifacts/a9/pin")
        stored = _json.loads(
            (tmp_path / "artifacts" / "artifact_pins.json").read_text("utf-8")
        )
        assert stored == {}


class TestPlainTextSummary:
    def test_collapses_whitespace(self):
        assert _plain_text_summary("# 标题\n\n正文  内容") == "# 标题 正文 内容"

    def test_truncates_at_limit(self):
        text = "x" * 200
        assert _plain_text_summary(text) == "x" * 120

    def test_non_string_returns_none(self):
        assert _plain_text_summary({"a": 1}) is None
        assert _plain_text_summary(None) is None
        assert _plain_text_summary("   \n ") is None


_WORKFLOW = {
    "workflow_id": "artifact-summary",
    "version": "0.1",
    "name": "Artifact Summary",
    "entrypoint": "writer",
    "nodes": [
        {
            "id": "writer",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "writer",
                "message_template": "[writer] done.",
                "emit": {
                    "artifact": {
                        "kind": "report",
                        "name": "report.md",
                        "content": "# 报告\n\n第一段内容。\n\n第二段内容。",
                    }
                },
            },
        }
    ],
    "edges": [{"from": "writer", "to": "END", "type": "final"}],
}


def _run_workflow(service: RuntimeService):
    workflow = WorkflowDefinition.model_validate(_WORKFLOW)
    return asyncio.run(
        service.run(RuntimeRequest(workflow=workflow, input={"goal": "g"}))
    )


class TestArtifactSummaryAndPinnedProjection:
    def test_artifact_metadata_carries_plain_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(artifact_pins, "_PINS_DIR", tmp_path / "artifacts")
        service = RuntimeService()
        result = _run_workflow(service)
        assert len(result.artifacts) == 1
        assert result.artifacts[0].metadata["summary"] == "# 报告 第一段内容。 第二段内容。"

        lineage = service.export_artifact_lineage(run_id=result.run.run_id)
        node = next(n for n in lineage.nodes if n.entity_type == "artifact")
        assert node.metadata["summary"] == "# 报告 第一段内容。 第二段内容。"
        assert node.metadata["pinned"] is False

    def test_pin_flag_flows_into_lineage_projection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(artifact_pins, "_PINS_DIR", tmp_path / "artifacts")
        service = RuntimeService()
        result = _run_workflow(service)
        artifact_id = result.artifacts[0].artifact_id

        artifact_pins.toggle_pin(artifact_id)
        lineage = service.export_artifact_lineage(run_id=result.run.run_id)
        node = next(n for n in lineage.nodes if n.entity_type == "artifact")
        assert node.metadata["pinned"] is True

        artifact_pins.toggle_pin(artifact_id)  # toggle off
        lineage = service.export_artifact_lineage(run_id=result.run.run_id)
        node = next(n for n in lineage.nodes if n.entity_type == "artifact")
        assert node.metadata["pinned"] is False
