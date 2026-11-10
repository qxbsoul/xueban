"""Adoption-item tests: #1 diagnostics sensor, #2 ACP provenance meta,
#3 permission tri-state, #4 task ledger events.

All deterministic — approvals are resolved by directly poking the same
registry the /api/approvals endpoints drive.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.executors import BaseExecutor, ExecutorRegistry
from xueban.runtime.permissions import (
    PermissionBlocked,
    PermissionPolicy,
    parse_permission_policy,
)
from xueban.runtime.run_event_log import InMemoryRunEventLog, replay_run_summary


class OkExecutor(BaseExecutor):
    kind = "ok_demo"

    async def execute(self, config, payload):
        return {"message": "ok", "ran": True}


def _wf(node_config: dict, defaults: dict | None = None) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "perm-demo",
            "version": "0.1",
            "name": "Perm Demo",
            "entrypoint": "n",
            "nodes": [
                {"id": "n", "kind": "agent", "type": "agent.execute", "config": node_config},
            ],
            "edges": [{"from": "n", "to": "END", "type": "final"}],
            **({"defaults": defaults} if defaults else {}),
        }
    )


def _service(log=None):
    return RuntimeService(
        executor_registry=ExecutorRegistry(executors=[OkExecutor()]),
        run_event_log=log,
    )


# ---------------------------------------------------------------------------
# #3 permission tri-state
# ---------------------------------------------------------------------------


def test_permission_auto_allows_and_logs():
    log = InMemoryRunEventLog()
    service = _service(log)
    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=_wf({"executor": {"kind": "ok_demo"},
                              "permissions": {"default": "auto"}}),
                input={},
            )
        )
    )
    assert result.run.status == "succeeded"
    assert result.steps[0].output["ran"] is True
    assert result.steps[0].output["permission"]["decision"] == "allowed"
    checked = [e for e in log.iter_events(result.run.run_id) if e.event_type == "permission.checked"]
    assert checked and checked[0].payload["mode"] == "auto"


def test_permission_block_refuses_before_dispatch():
    service = _service()
    with pytest.raises(PermissionBlocked, match=r"\(block\)"):
        asyncio.run(
            service.run(
                RuntimeRequest(
                    workflow=_wf({"executor": {"kind": "ok_demo"},
                                  "permissions": {"rules": {"dispatch": "block"}}}),
                    input={},
                )
            )
        )


def test_permission_prompt_waits_for_approval():
    service = _service()

    async def scenario():
        task = asyncio.create_task(
            service.run(
                RuntimeRequest(
                    workflow=_wf({"executor": {"kind": "ok_demo"},
                                  "permissions": {"rules": {"dispatch": "prompt"},
                                                  "timeout_seconds": 5}}),
                    input={},
                )
            )
        )
        # Approve as soon as the approval registry has a pending entry — the
        # same state /api/approvals/{id}/approve drives.
        for _ in range(2000):
            await asyncio.sleep(0)
            for key, evt in list(service._approval_events.items()):
                if not evt.is_set():
                    service._approval_decisions[key] = {"decision": "approve", "reason": ""}
                    evt.set()
            if task.done():
                break
        result = await task
        return result

    result = asyncio.run(scenario())
    assert result.run.status == "succeeded"
    assert result.steps[0].output["permission"] == {
        "action": "dispatch", "mode": "prompt", "decision": "approved",
    }


def test_permission_prompt_rejection_blocks():
    service = _service()

    async def scenario():
        task = asyncio.create_task(
            service.run(
                RuntimeRequest(
                    workflow=_wf({"executor": {"kind": "ok_demo"},
                                  "permissions": {"rules": {"dispatch": "prompt"},
                                                  "timeout_seconds": 5}}),
                    input={},
                )
            )
        )
        for _ in range(2000):
            await asyncio.sleep(0)
            for key, evt in list(service._approval_events.items()):
                if not evt.is_set():
                    service._approval_decisions[key] = {"decision": "reject", "reason": "no"}
                    evt.set()
            if task.done():
                break
        return await task

    with pytest.raises(PermissionBlocked, match="prompt-reject"):
        asyncio.run(scenario())


def test_workflow_level_permission_default_applies():
    service = _service()
    with pytest.raises(PermissionBlocked):
        asyncio.run(
            service.run(
                RuntimeRequest(
                    workflow=_wf(
                        {"executor": {"kind": "ok_demo"}},
                        defaults={"permissions": {"rules": {"dispatch": "block"}}},
                    ),
                    input={},
                )
            )
        )


def test_node_policy_overrides_workflow_default():
    service = _service()
    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=_wf(
                    {"executor": {"kind": "ok_demo"},
                     "permissions": {"rules": {"dispatch": "auto"}}},
                    defaults={"permissions": {"rules": {"dispatch": "block"}}},
                ),
                input={},
            )
        )
    )
    assert result.run.status == "succeeded"


def test_parse_permission_policy_garbage_safe():
    assert parse_permission_policy(None) is None
    assert parse_permission_policy("x") is None
    assert parse_permission_policy({"rules": {"dispatch": "nonsense"}}) is None
    assert PermissionPolicy().resolve("dispatch") == "auto"


# ---------------------------------------------------------------------------
# #1 diagnostics sensor
# ---------------------------------------------------------------------------


def _diag_workflow(diags: list, sensor_cfg: dict, budget: int = 3) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "diag-demo",
            "version": "0.1",
            "name": "Diag Demo",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": budget,
                            "subgraph": ["lint", "check"]}},
                {"id": "lint", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "Linter", "message_template": "[lint]",
                            "emit": {"diagnostics": diags}}},
                {"id": "check", "kind": "node", "type": "control.sensor",
                 "config": sensor_cfg},
            ],
            "edges": [
                {"from": "lint", "to": "check"},
                {"from": "loop", "to": "END", "type": "final"},
            ],
        }
    )


def test_diagnostics_sensor_converges_when_clean():
    wf = _diag_workflow(
        diags=[{"severity": "warning", "message": "unused var"}],
        sensor_cfg={"type": "diagnostics", "value": 1},
    )
    result = asyncio.run(RuntimeService().run(RuntimeRequest(workflow=wf, input={})))
    out = result.final_output
    assert out["loop_iterations"] == 1
    assert out["loop_converged"] is True
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["sensor"]["sensor_type"] == "diagnostics"
    assert evidence["sensor"]["value"] == 1.0  # one warning <= target 1


def test_diagnostics_sensor_counts_only_errors_when_included():
    wf = _diag_workflow(
        diags=[
            {"severity": "warning", "message": "w1"},
            {"severity": "warning", "message": "w2"},
            {"severity": "error", "message": "e1"},
        ],
        sensor_cfg={"type": "diagnostics", "include": "errors", "value": 0},
    )
    result = asyncio.run(RuntimeService().run(RuntimeRequest(workflow=wf, input={})))
    evidence = result.final_output["loop_record"]["iterations"][0]["evidence"]
    assert evidence["sensor"]["issue_count"] == 1  # only the error counts


def test_diagnostics_sensor_runs_budget_when_dirty():
    wf = _diag_workflow(
        diags=[{"severity": "error", "message": "e"}],
        sensor_cfg={"type": "diagnostics", "value": 0},
        budget=2,
    )
    result = asyncio.run(RuntimeService().run(RuntimeRequest(workflow=wf, input={})))
    out = result.final_output
    assert out["loop_iterations"] == 2
    assert out["loop_converged"] is False


# ---------------------------------------------------------------------------
# #4 task ledger events
# ---------------------------------------------------------------------------


def test_task_ledger_events_and_replay():
    log = InMemoryRunEventLog()
    service = RuntimeService(run_event_log=log)
    wf = WorkflowDefinition.model_validate(
        {
            "workflow_id": "ledger-demo", "version": "0.1", "name": "Ledger",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": 3,
                            "subgraph": ["do"],
                            "setpoint": {"metric": "score", "op": ">=", "value": 5}}},
                {"id": "do", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "Do", "message_template": "[do]",
                            "emit": {"score": 7}}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    result = asyncio.run(service.run(RuntimeRequest(workflow=wf, input={})))

    ledger = [e for e in log.iter_events(result.run.run_id) if e.event_type == "task.ledger"]
    assert len(ledger) == 1  # converged on iteration 0
    assert ledger[0].payload["done"] is True
    assert ledger[0].payload["goal"] == "score"
    assert ledger[0].payload["current"] == 7.0

    from xueban.runtime.run_event_log import RunEventRecord

    summary = replay_run_summary([RunEventRecord.model_validate(e.model_dump()) for e in log.iter_events(result.run.run_id)])
    assert summary["ledger"] == [
        {"loop_id": ledger[0].payload["loop_id"], "iteration": 0, "done": True, "goal": "score"}
    ]


# ---------------------------------------------------------------------------
# #2 ACP provenance meta
# ---------------------------------------------------------------------------


def test_new_session_request_carries_loop_provenance():
    from xueban.runtime.acp.messages import NewSessionRequest

    msg = NewSessionRequest(
        run_id="run-x",
        node_id="do",
        agent_id="agent-y",
        provenance={
            "run_id": "run-x",
            "node_id": "do",
            "loop": {
                "loop_id": "loop-1",
                "iteration": 2,
                "depth": 1,
                "parent_loop_id": "loop-0",
            },
        },
    )
    body = msg.model_dump(exclude_none=True)
    meta = body["params"]["_meta"]["xueban"]
    assert meta["runId"] == "run-x"
    assert meta["loopId"] == "loop-1"
    assert meta["iteration"] == 2
    assert meta["loopDepth"] == 1
    assert meta["parentLoopId"] == "loop-0"


def test_new_session_request_meta_without_provenance_unchanged():
    from xueban.runtime.acp.messages import NewSessionRequest

    body = NewSessionRequest(run_id="r", node_id="n", agent_id="a").model_dump(exclude_none=True)
    meta = body["params"]["_meta"]["xueban"]
    assert set(meta) == {"runId", "nodeId", "agentId"}
