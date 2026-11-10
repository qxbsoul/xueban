"""Causal attribution tests: method ① Chain-of-Evidence + method ③ counterfactual replay."""

from __future__ import annotations

import asyncio

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition


def _closed_loop(*, coe: bool = False, freeze=None) -> WorkflowDefinition:
    config = {
        "kind": "until_converge",
        "iteration_budget": 20,
        "setpoint": {"metric": "effort", "op": ">=", "value": 100},
        "controller": {"type": "setpoint_nudge", "field": "effort", "rate": 25, "init": {"effort": 0}},
        "subgraph": ["pt"],
    }
    if coe:
        config["attribution"] = {"coe": True}
    if freeze:
        config["freeze"] = freeze
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "attr",
            "version": "0.1",
            "name": "Attr",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop", "config": config},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


# ---------- ① Chain-of-Evidence ----------


def test_coe_emits_structured_claims_with_evidence_refs():
    result = asyncio.run(
        RuntimeService().run(RuntimeRequest(workflow=_closed_loop(coe=True), input={"goal": "g"}))
    )
    iterations = result.final_output["loop_record"]["iterations"]
    # every iteration carries claims; each claim binds a statement to its body source
    for it in iterations:
        claims = it["evidence"]["claims"]
        assert claims, "CoE iteration must carry at least one claim"
        measured = [c for c in claims if c["stmt"].startswith("measured=")]
        assert measured
        assert all(c["evidence_ref"].startswith("body:") for c in claims)
        assert measured[0]["verified"] in ("below", "converging", "converged", "above", "insufficient")


def test_coe_disabled_by_default_omits_claims():
    result = asyncio.run(
        RuntimeService().run(RuntimeRequest(workflow=_closed_loop(), input={"goal": "g"}))
    )
    first = result.final_output["loop_record"]["iterations"][0]
    assert "claims" not in first["evidence"]


# ---------- ③ Counterfactual replay ----------


def test_freeze_factor_blocks_convergence():
    # Freezing 'effort' pins it at init (0) -> never reaches 100 -> runs full budget, halted.
    result = asyncio.run(
        RuntimeService().run(RuntimeRequest(workflow=_closed_loop(freeze=["effort"]), input={"goal": "g"}))
    )
    out = result.final_output
    assert out["loop_converged"] is False
    assert out["loop_iterations"] == 20  # full budget
    assert out["loop_status"] == "halted"


def test_attribute_factor_flags_causal_significance():
    svc = RuntimeService()
    attribution = asyncio.run(svc.attribute_factor(_closed_loop(), {"goal": "g"}, "effort"))

    # factual: effort ramps -> converges in 5; counterfactual: frozen -> never converges
    assert attribution["factual"]["converged"] is True
    assert attribution["factual"]["iterations"] == 5
    assert attribution["counterfactual"]["converged"] is False
    assert attribution["counterfactual"]["iterations"] == 20
    # freezing effort flips convergence off -> effort is causally significant
    assert attribution["causally_significant"] is True
    assert attribution["delta_metric"] == 100  # 100 (factual) - 0 (counterfactual)


def test_attribute_non_factor_is_not_significant():
    # Freezing a field the controller doesn't actuate ('noise') changes nothing.
    svc = RuntimeService()
    attribution = asyncio.run(svc.attribute_factor(_closed_loop(), {"goal": "g"}, "noise"))
    assert attribution["causally_significant"] is False
    assert attribution["factual"]["iterations"] == attribution["counterfactual"]["iterations"]
