"""Loop Engine v2 — DAG body tests.

The loop body is a subgraph of the host DAG: execution follows the workflow's
own edges between subgraph nodes (conditions, back edges). Sensor/Controller
are first-class node types (control.sensor / control.controller). Presets are
an OPEN registry — PDCA ships as a bundled example, users can register their
own via register_loop_preset() or SF_LOOP_PRESETS YAML.

Deterministic emit-based body nodes only; no LLM required.
See docs/architecture/loop-engine-design.md §8.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.loop_engine import (
    execute_sensor_node,
    expand_preset_body,
    get_loop_preset,
    list_loop_presets,
    register_loop_preset,
)


def _run(workflow: WorkflowDefinition, service: RuntimeService | None = None):
    return asyncio.run(
        (service or RuntimeService()).run(
            RuntimeRequest(workflow=workflow, input={"goal": "dag loop test"})
        )
    )


def _task_node(node_id: str, *, emits: dict | None = None, role: str | None = None) -> dict:
    return {
        "id": node_id,
        "kind": "agent",
        "type": "agent.execute",
        "config": {
            "role": role or node_id,
            "message_template": f"[{node_id}] step",
            **({"emit": emits} if emits else {}),
        },
    }


def _loop_node(config: dict) -> dict:
    return {"id": "loop", "kind": "node", "type": "control.loop", "config": config}


def _workflow(nodes: list[dict], edges: list[dict]) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "dag-loop",
            "version": "0.1",
            "name": "DAG Loop",
            "entrypoint": "loop",
            "nodes": nodes,
            "edges": edges + [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def _loop_output(result):
    return result.final_output


# ---------------------------------------------------------------------------
# DAG mode: edges drive order, not list order
# ---------------------------------------------------------------------------


def test_dag_body_follows_edges_not_list_order():
    """subgraph lists [a, b, c] but edges route b→a→c; the walk follows edges."""
    wf = _workflow(
        nodes=[
            _loop_node({"kind": "fixed_count", "iteration_budget": 1, "subgraph": ["a", "b", "c"]}),
            _task_node("a"),
            _task_node("b"),
            _task_node("c"),
        ],
        edges=[
            {"from": "b", "to": "a"},
            {"from": "a", "to": "c"},
        ],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_body_mode"] == "dag"
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["b", "a", "c"]  # edge order, not list order
    assert evidence["body_mode"] == "dag"


def test_linear_fallback_when_body_has_no_edges():
    """No edges among body nodes → legacy list-order chain (backward compat)."""
    wf = _workflow(
        nodes=[
            _loop_node({"kind": "fixed_count", "iteration_budget": 1, "subgraph": ["a", "b"]}),
            _task_node("a"),
            _task_node("b"),
        ],
        edges=[],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_body_mode"] == "linear"
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["a", "b"]


def test_entry_config_overrides_inferred_dag_start():
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "kind": "fixed_count",
                    "iteration_budget": 1,
                    "subgraph": ["a", "b"],
                    "entry": "b",
                }
            ),
            _task_node("a"),
            _task_node("b"),
        ],
        edges=[{"from": "b", "to": "a"}],
    )
    result = _run(wf)
    evidence = _loop_output(result)["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["b", "a"]


def test_conditional_back_edge_rewalks_within_iteration():
    """A back edge check→do turns the walk itself into a graph loop, bounded
    by the per-iteration walk budget (len(body)*3)."""
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "kind": "until_converge",
                    "iteration_budget": 2,
                    "subgraph": ["do", "check"],
                    "setpoint": {"metric": "score", "op": ">=", "value": 5},
                }
            ),
            _task_node("do", emits={"score": 1}),  # never reaches 5
            {
                "id": "check",
                "kind": "node",
                "type": "control.sensor",
                "config": {"type": "threshold", "metric": "score", "op": ">=", "value": 5},
            },
        ],
        edges=[
            {"from": "do", "to": "check"},
            {"from": "check", "to": "do"},  # unconditional back edge
        ],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_body_mode"] == "dag"
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    # walk budget = max(2*3, 1) = 6 visits, alternating do/check
    assert evidence["walk_steps"] == 6
    assert evidence["body_node_ids"] == ["do", "check", "do", "check", "do", "check"]
    assert out["loop_converged"] is False  # score stays 1


# ---------------------------------------------------------------------------
# Sensor / Controller as graph nodes
# ---------------------------------------------------------------------------


def test_sensor_graph_node_drives_convergence():
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "kind": "until_converge",
                    "iteration_budget": 5,
                    "subgraph": ["produce", "check"],
                }
            ),
            _task_node("produce", emits={"score": 7}),
            {
                "id": "check",
                "kind": "node",
                "type": "control.sensor",
                "config": {"type": "threshold", "metric": "score", "op": ">=", "value": 5},
            },
        ],
        edges=[{"from": "produce", "to": "check"}],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_iterations"] == 1
    assert out["loop_converged"] is True
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["produce", "check"]
    assert evidence["sensor"]["converged"] is True
    assert evidence["sensor"]["value"] == 7.0


def test_controller_graph_node_actuates_across_iterations():
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "kind": "fixed_count",
                    "iteration_budget": 2,
                    "subgraph": ["do", "check", "act"],
                }
            ),
            _task_node("do", emits={"score": 1}),
            {
                "id": "check",
                "kind": "node",
                "type": "control.sensor",
                "config": {"type": "threshold", "metric": "score", "op": ">=", "value": 5},
            },
            {
                "id": "act",
                "kind": "node",
                "type": "control.controller",
                "config": {"type": "setpoint_nudge", "field": "score", "rate": 1, "init_value": 0},
            },
        ],
        edges=[
            {"from": "do", "to": "check"},
            {"from": "check", "to": "act"},
        ],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_iterations"] == 2
    iterations = out["loop_record"]["iterations"]
    # iteration 0: controlled starts empty → nudge 0+1=1
    assert iterations[0]["evidence"]["controlled"] == {"score": 1.0}
    # iteration 1: controller reads prior controlled (injected) → nudge 1+1=2
    assert iterations[1]["evidence"]["controlled"] == {"score": 2.0}


# ---------------------------------------------------------------------------
# Presets — open registry, PDCA is a bundled example
# ---------------------------------------------------------------------------


def test_pdca_example_preset_expands_and_runs():
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "preset": "pdca",
                    "kind": "until_converge",
                    "iteration_budget": 3,
                    "setpoint": {"metric": "score", "op": ">=", "value": 5},
                    "sensor": {"type": "threshold"},
                    "phases": {"do": {"emit": {"score": 7}}},
                }
            ),
        ],
        edges=[],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_body_mode"] == "preset"
    assert out["loop_preset"] == "pdca"
    assert out["loop_iterations"] == 1
    assert out["loop_converged"] is True
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["loop-plan", "loop-do", "loop-check", "loop-act"]
    assert evidence["sensor"]["converged"] is True


def test_expand_preset_body_maps_verifier_config_onto_sensor_phase():
    node_cfg = {
        "preset": "pdca",
        "setpoint": {"metric": "score", "op": ">=", "value": 0.8},
        "sensor": {"type": "verifier", "verifier": {"K": 4, "provider": "zhipu"}},
    }
    from xueban.runtime.contracts import NodeDefinition

    node = NodeDefinition(id="loop", type="control.loop", config=node_cfg)
    nodes, edges = expand_preset_body(node)
    check = next(n for n in nodes if n.type == "control.sensor")
    assert check.config["type"] == "verifier"
    assert check.config["metric"] == "score"
    assert check.config["verifier"]["K"] == 4
    assert [(e.from_id, e.to_id) for e in edges] == [
        ("loop-plan", "loop-do"),
        ("loop-do", "loop-check"),
        ("loop-check", "loop-act"),
    ]


def test_register_loop_preset_custom_template():
    register_loop_preset(
        "my_review_loop",
        {
            "description": "draft → review → fix",
            "phases": [
                {"id": "draft", "type": "task", "role": "Drafter"},
                {"id": "review", "type": "control.sensor", "role": "Reviewer"},
                {"id": "fix", "type": "control.controller", "role": "Fixer"},
            ],
        },
    )
    wf = _workflow(
        nodes=[
            _loop_node(
                {
                    "preset": "my_review_loop",
                    "kind": "fixed_count",
                    "iteration_budget": 1,
                }
            ),
        ],
        edges=[],
    )
    result = _run(wf)
    out = _loop_output(result)

    assert out["loop_preset"] == "my_review_loop"
    evidence = out["loop_record"]["iterations"][0]["evidence"]
    assert evidence["body_node_ids"] == ["loop-draft", "loop-review", "loop-fix"]
    assert "my_review_loop" in list_loop_presets()


def test_unknown_preset_raises_at_runtime():
    wf = _workflow(
        nodes=[
            _loop_node({"preset": "no_such_preset", "kind": "fixed_count", "iteration_budget": 1}),
        ],
        edges=[],
    )
    with pytest.raises(ValueError, match="unknown loop preset"):
        _run(wf)


def test_register_loop_preset_rejects_bad_spec():
    with pytest.raises(ValueError, match="phases"):
        register_loop_preset("bad_preset", {"phases": []})
    with pytest.raises(ValueError, match="phase"):
        register_loop_preset(
            "bad_phase",
            {"phases": [{"id": "x", "type": "control.loop"}]},
        )


def test_bundled_presets_are_marked_as_examples():
    for name in ("pdca", "ooda", "generate_evaluate"):
        spec = get_loop_preset(name)
        assert spec["example"] is True, f"{name} should be flagged as a bundled example"


# ---------------------------------------------------------------------------
# Contracts validation
# ---------------------------------------------------------------------------


def test_preset_loop_does_not_require_subgraph():
    wf = _workflow(
        nodes=[_loop_node({"preset": "pdca", "kind": "fixed_count", "iteration_budget": 1})],
        edges=[],
    )
    assert wf.nodes[0].config["preset"] == "pdca"


def test_entry_must_reference_subgraph_node():
    with pytest.raises(ValidationError, match="entry"):
        _workflow(
            nodes=[
                _loop_node(
                    {
                        "kind": "fixed_count",
                        "iteration_budget": 1,
                        "subgraph": ["a", "b"],
                        "entry": "zzz",
                    }
                ),
                _task_node("a"),
                _task_node("b"),
            ],
            edges=[],
        )


def test_empty_preset_string_rejected():
    with pytest.raises(ValidationError, match="preset"):
        _workflow(
            nodes=[_loop_node({"preset": "", "kind": "fixed_count", "iteration_budget": 1})],
            edges=[],
        )


# ---------------------------------------------------------------------------
# Sensor node unit behavior
# ---------------------------------------------------------------------------


def test_sensor_node_verifier_degrades_gracefully(monkeypatch):
    """A verifier-type sensor node without an LLM key must degrade to the
    threshold baseline rather than fail the loop.

    Hermetic per tests/test_verifier_byok.py convention: strip SF_VERIFIER_*
    so an operator shell with real keys can't leak into the suite.
    """
    import os

    for var in list(os.environ):
        if var.startswith("SF_VERIFIER"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from xueban.runtime.contracts import NodeDefinition

    node = NodeDefinition(
        id="check",
        type="control.sensor",
        config={"type": "verifier", "metric": "score", "op": ">=", "value": 5},
    )
    service = RuntimeService()
    output = asyncio.run(execute_sensor_node(service, node, {"score": 9}))
    assert output["converged"] is True
    assert output["value"] == 9.0
    assert output["sensor"]["sensor_type"] in {"verifier", "threshold"}
