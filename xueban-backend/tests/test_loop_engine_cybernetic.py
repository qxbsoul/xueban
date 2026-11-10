"""LE-2 cybernetic core tests: Sensor (C) + Controller (A) closed feedback loop.

These verify the loop is a genuine feedback controller, not just an iterator:
- the Controller (setpoint_nudge) actuates a variable each non-converged iteration
- the Sensor observes the response (body output merged with the actuated variable)
- the loop converges BECAUSE the controller drives the variable toward the setpoint
- trajectory sensor classifies the metric-series shape (causal-attribution method ②)

Body nodes are deterministic pass-throughs; the controller is the source of change,
which is exactly the C→A feedback path closing.
"""

from __future__ import annotations

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition


def _run(workflow: WorkflowDefinition):
    return RuntimeService().run(RuntimeRequest(workflow=workflow, input={"goal": "cybernetic"}))


def _closed_loop_workflow(*, sensor_type: str | None = None) -> WorkflowDefinition:
    config: dict = {
        "kind": "until_converge",
        "iteration_budget": 20,
        "setpoint": {"metric": "effort", "op": ">=", "value": 100},
        "controller": {"type": "setpoint_nudge", "field": "effort", "rate": 25, "init": {"effort": 0}},
        "subgraph": ["pt"],
    }
    if sensor_type:
        config["sensor"] = {"type": sensor_type}
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "cybernetic",
            "version": "0.1",
            "name": "Cybernetic",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop", "config": config},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def test_controller_closes_feedback_loop_and_drives_convergence():
    import asyncio
    result = asyncio.run(_run(_closed_loop_workflow()))

    out = result.final_output
    assert out["loop_converged"] is True
    # effort ramps 0 -> 25 -> 50 -> 75 -> 100 (converges on the 5th iteration)
    assert out["loop_iterations"] == 5
    efforts = [it["evidence"]["controlled"]["effort"] for it in out["loop_record"]["iterations"]]
    assert efforts == [0, 25, 50, 75, 100]


def test_trajectory_sensor_classifies_monotone_convergence():
    import asyncio
    result = asyncio.run(_run(_closed_loop_workflow(sensor_type="trajectory")))

    iterations = result.final_output["loop_record"]["iterations"]
    # iter 0 has too little history; iters 1-3 are monotone toward the >= target -> "converging"
    classifications = [it["evidence"]["sensor"]["classification"] for it in iterations]
    assert classifications[0] == "insufficient"
    assert classifications[1] == "converging"
    assert classifications[3] == "converging"
    assert classifications[-1] == "converged"
    assert all(it["evidence"]["sensor"]["sensor_type"] == "trajectory" for it in iterations)


def test_open_loop_without_controller_does_not_converge_on_unreachable_setpoint():
    # Same setpoint, NO controller, static body -> effort never appears -> never converges.
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "open-loop",
            "version": "0.1",
            "name": "Open Loop",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "until_converge", "iteration_budget": 5,
                            "setpoint": {"metric": "effort", "op": ">=", "value": 100},
                            "subgraph": ["pt"]}},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    import asyncio
    result = asyncio.run(_run(workflow))
    out = result.final_output
    assert out["loop_converged"] is False
    assert out["loop_iterations"] == 5  # ran the full budget without converging
    assert out["loop_status"] == "halted"


def test_iteration_record_carries_sensor_result_and_controlled_snapshot():
    import asyncio
    result = asyncio.run(_run(_closed_loop_workflow(sensor_type="trajectory")))
    iterations = result.final_output["loop_record"]["iterations"]
    first = iterations[0]
    assert "sensor" in first["evidence"]
    assert "controlled" in first["evidence"]
    assert first["evidence"]["sensor"]["sensor_type"] == "trajectory"
    assert "value" in first["evidence"]["sensor"]
    # verifier_score on IterationRecord mirrors the sensor reading
    assert first["verifier_score"] == first["evidence"]["sensor"]["value"]
