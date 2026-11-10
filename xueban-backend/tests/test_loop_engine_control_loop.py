"""LE-1 control.loop node tests.

Validates the runtime control.loop node: in-run iteration, convergence break,
node.retried emission, LoopFrame scope isolation, and WorkflowDefinition
validation. Body nodes are deterministic (emit/message_template) so no LLM is
required. See docs/architecture/loop-engine-design.md §5.4.
"""

from __future__ import annotations

import asyncio

import pytest

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.events import NODE_RETRIED, RunEventBus


def _run(workflow: WorkflowDefinition, service: RuntimeService):
    return asyncio.run(
        service.run(RuntimeRequest(workflow=workflow, input={"goal": "loop test"}))
    )


def _loop_workflow(*, kind: str, budget: int, setpoint: dict | None = None, body: list[str], emits: dict | None = None) -> WorkflowDefinition:
    nodes = [
        {
            "id": "loop",
            "kind": "node",
            "type": "control.loop",
            "config": {
                "kind": kind,
                "iteration_budget": budget,
                "subgraph": body,
                **({"setpoint": setpoint} if setpoint else {}),
            },
        }
    ]
    for body_id in body:
        nodes.append(
            {
                "id": body_id,
                "kind": "agent",
                "type": "agent.execute",
                "config": {
                    "role": body_id,
                    "message_template": f"[{body_id}] step",
                    **({"emit": emits} if body_id == body[-1] and emits else {}),
                },
            }
        )
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": f"loop-{kind}",
            "version": "0.1",
            "name": f"Loop {kind}",
            "entrypoint": "loop",
            "nodes": nodes,
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def test_fixed_count_runs_all_iterations():
    wf = _loop_workflow(kind="fixed_count", budget=3, body=["p", "d"])
    result = _run(wf, RuntimeService())

    assert [step.node_id for step in result.steps] == ["loop"]
    out = result.final_output
    assert out["loop_kind"] == "fixed_count"
    assert out["loop_iterations"] == 3
    assert out["loop_converged"] is True
    assert out["loop_retried_count"] == 2  # retries after iter 0 and 1, not the last
    assert len(out["loop_record"]["iterations"]) == 3


def test_until_converge_breaks_early_when_setpoint_met():
    wf = _loop_workflow(
        kind="until_converge",
        budget=5,
        setpoint={"metric": "score", "op": ">=", "value": 5},
        body=["p", "c"],
        emits={"score": 7},  # c (last body node) emits a passing score
    )
    result = _run(wf, RuntimeService())

    out = result.final_output
    assert out["loop_iterations"] == 1  # converged on first iteration
    assert out["loop_converged"] is True
    assert out["loop_retried_count"] == 0  # no retry: converged before re-entry


def test_until_converge_runs_full_budget_when_not_met():
    wf = _loop_workflow(
        kind="until_converge",
        budget=3,
        setpoint={"metric": "score", "op": ">=", "value": 100},
        body=["p", "c"],
        emits={"score": 7},  # never reaches 100
    )
    result = _run(wf, RuntimeService())

    out = result.final_output
    assert out["loop_iterations"] == 3
    assert out["loop_converged"] is False
    assert out["loop_retried_count"] == 2
    assert out["loop_record"]["status"] == "halted"


def test_node_retried_events_published():
    bus = RunEventBus()
    service = RuntimeService(event_bus=bus)
    wf = _loop_workflow(kind="fixed_count", budget=3, body=["p", "d"])
    result = _run(wf, service)

    retried = [
        evt
        for _seq, evt in bus.get_events(result.run.run_id)
        if evt.get("type") == NODE_RETRIED
    ]
    assert len(retried) == 2
    assert all(evt["node_id"] == "loop" for evt in retried)
    assert [evt["iteration"] for evt in retried] == [0, 1]


def test_loop_body_isolated_from_run_visited_nodes():
    wf = _loop_workflow(kind="fixed_count", budget=3, body=["p", "d"])
    result = _run(wf, RuntimeService())

    # The loop node is the only main-walk step; body nodes are NOT run-level visited.
    assert result.checkpoints, "expected at least one checkpoint"
    run_visited = result.checkpoints[0].state.visited_nodes
    assert run_visited == ["loop"]
    assert "p" not in run_visited and "d" not in run_visited

    # Body nodes are tracked per-iteration in the loop-local visited log.
    local = result.final_output["loop_local_visited"]
    assert local == ["p", "d", "p", "d", "p", "d"]


def test_loop_iterations_do_not_exhaust_max_steps():
    # budget=6 with a 3-node workflow would blow past max_steps=nodes*3=9 if iterations
    # counted as main-walk steps. They must not.
    wf = _loop_workflow(kind="fixed_count", budget=6, body=["p", "d"])
    result = _run(wf, RuntimeService())
    assert result.run.status == "succeeded"
    assert len(result.steps) == 1
    assert result.final_output["loop_iterations"] == 6


# ---------- validation ----------


def test_validation_rejects_missing_subgraph():
    with pytest.raises(ValueError, match="non-empty config.subgraph"):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "bad-loop",
                "version": "0.1",
                "name": "Bad Loop",
                "entrypoint": "loop",
                "nodes": [
                    {"id": "loop", "kind": "node", "type": "control.loop",
                     "config": {"kind": "fixed_count", "iteration_budget": 2}},
                    {"id": "p", "kind": "agent", "type": "agent.execute",
                     "config": {"role": "p", "message_template": "p"}},
                ],
                "edges": [{"from": "loop", "to": "END", "type": "final"}],
            }
        )


def test_validation_rejects_bad_kind():
    with pytest.raises(ValueError, match="config.kind must be one of"):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "bad-kind",
                "version": "0.1",
                "name": "Bad Kind",
                "entrypoint": "loop",
                "nodes": [
                    {"id": "loop", "kind": "node", "type": "control.loop",
                     "config": {"kind": "forever", "iteration_budget": 2, "subgraph": ["p"]}},
                    {"id": "p", "kind": "agent", "type": "agent.execute",
                     "config": {"role": "p", "message_template": "p"}},
                ],
                "edges": [{"from": "loop", "to": "END", "type": "final"}],
            }
        )


def test_validation_rejects_nonpositive_budget():
    with pytest.raises(ValueError, match="iteration_budget must be a positive integer"):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "bad-budget",
                "version": "0.1",
                "name": "Bad Budget",
                "entrypoint": "loop",
                "nodes": [
                    {"id": "loop", "kind": "node", "type": "control.loop",
                     "config": {"kind": "fixed_count", "iteration_budget": 0, "subgraph": ["p"]}},
                    {"id": "p", "kind": "agent", "type": "agent.execute",
                     "config": {"role": "p", "message_template": "p"}},
                ],
                "edges": [{"from": "loop", "to": "END", "type": "final"}],
            }
        )
