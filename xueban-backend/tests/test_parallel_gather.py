"""control.parallel true-concurrency tests (GOAI M2).

Gather mode is OPT-IN (config.execution="gather"): every branch runs under
one asyncio.gather. The default stays interleaved scheduling so parallel
checkpoint/resume semantics are unchanged.
Uses an injectable sleep executor for the timing assertion — no network.
"""

from __future__ import annotations

import asyncio
import time

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.executors import BaseExecutor, ExecutorRegistry


class SleepExecutor(BaseExecutor):
    kind = "sleep_demo"

    async def execute(self, config, payload):
        await asyncio.sleep(float(config.get("duration", 0.05)))
        return {
            "message": f"slept {config.get('duration', 0.05)}s",
            "branch_result": str(config.get("label", "?")),
        }


def _parallel_workflow(execution: str | None = None) -> WorkflowDefinition:
    parallel_config: dict = {
        "branches": ["b1", "b2"],
        "barrier": "join",
    }
    if execution:
        parallel_config["execution"] = execution
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": f"parallel-{execution or 'default'}",
            "version": "0.1",
            "name": "Parallel Demo",
            "entrypoint": "fanout",
            "nodes": [
                {"id": "fanout", "kind": "node", "type": "control.parallel",
                 "config": parallel_config},
                {"id": "b1", "kind": "agent", "type": "agent.execute",
                 "config": {"executor": {"kind": "sleep_demo", "duration": 0.25, "label": "b1"}}},
                {"id": "b2", "kind": "agent", "type": "agent.execute",
                 "config": {"executor": {"kind": "sleep_demo", "duration": 0.25, "label": "b2"}}},
                {"id": "join", "kind": "node", "type": "control.barrier",
                 "config": {"source_parallel": "fanout"}},
            ],
            "edges": [{"from": "join", "to": "END", "type": "final"}],
        }
    )


def _service() -> RuntimeService:
    return RuntimeService(executor_registry=ExecutorRegistry(executors=[SleepExecutor()]))


def test_gather_runs_branches_concurrently_and_barrier_joins():
    service = _service()
    started = time.perf_counter()
    result = asyncio.run(service.run(RuntimeRequest(workflow=_parallel_workflow(execution="gather"), input={})))
    elapsed = time.perf_counter() - started

    assert result.run.status == "succeeded"
    # concurrency proof: two 0.25s branches finish well under the 0.5s a
    # serial schedule would need (margin for scheduler jitter).
    assert elapsed < 0.4, f"branches appear serialized: {elapsed:.3f}s"

    parallel_step = next(s for s in result.steps if s.node_id == "fanout")
    assert parallel_step.output["execution"] == "gather"
    assert parallel_step.output["failed_branches"] == []
    assert set(parallel_step.output["branch_outputs"]) == {"b1", "b2"}

    barrier_step = next(s for s in result.steps if s.node_id == "join")
    joined = barrier_step.output["branch_outputs"]
    assert {k: v["branch_result"] for k, v in joined.items()} == {"b1": "b1", "b2": "b2"}
    assert barrier_step.output["branch_count"] == 2


def test_default_mode_keeps_legacy_interleaved_scheduling():
    service = _service()
    result = asyncio.run(
        service.run(RuntimeRequest(workflow=_parallel_workflow(), input={}))
    )

    parallel_step = next(s for s in result.steps if s.node_id == "fanout")
    assert parallel_step.output.get("execution") != "gather"
    # legacy: branches execute as their own main-walk steps
    assert [s.node_id for s in result.steps] == ["fanout", "b1", "b2", "join"]


def test_gather_branch_failure_does_not_sink_siblings():
    class FailingExecutor(BaseExecutor):
        kind = "fail_demo"

        async def execute(self, config, payload):
            if config.get("fail"):
                raise RuntimeError("boom")
            return {"message": "ok", "branch_result": "fine"}

    service = RuntimeService(executor_registry=ExecutorRegistry(executors=[FailingExecutor()]))

    def _wf() -> WorkflowDefinition:
        return WorkflowDefinition.model_validate(
            {
                "workflow_id": "parallel-fail",
                "version": "0.1",
                "name": "Parallel Fail",
                "entrypoint": "fanout",
                "nodes": [
                    {"id": "fanout", "kind": "node", "type": "control.parallel",
                     "config": {"branches": ["bad", "good"], "barrier": "join", "execution": "gather"}},
                    {"id": "bad", "kind": "agent", "type": "agent.execute",
                     "config": {"executor": {"kind": "fail_demo", "fail": True}}},
                    {"id": "good", "kind": "agent", "type": "agent.execute",
                     "config": {"executor": {"kind": "fail_demo"}}},
                    {"id": "join", "kind": "node", "type": "control.barrier",
                     "config": {"source_parallel": "fanout"}},
                ],
                "edges": [{"from": "join", "to": "END", "type": "final"}],
            }
        )

    result = asyncio.run(service.run(RuntimeRequest(workflow=_wf(), input={})))
    parallel_step = next(s for s in result.steps if s.node_id == "fanout")
    assert parallel_step.output["failed_branches"] == ["bad"]
    assert "boom" in parallel_step.output["branch_outputs"]["bad"]["error"]
    assert parallel_step.output["branch_outputs"]["good"]["branch_result"] == "fine"
    assert result.run.status == "succeeded"
