"""River Memory tests: module ops + loop integration (design §5.7)."""

from __future__ import annotations

import asyncio

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.river_memory import RiverMemory


# ---------- module-level ops ----------


def test_pour_drink_filters_by_kind_tags_and_recency():
    river = RiverMemory()
    river.pour({"n": 1}, run_id="r1", kind="observation", tags=["a"])
    river.pour({"n": 2}, run_id="r1", kind="observation", tags=["b"])
    river.pour({"n": 3}, run_id="r1", kind="decision", tags=["a"])

    by_kind = [c.content["n"] for c in river.drink(kind="observation")]
    assert by_kind == [1, 2]
    by_tag = [c.content["n"] for c in river.drink(tags=["b"])]
    assert by_tag == [2]
    limited = river.drink(limit=1)
    assert len(limited) == 1 and limited[0].content["n"] == 3  # most-recent-first internally


def test_scoop_predicate():
    river = RiverMemory()
    river.pour({"v": 5}, run_id="r")
    river.pour({"v": 50}, run_id="r")
    big = river.scoop(lambda c: c.content.get("v", 0) > 10)
    assert [c.content["v"] for c in big] == [50]


def test_settle_and_dredge_sediment_layer():
    river = RiverMemory()
    river.pour({"n": 1}, run_id="r", kind="observation")  # main, not sediment
    river.settle({"skill": "plan_v2"}, run_id="r", tags=["planning"])
    # dredge reads sediment only; drink reads main only
    assert [c.content["skill"] for c in river.dredge()] == ["plan_v2"]
    assert river.drink() == [river.drink()[0]]  # main has the observation
    assert all(c.kind == "observation" for c in river.drink())


def test_build_dam_and_open_dam_time_travel():
    river = RiverMemory()
    river.pour({"n": 1}, run_id="r")
    river.build_dam("ckpt-A")
    river.pour({"n": 2}, run_id="r")
    # opening the dam returns the snapshot frozen at build time (only n=1)
    snapshot = river.open_dam("ckpt-A")
    assert [c.content["n"] for c in snapshot] == [1]
    assert "ckpt-A" in river.dam_names()


def test_clear_by_run_id():
    river = RiverMemory()
    river.pour({"n": 1}, run_id="r1")
    river.pour({"n": 2}, run_id="r2")
    river.clear("r1")
    assert [c.run_id for c in river.drink()] == ["r2"]


# ---------- loop integration ----------


def _closed_loop_with_river(river: RiverMemory, *, recall_tags=None) -> RuntimeService:
    svc = RuntimeService(river_memory=river)
    return svc


def _river_workflow(*, tags=None) -> WorkflowDefinition:
    config = {
        "kind": "until_converge",
        "iteration_budget": 20,
        "setpoint": {"metric": "effort", "op": ">=", "value": 100},
        "controller": {"type": "setpoint_nudge", "field": "effort", "rate": 25, "init": {"effort": 0}},
        "subgraph": ["pt"],
        "memory": {"river": True, "recall": 3, **({"tags": tags} if tags else {})},
    }
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "river-loop",
            "version": "0.1",
            "name": "River Loop",
            "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop", "config": config},
                {"id": "pt", "kind": "agent", "type": "agent.execute",
                 "config": {"role": "pt", "message_template": "[pt]"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def test_converging_loop_settles_pattern_into_river():
    river = RiverMemory()
    svc = _closed_loop_with_river(river)
    result = asyncio.run(svc.run(RuntimeRequest(workflow=_river_workflow(), input={"goal": "g"})))

    out = result.final_output
    assert out["loop_converged"] is True
    assert out["loop_memory_settled"] is True
    assert out["loop_memory_recalled"] == 0  # first run: nothing to recall
    # The pattern was sedimented.
    patterns = river.dredge()
    assert len(patterns) == 1
    assert patterns[0].content["controlled"]["effort"] == 100
    assert patterns[0].content["iterations"] == 5


def test_second_run_recalls_prior_settled_pattern():
    river = RiverMemory()
    svc = _closed_loop_with_river(river)

    asyncio.run(svc.run(RuntimeRequest(workflow=_river_workflow(), input={"goal": "g1"})))
    # Second run shares the river -> recalls the pattern settled by the first.
    result2 = asyncio.run(svc.run(RuntimeRequest(workflow=_river_workflow(), input={"goal": "g2"})))

    assert result2.final_output["loop_memory_recalled"] == 1
    assert result2.final_output["loop_memory_settled"] is True
    assert len(river.dredge()) == 2  # both runs sedimented
