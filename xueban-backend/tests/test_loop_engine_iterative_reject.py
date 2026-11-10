"""Iterative Policy reject tests (design §6.4): loop-granularity reject + resume."""

from __future__ import annotations

import asyncio

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.contracts import ResumeRequest


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "iter-reject",
            "version": "0.1",
            "name": "Iter Reject",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 3, "subgraph": ["p"]}},
                {"id": "p", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "p", "message_template": "[p]"}},
                {"id": "after", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "after", "message_template": "[after]"}},
            ],
            "edges": [
                {"from": "loop", "to": "after"},
                {"from": "after", "to": "END", "type": "final"},
            ],
        }
    )


def _loop_step(result):
    return next(s for s in result.steps if s.node_id == "loop")


def test_reject_loop_iteration_seeds_checkpoint_with_progress():
    svc = RuntimeService()
    result = asyncio.run(svc.run(RuntimeRequest(workflow=_workflow(), input={"goal": "g"})))
    run_id = result.run.run_id
    original_iter0_id = _loop_step(result).output["loop_record"]["iterations"][0]["iteration_id"]

    cp_id = asyncio.run(svc.reject_loop_iteration(run_id, "reviewer", "loop", target_iteration=1, reason="redo"))

    snapshot = svc._checkpoints[cp_id]
    shared = snapshot.state.state["shared_state"]
    progress = shared["loop_progress"]["loop"]
    assert progress["next_iteration"] == 1
    assert len(progress["iterations"]) == 1  # only iteration 0 restored
    assert set(shared["invalidated_nodes"]) >= {"loop", "after"}
    assert snapshot.state.next_node_id == "loop"
    return svc, run_id, cp_id, original_iter0_id


def test_resume_after_iterative_reject_redoes_loop_from_target_iteration():
    svc = RuntimeService()
    result = asyncio.run(svc.run(RuntimeRequest(workflow=_workflow(), input={"goal": "g"})))
    run_id = result.run.run_id
    original_iter0_id = _loop_step(result).output["loop_record"]["iterations"][0]["iteration_id"]

    cp_id = asyncio.run(svc.reject_loop_iteration(run_id, "reviewer", "loop", target_iteration=1))

    resumed = asyncio.run(svc.resume(run_id, ResumeRequest(checkpoint_id=cp_id)))
    assert resumed.run.status == "succeeded"

    loop_out = _loop_step(resumed).output
    iterations = loop_out["loop_record"]["iterations"]
    # iteration 0 is the RESTORED one (same iteration_id as original); 1 and 2 are fresh.
    assert iterations[0]["iteration_id"] == original_iter0_id
    assert iterations[1]["iteration_id"] != original_iter0_id
    assert len(iterations) == 3


def test_reject_loop_iteration_validates_target_range():
    svc = RuntimeService()
    result = asyncio.run(svc.run(RuntimeRequest(workflow=_workflow(), input={"goal": "g"})))
    run_id = result.run.run_id
    # the loop ran 3 iterations; target 99 is out of range
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        asyncio.run(svc.reject_loop_iteration(run_id, "reviewer", "loop", target_iteration=99))
