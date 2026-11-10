"""SEED-lite tests: the A (Controller) learns from River Memory across runs (design §7.4 lite)."""

from __future__ import annotations

import asyncio

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.river_memory import RiverMemory


def _seed_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "seed",
            "version": "0.1",
            "name": "SEED-lite",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop", "config": {
                    "kind": "until_converge", "iteration_budget": 20,
                    "setpoint": {"metric": "effort", "op": ">=", "value": 100},
                    "controller": {"type": "memory_nudge", "field": "effort", "rate": 25,
                                   "init": {"effort": 0}, "warm_start_factor": 0.8},
                    "memory": {"river": True, "recall": 3},
                    "subgraph": ["pt"]}},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def test_second_run_converges_faster_by_learning_from_memory():
    river = RiverMemory()
    svc = RuntimeService(river_memory=river)
    wf = _seed_workflow()

    # Run 1: cold start — effort ramps 0 -> 25 -> 50 -> 75 -> 100 (5 iterations).
    r1 = asyncio.run(svc.run(RuntimeRequest(workflow=wf, input={"goal": "g1"})))
    assert r1.final_output["loop_iterations"] == 5
    assert r1.final_output["loop_converged"] is True

    # Run 2: dredges run 1's settled pattern (effort=100) and warm-starts at 80 (0.8*100).
    r2 = asyncio.run(svc.run(RuntimeRequest(workflow=wf, input={"goal": "g2"})))
    first_effort = r2.final_output["loop_record"]["iterations"][0]["evidence"]["controlled"]["effort"]
    assert first_effort == 80  # warm-started from learned experience
    # 80 -> (nudge) 105 -> converged: 2 iterations vs the cold-start 5.
    assert r2.final_output["loop_iterations"] == 2
    assert r2.final_output["loop_converged"] is True


def test_memory_nudge_without_river_behaves_like_cold_start():
    # No memory.river -> nothing to dredge -> behaves as a plain setpoint_nudge.
    wf = WorkflowDefinition.model_validate(
        {
            "workflow_id": "seed-noriver", "version": "0.1", "name": "NoRiver", "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop", "config": {
                    "kind": "until_converge", "iteration_budget": 20,
                    "setpoint": {"metric": "effort", "op": ">=", "value": 100},
                    "controller": {"type": "memory_nudge", "field": "effort", "rate": 25, "init": {"effort": 0}},
                    "subgraph": ["pt"]}},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    r = asyncio.run(RuntimeService().run(RuntimeRequest(workflow=wf, input={"goal": "g"})))
    # cold start: effort begins at 0 -> 5 iterations.
    first_effort = r.final_output["loop_record"]["iterations"][0]["evidence"]["controlled"]["effort"]
    assert first_effort == 0
    assert r.final_output["loop_iterations"] == 5
