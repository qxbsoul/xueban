"""L6 证据层端点测试 — runs 列表 / loop verdicts / loop SSE / artifact preview_url.

覆盖 xueban/api/runs.py 新增的：
  GET /runs?limit=                                → RunSummary[]（raw array, newest first）
  GET /runs/{id}/loops/{loop_id}/verdicts         → {iteration, score, rationale, claims[]}
  GET /runs/{id}/loops/stream                     → SSE（loop.snapshot + 增量 loop.* 帧）
以及 xueban/runtime/service.py 的：
  _emit_run_event loop 域事件 → RunEventBus 扇出
  export_artifact_lineage artifact 节点 metadata.preview_url / uri_scheme
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.contracts import (
    ArtifactRef,
    RunRecord,
    RunResult,
    StepRecord,
    WritebackRef,
)
from xueban.runtime.events import RunEventBus
from xueban.runtime.service import RuntimeService, artifact_preview_url, artifact_uri_scheme

RUN_ID = "run-evidence-1"
LOOP_ID = "loop-evidence-1"


def _writeback() -> WritebackRef:
    return WritebackRef(
        channel="artifact",
        target="host",
        mode="reference",
        host_action="persist_artifact_ref",
    )


def _iteration(index: int, score: float, reasoning: str) -> dict:
    """Mimic loop_engine 落盘的 IterationRecord.model_dump 结构."""
    return {
        "iteration_id": f"iter-{index}",
        "loop_id": LOOP_ID,
        "run_id": RUN_ID,
        "index": index,
        "evidence": {
            "body_mode": "sequential",
            "body_node_ids": ["body-1"],
            "sensor": {
                "sensor_type": "verifier",
                "value": score,
                "converged": score >= 0.95,
                "classification": "converged" if score >= 0.95 else "diverging",
                "verifier_score": score,
                "verifier_confidence": 0.9,
                "verifier_reasoning": reasoning,
            },
            "claims": [
                {
                    "stmt": f"measured={score}",
                    "evidence_ref": "body:body-1",
                    "verified": "converged" if score >= 0.95 else "diverging",
                }
            ],
        },
        "metrics": {"quality": score},
        "verifier_score": score,
        "trace_id": RUN_ID,
        "created_at": datetime(2026, 8, 22, tzinfo=timezone.utc).isoformat(),
    }


def _loop_record() -> dict:
    return {
        "loop_id": LOOP_ID,
        "run_id": RUN_ID,
        "kind": "until_converge",
        "setpoint": {"metric": "quality", "op": ">=", "value": 0.95},
        "iteration_count": 2,
        "iterations": [
            _iteration(0, 0.6, "初稿离目标尚远"),
            _iteration(1, 0.97, "已达到质量阈值"),
        ],
        "status": "converged",
    }


def _make_run(
    run_id: str = RUN_ID,
    started_at: datetime | None = None,
    with_loop: bool = True,
    artifact_uri: str = "memory://run-evidence-1/writer/0/0",
) -> RunResult:
    started = started_at or datetime(2026, 8, 22, tzinfo=timezone.utc)
    step = StepRecord(
        step_id=f"step-{run_id}",
        run_id=run_id,
        node_id="loop-node",
        status="succeeded",
        index=0,
        started_at=started,
        ended_at=started + timedelta(seconds=5),
        output=(
            {
                "loop_id": LOOP_ID,
                "loop_status": "converged",
                "loop_record": _loop_record(),
            }
            if with_loop
            else {"message": "ok"}
        ),
    )
    artifact = ArtifactRef(
        artifact_id=f"artifact-{run_id}",
        kind="report",
        name="final.md",
        uri=artifact_uri,
        producer_step_id=step.step_id,
        writeback=_writeback(),
    )
    return RunResult(
        run=RunRecord(
            run_id=run_id,
            request_id=f"req-{run_id}",
            workflow_id="wf-evidence",
            status="succeeded",
            started_at=started,
            ended_at=started + timedelta(seconds=5),
            entrypoint="loop-node",
        ),
        steps=[step],
        artifacts=[artifact],
    )


@pytest.fixture()
def runtime() -> RuntimeService:
    svc = RuntimeService(event_bus=RunEventBus())
    svc._runs[RUN_ID] = _make_run()
    return svc


@pytest.fixture()
def client(runtime: RuntimeService, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import xueban.server as server

    monkeypatch.setattr(server, "runtime_service", runtime)
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Gap 3: GET /runs?limit=
# ---------------------------------------------------------------------------


class TestListRuns:
    def test_returns_raw_array_newest_first(self, client: TestClient, runtime: RuntimeService) -> None:
        older = _make_run(run_id="run-evidence-0", started_at=datetime(2026, 8, 21, tzinfo=timezone.utc))
        runtime._runs["run-evidence-0"] = older

        res = client.get("/runs")
        assert res.status_code == 200
        payload = res.json()
        assert isinstance(payload, list)
        run_ids = [item["run_id"] for item in payload]
        assert run_ids[:2] == [RUN_ID, "run-evidence-0"]
        assert payload[0]["workflow_id"] == "wf-evidence"
        assert payload[0]["status"] == "succeeded"

    def test_limit_caps_results(self, client: TestClient, runtime: RuntimeService) -> None:
        runtime._runs["run-evidence-0"] = _make_run(
            run_id="run-evidence-0", started_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
        )

        res = client.get("/runs", params={"limit": 1})
        assert res.status_code == 200
        payload = res.json()
        assert len(payload) == 1
        assert payload[0]["run_id"] == RUN_ID  # newest first

    def test_api_alias_works(self, client: TestClient) -> None:
        res = client.get("/api/runs", params={"limit": 5})
        assert res.status_code == 200
        assert any(item["run_id"] == RUN_ID for item in res.json())

    def test_limit_must_be_positive(self, client: TestClient) -> None:
        res = client.get("/runs", params={"limit": 0})
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# Gap 1: GET /runs/{id}/loops/{loop_id}/verdicts
# ---------------------------------------------------------------------------


class TestLoopVerdicts:
    def test_returns_per_iteration_verdicts(self, client: TestClient) -> None:
        res = client.get(f"/runs/{RUN_ID}/loops/{LOOP_ID}/verdicts")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["run_id"] == RUN_ID
        assert data["loop_id"] == LOOP_ID
        assert data["loop_status"] == "converged"
        assert len(data["verdicts"]) == 2

        first, second = data["verdicts"]
        assert first["iteration"] == 0
        assert first["score"] == pytest.approx(0.6)
        assert first["rationale"] == "初稿离目标尚远"
        assert first["claims"] == [
            {"stmt": "measured=0.6", "evidence_ref": "body:body-1", "verified": "diverging"}
        ]
        assert second["iteration"] == 1
        assert second["score"] == pytest.approx(0.97)
        assert second["rationale"] == "已达到质量阈值"

    def test_api_alias(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/{RUN_ID}/loops/{LOOP_ID}/verdicts")
        assert res.status_code == 200
        assert len(res.json()["data"]["verdicts"]) == 2

    def test_unknown_run_404(self, client: TestClient) -> None:
        res = client.get("/runs/run-missing/loops/loop-x/verdicts")
        assert res.status_code == 404
        assert res.json()["detail"]["error"]["code"] == "RUN_NOT_FOUND"

    def test_unknown_loop_404(self, client: TestClient) -> None:
        res = client.get(f"/runs/{RUN_ID}/loops/loop-missing/verdicts")
        assert res.status_code == 404
        assert res.json()["detail"]["error"]["code"] == "LOOP_NOT_FOUND"


# ---------------------------------------------------------------------------
# Gap 2: GET /runs/{id}/loops/stream (SSE)
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict]:
    frames = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        frame: dict = {}
        for line in chunk.splitlines():
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                import json

                frame["data"] = json.loads(line[6:])
        frames.append(frame)
    return frames


class TestLoopStream:
    def test_snapshot_then_incremental_frames(self, client: TestClient, runtime: RuntimeService) -> None:
        bus = runtime.event_bus
        assert bus is not None
        # 预置一条 loop 增量事件 + 一条非 loop 事件（应被过滤），然后关闭流。
        bus.publish(RUN_ID, {
            "type": "loop.iteration",
            "run_id": RUN_ID,
            "node_id": "loop-node",
            "iteration_index": 2,
            "loop_id": LOOP_ID,
            "converged": True,
            "verifier_score": 0.97,
        })
        bus.publish(RUN_ID, {"type": "node.started", "run_id": RUN_ID, "node_id": "other"})
        bus.publish(RUN_ID, {
            "type": "node.retried",
            "run_id": RUN_ID,
            "node_id": "loop-node",
            "loop_id": LOOP_ID,
            "iteration": 1,
        })
        bus.close_run(RUN_ID)

        with client.stream("GET", f"/runs/{RUN_ID}/loops/stream") as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            body = "".join(res.iter_text())

        frames = _parse_sse(body)
        events = [f["event"] for f in frames]
        assert events[0] == "loop.snapshot"
        snapshot = frames[0]["data"]
        assert snapshot["run_id"] == RUN_ID
        assert snapshot["total_count"] == 1
        assert snapshot["loops"][0]["loop"]["loop_id"] == LOOP_ID

        assert "loop.iteration" in events
        iteration_frame = next(f for f in frames if f["event"] == "loop.iteration")
        assert iteration_frame["data"]["loop_id"] == LOOP_ID
        assert iteration_frame["data"]["verifier_score"] == pytest.approx(0.97)

        # node.retried 带 loop_id → 透出；node.started → 过滤
        assert "node.retried" in events
        assert "node.started" not in events

    def test_api_alias_and_last_event_id(self, client: TestClient, runtime: RuntimeService) -> None:
        bus = runtime.event_bus
        assert bus is not None
        bus.publish(RUN_ID, {"type": "loop.iteration", "run_id": RUN_ID, "loop_id": LOOP_ID, "iteration_index": 0})
        bus.publish(RUN_ID, {"type": "loop.iteration", "run_id": RUN_ID, "loop_id": LOOP_ID, "iteration_index": 1})
        bus.close_run(RUN_ID)

        with client.stream("GET", f"/api/runs/{RUN_ID}/loops/stream", params={"last_event_id": "0"}) as res:
            assert res.status_code == 200
            body = "".join(res.iter_text())

        frames = _parse_sse(body)
        iteration_frames = [f for f in frames if f["event"] == "loop.iteration"]
        # last_event_id=0 → 只重放 seq>=1 的增量帧
        assert len(iteration_frames) == 1
        assert iteration_frames[0]["data"]["iteration_index"] == 1


class TestEmitRunEventBusFanout:
    """service._emit_run_event: loop 域事件扇出到 bus，其余事件保持只落 durable log."""

    def test_loop_events_fan_out(self) -> None:
        bus = RunEventBus()
        svc = RuntimeService(event_bus=bus)  # run_event_log=None
        svc._emit_run_event(
            RUN_ID, "loop.iteration", node_id="loop-node", iteration_index=3,
            payload={"loop_id": LOOP_ID, "converged": False},
        )
        events = bus.get_events(RUN_ID)
        assert len(events) == 1
        _, evt = events[0]
        assert evt["type"] == "loop.iteration"
        assert evt["loop_id"] == LOOP_ID
        assert evt["iteration_index"] == 3

    def test_non_loop_events_not_fanned_out(self) -> None:
        bus = RunEventBus()
        svc = RuntimeService(event_bus=bus)
        svc._emit_run_event(RUN_ID, "run.started", payload={"workflow_id": "wf"})
        svc._emit_run_event(RUN_ID, "checkpoint.saved", payload={"checkpoint_id": "cp-1"})
        assert bus.get_events(RUN_ID) == []

    def test_no_bus_no_crash(self) -> None:
        svc = RuntimeService()  # event_bus=None
        svc._emit_run_event(RUN_ID, "loop.iteration", payload={"loop_id": LOOP_ID})


# ---------------------------------------------------------------------------
# Gap 4: artifact 节点 preview_url
# ---------------------------------------------------------------------------


class TestArtifactPreviewUrl:
    def test_scheme_mapping(self) -> None:
        assert artifact_uri_scheme("memory://r/n/0/0") == "memory"
        assert artifact_uri_scheme("https://example.com/a.md") == "https"
        assert artifact_uri_scheme("no-scheme") is None
        assert artifact_preview_url("https://example.com/a.md") == "https://example.com/a.md"
        assert artifact_preview_url("http://example.com/a.md") == "http://example.com/a.md"
        assert artifact_preview_url("memory://r/n/0/0") is None
        assert artifact_preview_url(None) is None

    def test_lineage_node_metadata(self, client: TestClient) -> None:
        res = client.get(f"/runs/{RUN_ID}/artifact-lineage")
        assert res.status_code == 200
        artifact_nodes = [n for n in res.json()["nodes"] if n["entity_type"] == "artifact"]
        assert len(artifact_nodes) == 1
        meta = artifact_nodes[0]["metadata"]
        assert meta["uri"] == f"memory://{RUN_ID}/writer/0/0"
        assert meta["uri_scheme"] == "memory"
        assert meta["preview_url"] is None

    def test_lineage_node_http_uri_direct_preview(self, client: TestClient, runtime: RuntimeService) -> None:
        runtime._runs["run-evidence-http"] = _make_run(
            run_id="run-evidence-http",
            artifact_uri="https://cdn.example.com/reports/final.md",
        )
        res = client.get("/runs/run-evidence-http/artifact-lineage")
        assert res.status_code == 200
        artifact_nodes = [n for n in res.json()["nodes"] if n["entity_type"] == "artifact"]
        meta = artifact_nodes[0]["metadata"]
        assert meta["uri_scheme"] == "https"
        assert meta["preview_url"] == "https://cdn.example.com/reports/final.md"
