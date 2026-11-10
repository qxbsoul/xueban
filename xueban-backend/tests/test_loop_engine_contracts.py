"""LE-0 Loop Engine contract tests.

Covers the new cybernetic-loop data models added in
docs/architecture/loop-engine-design.md (v0.2): LoopKind, LoopFrame,
IterationRecord, LoopRecord, plus the CheckpointState.loop_frames /
StepRecord.attempt_number / AgentCapabilities.loop_capable extensions.
"""

from __future__ import annotations

from xueban.runtime.contracts import (
    AgentCapabilities,
    CheckpointState,
    IterationRecord,
    LoopFrame,
    LoopKind,
    LoopRecord,
    StepRecord,
)


def test_loop_kind_alias_accepts_all_variants() -> None:
    for kind in ("while", "foreach", "until_converge", "fixed_count"):
        record = LoopRecord(run_id="run-1", kind=kind)  # type: ignore[arg-type]
        assert record.kind == kind


def test_loop_record_defaults_and_fractal_nesting() -> None:
    outer = LoopRecord(run_id="run-1", kind="until_converge", setpoint={"metric": "pass", "op": ">=", "value": 0.9})
    inner = LoopRecord(
        run_id="run-1",
        parent_loop_id=outer.loop_id,
        kind="until_converge",
        depth=1,
    )
    assert outer.parent_loop_id is None
    assert outer.depth == 0
    assert outer.status == "running"
    assert outer.iterations == []
    assert inner.parent_loop_id == outer.loop_id
    assert inner.depth == 1


def test_iteration_record_is_causal_atom() -> None:
    iteration = IterationRecord(
        loop_id="loop-1",
        run_id="run-1",
        index=3,
        evidence={"outputs": {"diff": "s3://.../pr.diff"}},
        changed_factors=["plan.prompt_v2", "worker.bob.added"],
        metrics={"test_pass_rate": 0.96},
        verifier_score=0.91,
        trace_id="req_abc",
    )
    assert iteration.index == 3
    assert iteration.changed_factors == ["plan.prompt_v2", "worker.bob.added"]
    assert iteration.verifier_score == 0.91
    assert iteration.iteration_id.startswith("iter-")


def test_loop_frame_carries_local_visited_and_depth() -> None:
    frame = LoopFrame(
        loop_id="loop-1",
        index=2,
        local_visited=["P", "D", "C"],
        setpoint={"goal_met": True},
        depth=1,
    )
    assert frame.local_visited == ["P", "D", "C"]
    assert frame.depth == 1
    assert frame.iteration_ref is None


def test_checkpoint_state_loop_frames_default_empty() -> None:
    # Backward compat: an old payload without loop_frames must still load.
    state = CheckpointState()
    assert state.loop_frames == []


def test_checkpoint_state_holds_loop_call_stack() -> None:
    inner = LoopFrame(loop_id="loop-inner", index=1, depth=1)
    outer = LoopFrame(loop_id="loop-outer", index=0, depth=0)
    state = CheckpointState(loop_frames=[outer, inner])
    assert [frame.loop_id for frame in state.loop_frames] == ["loop-outer", "loop-inner"]
    round_tripped = CheckpointState.model_validate_json(state.model_dump_json())
    assert round_tripped.loop_frames[1].depth == 1


def test_step_record_attempt_and_iteration_defaults() -> None:
    # Backward compat: old StepRecord payloads (no attempt_number/iteration_ref) load.
    step = StepRecord(
        step_id="s1",
        run_id="run-1",
        node_id="planner",
        status="succeeded",
        index=1,
        started_at="2026-08-11T00:00:00+00:00",
    )
    assert step.attempt_number == 1
    assert step.iteration_ref is None


def test_step_record_marks_retry_and_iteration() -> None:
    step = StepRecord(
        step_id="s2",
        run_id="run-1",
        node_id="D",
        status="succeeded",
        index=4,
        attempt_number=2,
        iteration_ref="iter-deadbeef",
        started_at="2026-08-11T00:00:00+00:00",
    )
    assert step.attempt_number == 2
    assert step.iteration_ref == "iter-deadbeef"


def test_agent_capabilities_loop_capable_default_false() -> None:
    caps = AgentCapabilities()
    assert caps.loop_capable is False
    loop_caps = AgentCapabilities(loop_capable=True)
    assert loop_caps.loop_capable is True


def test_loop_record_full_round_trip() -> None:
    record = LoopRecord(
        run_id="run-1",
        kind="until_converge",
        setpoint={"metric": "review_approved", "op": "==", "value": True},
        status="converged",
        iterations=[
            IterationRecord(loop_id="x", run_id="run-1", index=0, verifier_score=0.4),
            IterationRecord(loop_id="x", run_id="run-1", index=1, verifier_score=0.92),
        ],
    )
    dumped = record.model_dump_json()
    restored = LoopRecord.model_validate_json(dumped)
    assert restored.status == "converged"
    assert len(restored.iterations) == 2
    assert restored.iterations[1].verifier_score == 0.92
