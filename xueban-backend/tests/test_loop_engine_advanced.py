"""LE-1 resume + LE-2 fractal / depth-bound / watch-supervision tests.

Covers:
- mid-loop resume from persisted progress (LE-1 finish)
- PDCA fractal nesting (a control.loop inside a control.loop body) (LE-2)
- fractal depth bound (max_depth degrades to a single iteration) (LE-2)
- loops-watching-loops supervision via config.watch (LE-2)

Body nodes are deterministic so no LLM is required.
"""

from __future__ import annotations

import asyncio

import pytest

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.contracts import IterationRecord, TaskRecord
from xueban.runtime.executors import AgentTeamsExecutor


def _run(workflow: WorkflowDefinition, service: RuntimeService | None = None):
    return asyncio.run(
        (service or RuntimeService()).run(
            RuntimeRequest(workflow=workflow, input={"goal": "advanced loop test"})
        )
    )


# ---------- LE-1: mid-loop resume ----------


def test_mid_loop_resume_continues_from_saved_iteration():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "resume-loop",
            "version": "0.1",
            "name": "Resume Loop",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": 5,
                            "setpoint": {"metric": "score", "op": ">=", "value": 100},
                            "subgraph": ["p"]}},
                {"id": "p", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "p", "message_template": "[p]", "emit": {"score": 1}}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    service = RuntimeService()
    nodes_by_id = {n.id: n for n in workflow.nodes}
    loop_node = nodes_by_id["loop"]

    # Simulate a run interrupted after 2 iterations: pre-seed saved progress.
    seeded = [
        IterationRecord(loop_id="loop-prior", run_id="run-1", index=0, metrics={"score": 1.0}).model_dump(mode="json"),
        IterationRecord(loop_id="loop-prior", run_id="run-1", index=1, metrics={"score": 1.0}).model_dump(mode="json"),
    ]
    state = {"loop_progress": {"loop": {"next_iteration": 2, "iterations": seeded}}}

    out = asyncio.run(
        service._execute_loop_node(
            request=RuntimeRequest(workflow=workflow),
            run_id="run-1",
            task=TaskRecord(task_id="t1", run_id="run-1", root_task_id="t1"),
            step_id="step-001",
            node=loop_node,
            step_input={"goal": "resume"},
            state=state,
            nodes_by_id=nodes_by_id,
        )
    )

    # 2 restored + iterations 2,3,4 (effective_budget=5 at depth 0) = 5 total.
    assert out["loop_iterations"] == 5
    indices = [it["index"] for it in out["loop_record"]["iterations"]]
    assert indices == [0, 1, 2, 3, 4]
    # Progress cleared once the loop completes.
    assert "loop" not in state.get("loop_progress", {})


# ---------- LE-2: fractal nesting ----------


def test_fractal_nested_loop_executes_inner_loop_with_parent_link():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "fractal",
            "version": "0.1",
            "name": "Fractal",
            "entrypoint": "outer",
            "nodes": [
                {"id": "outer", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 2, "subgraph": ["p", "inner"]}},
                {"id": "p", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "p", "message_template": "[p]"}},
                {"id": "inner", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 2, "subgraph": ["c"]}},
                {"id": "c", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "c", "message_template": "[c]"}},
            ],
            "edges": [{"from": "outer", "to": "END", "type": "final"}],
        }
    )
    result = _run(workflow)
    outer = result.final_output

    assert outer["loop_iterations"] == 2  # outer ran its 2 iterations
    assert outer["loop_depth"] == 0

    # The inner loop is the last body node, so its output is captured in iteration evidence.
    first_iter_evidence = outer["loop_record"]["iterations"][0]["evidence"]
    inner_output = first_iter_evidence["last_output"]
    assert inner_output["node_type"] == "control.loop"
    assert inner_output["loop_depth"] == 1  # nested one level deeper
    # depth_factor=0.6 shrinks inner effective budget: int(2 * 0.6^1) = 1
    assert inner_output["loop_iterations"] == 1
    # parent_loop_id links the inner loop back to the outer loop id.
    assert inner_output["loop_record"]["parent_loop_id"] == outer["loop_id"]


# ---------- LE-2: fractal depth bound ----------


def test_depth_bound_degrades_loop_to_single_iteration():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "depth-bound",
            "version": "0.1",
            "name": "Depth Bound",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 5, "max_depth": 0, "subgraph": ["p"]}},
                {"id": "p", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "p", "message_template": "[p]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    result = _run(workflow)
    # current_depth(0) >= max_depth(0) -> effective_budget degrades to 1.
    assert result.final_output["loop_iterations"] == 1


# ---------- LE-2: loops watching loops ----------


def test_watch_supervision_halts_loop_when_watched_loop_signaled():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "watch",
            "version": "0.1",
            "name": "Watch",
            "entrypoint": "loop_a",
            "nodes": [
                {"id": "loop_a", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": 2,
                            "setpoint": {"metric": "score", "op": ">=", "value": 100},
                            "subgraph": ["a"]}},
                {"id": "a", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "a", "message_template": "[a]", "emit": {"score": 1}}},
                {"id": "loop_b", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 3, "subgraph": ["b"],
                            "watch": [{"loop": "loop_a", "signal": "halted"}]}},
                {"id": "b", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "b", "message_template": "[b]"}},
            ],
            "edges": [
                {"from": "loop_a", "to": "loop_b"},
                {"from": "loop_b", "to": "END", "type": "final"},
            ],
        }
    )
    result = _run(workflow)

    assert [step.node_id for step in result.steps] == ["loop_a", "loop_b"]
    loop_a_out = result.steps[0].output
    loop_b_out = result.steps[1].output

    # loop_a never converges (score=1 < 100) -> status halted, publishes "halted" signal.
    assert loop_a_out["loop_status"] == "halted"
    # loop_b watches loop_a:halted -> halts before running any iteration.
    assert loop_b_out["loop_halted_by_watch"] is True
    assert loop_b_out["loop_iterations"] == 0
    assert loop_b_out["loop_status"] == "halted"


def test_watch_does_not_trigger_when_signal_mismatched():
    # Same shape but loop_b watches for a "converged" signal that loop_a never emits.
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "watch-mismatch",
            "version": "0.1",
            "name": "Watch Mismatch",
            "entrypoint": "loop_a",
            "nodes": [
                {"id": "loop_a", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": 2,
                            "setpoint": {"metric": "score", "op": ">=", "value": 100},
                            "subgraph": ["a"]}},
                {"id": "a", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "a", "message_template": "[a]", "emit": {"score": 1}}},
                {"id": "loop_b", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 3, "subgraph": ["b"],
                            "watch": [{"loop": "loop_a", "signal": "converged"}]}},
                {"id": "b", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "b", "message_template": "[b]"}},
            ],
            "edges": [
                {"from": "loop_a", "to": "loop_b"},
                {"from": "loop_b", "to": "END", "type": "final"},
            ],
        }
    )
    result = _run(workflow)
    loop_b_out = result.steps[1].output
    # loop_a emitted "halted", not "converged" -> watch never triggers -> loop_b runs fully.
    assert loop_b_out["loop_halted_by_watch"] is False
    assert loop_b_out["loop_iterations"] == 3
def test_agentteams_executor_dry_run_in_workflow():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "agentteams-dry",
            "version": "0.1",
            "name": "AgentTeams Dry",
            "entrypoint": "team",
            "nodes": [
                {"id": "team", "kind": "agent", "type": "agentteams.delegate",
                 "config": {"role": "team", "message_template": "[team]",
                            "executor": {"kind": "agentteams", "provider": "hermes", "dry_run": True,
                                         "workers": ["bob"], "skills": ["code"]}}},
            ],
            "edges": [{"from": "team", "to": "END", "type": "final"}],
        }
    )
    result = _run(workflow)
    out = result.final_output
    assert result.run.status == "succeeded"
    assert out["handled_by"] == "agentteams:hermes"
    assert out["dry_run"] is True
    assert out["team_spec"]["workers"] == ["bob"]
    assert out["matrix_room"].startswith("!dryrun-hermes:")


def test_agentteams_executor_real_dispatch_requires_matrix_env():
    import asyncio
    # No MATRIX_HOMESERVER configured -> real dispatch fails fast (env-gated).
    with pytest.raises(RuntimeError, match="MATRIX_HOMESERVER"):
        asyncio.run(AgentTeamsExecutor().execute({"provider": "hermes", "dry_run": False}, {"prompt": "x"}))
