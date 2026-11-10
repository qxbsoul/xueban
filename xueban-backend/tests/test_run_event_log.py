"""P1 durable run state tests: append-only event log, SQLite run/request
snapshots, SQLite checkpoint store, restart resilience, and live loop halt
(E2). All deterministic — no LLM, no network."""

from __future__ import annotations

import asyncio

import pytest

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.checkpoint_store import SqliteCheckpointStore
from xueban.runtime.contracts import CheckpointRef, CheckpointState, WritebackRef
from xueban.runtime.run_event_log import (
    InMemoryRunEventLog,
    RunEventRecord,
    SqliteRunEventLog,
    SqliteRunStore,
    SqliteRequestContextStore,
    replay_run_summary,
)


def _simple_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "evt-demo",
            "version": "0.1",
            "name": "Event Demo",
            "entrypoint": "a",
            "nodes": [
                {"id": "a", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "A", "message_template": "[a]"}},
                {"id": "b", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "B", "message_template": "[b]"}},
            ],
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "END", "type": "final"},
            ],
        }
    )


def _loop_workflow(budget: int = 3) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "evt-loop",
            "version": "0.1",
            "name": "Event Loop",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {
                     "kind": "fixed_count",
                     "iteration_budget": budget,
                     "subgraph": ["do"],
                     "setpoint": {"metric": "score", "op": ">=", "value": 99},
                 }},
                {"id": "do", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "Do", "message_template": "[do]",
                            "emit": {"score": 1}}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


# ---------------------------------------------------------------------------
# Event log backends
# ---------------------------------------------------------------------------


def test_in_memory_event_log_sequencing():
    log = InMemoryRunEventLog()
    log.append("r1", "run.started", payload={"workflow_id": "w"})
    log.append("r1", "node.completed", node_id="a")
    log.append("r2", "run.started")

    r1 = log.iter_events("r1")
    assert [e.seq for e in r1] == [1, 2]
    assert [e.event_type for e in r1] == ["run.started", "node.completed"]
    assert log.latest_seq("r1") == 2
    assert len(log.iter_events("r2")) == 1
    assert log.iter_events("missing") == []


def test_sqlite_event_log_survives_reopen(tmp_path):
    db = tmp_path / "runtime.sqlite"
    log = SqliteRunEventLog(db)
    log.append("r1", "run.started", payload={"workflow_id": "w", "nested": {"x": 1}})
    log.append("r1", "loop.iteration", node_id="loop", iteration_index=0, payload={"converged": False})
    log.close()

    reopened = SqliteRunEventLog(db)
    events = reopened.iter_events("r1")
    assert [e.event_type for e in events] == ["run.started", "loop.iteration"]
    assert events[1].node_id == "loop"
    assert events[0].payload["nested"] == {"x": 1}
    reopened.close()


def test_replay_run_summary_derives_status_and_loops():
    events = [
        RunEventRecord(run_id="r1", seq=1, event_type="run.started", payload={"workflow_id": "w"}),
        RunEventRecord(run_id="r1", seq=2, event_type="node.completed", node_id="a"),
        RunEventRecord(run_id="r1", seq=3, event_type="loop.iteration",
                       payload={"loop_id": "L1", "converged": False}),
        RunEventRecord(run_id="r1", seq=4, event_type="loop.iteration",
                       payload={"loop_id": "L1", "converged": True}),
        RunEventRecord(run_id="r1", seq=5, event_type="loop.completed",
                       payload={"loop_id": "L1", "status": "converged"}),
        RunEventRecord(run_id="r1", seq=6, event_type="run.completed"),
    ]
    summary = replay_run_summary(events)
    assert summary["status"] == "succeeded"
    assert summary["nodes_executed"] == ["a"]
    assert summary["loops"]["L1"] == {"iterations": 2, "last_converged": True, "status": "converged"}


# ---------------------------------------------------------------------------
# RuntimeService emission + restart resilience
# ---------------------------------------------------------------------------


def test_service_emits_durable_lifecycle_events(tmp_path):
    db = tmp_path / "runtime.sqlite"
    log = SqliteRunEventLog(db)
    service = RuntimeService(run_event_log=log)
    result = asyncio.run(service.run(RuntimeRequest(workflow=_simple_workflow(), input={})))

    types = [e.event_type for e in log.iter_events(result.run.run_id)]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert types.count("node.started") == 2
    assert types.count("node.completed") == 2
    log.close()


def test_loop_events_emitted(tmp_path):
    log = InMemoryRunEventLog()
    service = RuntimeService(run_event_log=log)
    result = asyncio.run(service.run(RuntimeRequest(workflow=_loop_workflow(budget=2), input={})))

    types = [e.event_type for e in log.iter_events(result.run.run_id)]
    assert types.count("loop.iteration") == 2
    assert "loop.completed" in types
    loop_done = next(e for e in log.iter_events(result.run.run_id) if e.event_type == "loop.completed")
    assert loop_done.payload["status"] == "converged"  # fixed_count → converged
    assert loop_done.payload["iterations"] == 2


def test_restart_recovers_run_and_request_context_from_sqlite(tmp_path):
    db = tmp_path / "runtime.sqlite"
    run_store = SqliteRunStore(db)
    ctx_store = SqliteRequestContextStore(db)
    service = RuntimeService(run_store=run_store, request_context_store=ctx_store)
    result = asyncio.run(service.run(RuntimeRequest(workflow=_simple_workflow(), input={})))
    run_store.close()
    ctx_store.close()

    # Simulated restart: fresh service over the same SQLite file.
    restarted = RuntimeService(
        run_store=SqliteRunStore(db),
        request_context_store=SqliteRequestContextStore(db),
    )
    recovered = restarted.get_run(result.run.run_id)
    assert recovered is not None
    assert recovered.run.status == "succeeded"
    assert [s.node_id for s in recovered.steps] == ["a", "b"]

    request = restarted.get_request_context(result.run.run_id)
    assert request is not None
    assert request.workflow.workflow_id == "evt-demo"


# ---------------------------------------------------------------------------
# SqliteCheckpointStore (P2/C2)
# ---------------------------------------------------------------------------


def _checkpoint(run_id: str = "r-cp") -> CheckpointRef:
    return CheckpointRef(
        checkpoint_id=f"ckpt-{run_id}",
        run_id=run_id,
        step_id="step-001",
        state_ref=f"checkpoint://{run_id}/step-001",
        state=CheckpointState(current_node_id="a", next_node_id="b"),
        writeback=WritebackRef(
            channel="checkpoint", target="host", mode="reference",
            host_action="persist_checkpoint_ref",
        ),
    )


def test_sqlite_checkpoint_store_roundtrip(tmp_path):
    store = SqliteCheckpointStore(str(tmp_path / "cp.sqlite"))
    record = store.put(_checkpoint())
    assert record.run_id == "r-cp"

    fetched = store.get("ckpt-r-cp")
    assert fetched is not None and fetched.state.current_node_id == "a"
    listed = store.list_run("r-cp")
    assert len(listed) == 1 and listed[0].checkpoint_id == "ckpt-r-cp"
    store.close()

    reopened = SqliteCheckpointStore(str(tmp_path / "cp.sqlite"))
    assert reopened.get("ckpt-r-cp") is not None
    reopened.close()


def test_sqlite_checkpoint_store_list_run_scoped(tmp_path):
    store = SqliteCheckpointStore(str(tmp_path / "cp.sqlite"))
    store.put(_checkpoint("r1"))
    store.put(_checkpoint("r2"))
    assert {r.run_id for r in store.list_run("r1")} == {"r1"}
    store.close()


# ---------------------------------------------------------------------------
# E2: live loop halt
# ---------------------------------------------------------------------------


def test_consume_loop_halt_semantics():
    service = RuntimeService()
    assert service._consume_loop_halt("r", "L1") is False

    service.request_loop_halt("r", "L1")
    assert service._consume_loop_halt("r", "L1") is True   # consumed once
    assert service._consume_loop_halt("r", "L1") is False  # then cleared

    service.request_loop_halt("r", "*")
    assert service._consume_loop_halt("r", "any-loop") is True  # wildcard


def test_wildcard_halt_stops_running_loop_at_boundary(tmp_path):
    """Request a run-wide halt as soon as run.started lands; the loop must
    stop at its first iteration boundary with loop_halted_by_request=True
    (or, if scheduling starved the requester, run the full budget — never
    anything in between)."""
    log = InMemoryRunEventLog()
    service = RuntimeService(run_event_log=log)

    async def scenario() -> dict:
        task = asyncio.create_task(
            service.run(RuntimeRequest(workflow=_loop_workflow(budget=50), input={}))
        )
        run_id = None
        for _ in range(2000):
            await asyncio.sleep(0)
            for rid in list(getattr(log, "_events", {})):
                if any(e.event_type == "run.started" for e in log.iter_events(rid)):
                    run_id = rid
                    break
            if run_id is not None:
                break
        assert run_id is not None, "run.started never observed"
        service.request_loop_halt(run_id, "*")
        result = await task
        return result.final_output

    out = asyncio.run(scenario())
    assert out["loop_halted_by_request"] is True or out["loop_iterations"] == 50
    if out["loop_halted_by_request"]:
        assert out["loop_status"] == "halted"
        assert any(
            e.event_type == "loop.halt_requested"
            for rid in list(log._events)
            for e in log.iter_events(rid)
        ), "halt request must be logged"


# ---------------------------------------------------------------------------
# P2: open executor kind registry
# ---------------------------------------------------------------------------


def test_register_executor_kind_opens_schema_validation():
    from xueban.runtime.contracts import EXECUTOR_KINDS, register_executor_kind

    register_executor_kind("custom_demo")
    try:
        wf = WorkflowDefinition.model_validate(
            {
                "workflow_id": "custom-kind",
                "version": "0.1",
                "name": "Custom Kind",
                "entrypoint": "x",
                "nodes": [
                    {"id": "x", "kind": "agent", "type": "agent.execute",
                     "config": {"executor": {"kind": "custom_demo"}}},
                ],
                "edges": [{"from": "x", "to": "END", "type": "final"}],
            }
        )
        assert wf.nodes[0].config["executor"]["kind"] == "custom_demo"
    finally:
        EXECUTOR_KINDS.discard("custom_demo")

    with pytest.raises(Exception):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "bad-kind",
                "version": "0.1",
                "name": "Bad Kind",
                "entrypoint": "x",
                "nodes": [
                    {"id": "x", "kind": "agent", "type": "agent.execute",
                     "config": {"executor": {"kind": "never_registered"}}},
                ],
                "edges": [{"from": "x", "to": "END", "type": "final"}],
            }
        )


def test_executor_registry_register_auto_opens_kind():
    from xueban.runtime.contracts import EXECUTOR_KINDS
    from xueban.runtime.executors import BaseExecutor, ExecutorRegistry

    class DemoExecutor(BaseExecutor):
        kind = "auto_registered_demo"

        async def execute(self, config, payload):
            return {"ok": True}

    registry = ExecutorRegistry(executors=[])
    try:
        registry.register(DemoExecutor())
        assert "auto_registered_demo" in EXECUTOR_KINDS
    finally:
        EXECUTOR_KINDS.discard("auto_registered_demo")
