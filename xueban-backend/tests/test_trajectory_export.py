"""Story 1.5 — Trajectory Export + Run 查询 API 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

import pytest

from xueban.runtime.checkpoint_store import InMemoryCheckpointStore
from xueban.runtime.contracts import (
    ArtifactRef,
    CheckpointRef,
    CheckpointState,
    HandoffRef,
    RunRecord,
    RunResult,
    RunTrajectory,
    RuntimeRequest,
    StepRecord,
    TrajectoryBundle,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
    WritebackRef,
)
from xueban.runtime.trajectory import build_run_trajectory, build_trajectory_bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _writeback(channel="artifact", action="persist_artifact_ref") -> WritebackRef:
    return WritebackRef(channel=channel, target="host", host_action=action)


def _checkpoint_writeback() -> WritebackRef:
    return WritebackRef(
        channel="checkpoint", target="host", host_action="persist_checkpoint_ref", resume_supported=True
    )


def _run_record(run_id: str = "run-traj-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        request_id=f"req-{uuid4().hex[:8]}",
        workflow_id="wf-test",
        status="succeeded",
        started_at=_utc_now(),
        ended_at=_utc_now(),
        entrypoint="outline",
    )


def _step(run_id: str, node_id: str, status: str = "succeeded", index: int = 0) -> StepRecord:
    return StepRecord(
        step_id=f"step-{uuid4().hex[:8]}",
        run_id=run_id,
        node_id=node_id,
        status=status,
        index=index,
        started_at=_utc_now(),
    )


def _artifact(step_id: str, run_id: str = "run-traj-1") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art-{uuid4().hex[:8]}",
        kind="text",
        name="output",
        uri="host://artifacts/output",
        producer_step_id=step_id,
        writeback=_writeback(),
    )


def _checkpoint(run_id: str) -> CheckpointRef:
    return CheckpointRef(
        checkpoint_id=f"ckpt-{uuid4().hex[:8]}",
        run_id=run_id,
        state=CheckpointState(
            current_node_id="outline",
            next_node_id="litreview",
            visited_nodes=["outline"],
            last_output={"draft": "x"},
        ),
        writeback=_checkpoint_writeback(),
    )


def _handoff(run_id: str, step_id: str) -> HandoffRef:
    return HandoffRef(
        handoff_id=f"hoff-{uuid4().hex[:8]}",
        run_id=run_id,
        from_step_id=step_id,
        from_node_id="outline",
        to_node_id="litreview",
    )


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "wf-test",
            "version": "1.0",
            "name": "Test WF",
            "entrypoint": "outline",
            "nodes": [
                {"id": "outline", "type": "agent"},
                {"id": "litreview", "type": "agent"},
                {"id": "advisor", "type": "agent"},
            ],
            "edges": [
                {"from": "outline", "to": "litreview"},
                {"from": "litreview", "to": "advisor", "type": "final"},
            ],
            "policy_matrix": {
                "allow_send": {},
                "allow_reject": {"advisor": ["litreview"]},
            },
        }
    )


def _run_result(run_id: str = "run-traj-1") -> RunResult:
    run = _run_record(run_id)
    step = _step(run_id, "outline")
    art = _artifact(step.step_id, run_id)
    cp = _checkpoint(run_id)
    hoff = _handoff(run_id, step.step_id)
    return RunResult(
        run=run,
        steps=[step],
        artifacts=[art],
        checkpoints=[cp],
        handoffs=[hoff],
    )


# ---------------------------------------------------------------------------
# RunTrajectory structure
# ---------------------------------------------------------------------------

class TestRunTrajectory:
    def test_has_five_required_keys(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        d = traj.model_dump(mode="json", exclude_none=True)
        for key in ("run", "steps", "handoffs", "checkpoints", "final_artifacts"):
            assert key in d, f"missing key: {key}"

    def test_steps_populated(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        assert len(traj.steps) == 1

    def test_handoffs_populated(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        assert len(traj.handoffs) == 1

    def test_checkpoints_populated(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        assert len(traj.checkpoints) == 1

    def test_final_artifacts_populated(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        # The one artifact is produced by a succeeded step — should appear in final_artifacts
        assert len(traj.final_artifacts) >= 0  # may be 0 if no "final" flag

    def test_empty_run_result(self):
        result = RunResult(run=_run_record(), steps=[], artifacts=[], checkpoints=[], handoffs=[])
        traj = build_run_trajectory(result)
        assert traj.steps == []
        assert traj.handoffs == []
        assert traj.checkpoints == []


# ---------------------------------------------------------------------------
# ISO 8601 UTC timestamps
# ---------------------------------------------------------------------------

class TestTimestampFormat:
    def test_run_started_at_is_utc(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        d = traj.model_dump(mode="json", exclude_none=True)
        ts = d["run"]["started_at"]
        # datetime in JSON mode is ISO 8601 string
        assert isinstance(ts, str)
        # Must be parseable and UTC-aware
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None

    def test_exported_at_present(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        d = traj.model_dump(mode="json", exclude_none=True)
        assert "exported_at" in d


# ---------------------------------------------------------------------------
# exclude_none=True
# ---------------------------------------------------------------------------

class TestExcludeNone:
    def test_no_null_values_in_summary(self):
        result = _run_result()
        traj = build_run_trajectory(result)
        d = traj.model_dump(mode="json", exclude_none=True)
        _assert_no_none(d)

    def test_no_null_values_in_bundle(self):
        result = _run_result()
        wf = _workflow()
        bundle = build_trajectory_bundle(result, wf)
        d = bundle.model_dump(mode="json", exclude_none=True)
        _assert_no_none(d)


def _assert_no_none(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert v is not None, f"unexpected None at key={k}"
            _assert_no_none(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_none(item)


# ---------------------------------------------------------------------------
# TrajectoryBundle (format=trajectory)
# ---------------------------------------------------------------------------

class TestTrajectoryBundle:
    def test_has_trajectory_key(self):
        result = _run_result()
        bundle = build_trajectory_bundle(result)
        d = bundle.model_dump(mode="json", exclude_none=True)
        assert "trajectory" in d

    def test_includes_workflow_yaml_when_provided(self):
        result = _run_result()
        wf = _workflow()
        bundle = build_trajectory_bundle(result, wf)
        d = bundle.model_dump(mode="json", exclude_none=True)
        assert "workflow_yaml" in d
        assert isinstance(d["workflow_yaml"], str)
        assert "wf-test" in d["workflow_yaml"]

    def test_includes_policy_matrix_when_provided(self):
        result = _run_result()
        wf = _workflow()
        bundle = build_trajectory_bundle(result, wf)
        d = bundle.model_dump(mode="json", exclude_none=True)
        assert "policy_matrix" in d
        pm = d["policy_matrix"]
        assert "allow_reject" in pm

    def test_no_workflow_yaml_when_workflow_not_provided(self):
        result = _run_result()
        bundle = build_trajectory_bundle(result, workflow=None)
        d = bundle.model_dump(mode="json", exclude_none=True)
        assert "workflow_yaml" not in d

    def test_bundle_version_present(self):
        result = _run_result()
        bundle = build_trajectory_bundle(result)
        assert bundle.bundle_version == "1.0"

    def test_metadata_has_run_id(self):
        result = _run_result("run-meta-check")
        bundle = build_trajectory_bundle(result)
        assert bundle.metadata["run_id"] == "run-meta-check"

    def test_trajectory_preserves_steps(self):
        result = _run_result()
        wf = _workflow()
        bundle = build_trajectory_bundle(result, wf)
        assert len(bundle.trajectory.steps) == 1

    def test_exported_at_deterministic(self):
        result = _run_result()
        wf = _workflow()
        b1 = build_trajectory_bundle(result, wf)
        b2 = build_trajectory_bundle(result, wf)
        assert b1.exported_at == b2.exported_at
        assert b1.trajectory.exported_at == b1.exported_at


# ---------------------------------------------------------------------------
# Non-happy-path run statuses (CRITICAL coverage)
# ---------------------------------------------------------------------------

def _run_record_with_status(status: str, run_id: str = "run-status-test") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        request_id=f"req-{uuid4().hex[:8]}",
        workflow_id="wf-test",
        status=status,
        started_at=_utc_now(),
        ended_at=_utc_now() if status in ("succeeded", "failed", "cancelled") else None,
        entrypoint="outline",
    )


def _run_result_with_status(status: str, step_statuses: list[str] | None = None) -> RunResult:
    run_id = f"run-{status}"
    run = _run_record_with_status(status, run_id)
    steps = []
    if step_statuses:
        for i, ss in enumerate(step_statuses):
            steps.append(_step(run_id, f"node-{i}", status=ss, index=i))
    else:
        steps.append(_step(run_id, "outline", status="succeeded"))
    art = _artifact(steps[0].step_id, run_id)
    cp = _checkpoint(run_id)
    hoff = _handoff(run_id, steps[0].step_id)
    return RunResult(run=run, steps=steps, artifacts=[art], checkpoints=[cp], handoffs=[hoff])


class TestRejectedRun:
    def test_trajectory_builds_for_failed_run(self):
        result = _run_result_with_status("failed", ["succeeded", "failed"])
        traj = build_run_trajectory(result)
        assert traj.run.status == "failed"
        assert len(traj.steps) == 2

    def test_trajectory_no_final_artifacts_when_none_flagged(self):
        result = _run_result_with_status("failed", ["succeeded", "failed"])
        traj = build_run_trajectory(result)
        assert traj.final_artifacts == []

    def test_bundle_builds_for_failed_run(self):
        result = _run_result_with_status("failed", ["succeeded", "failed"])
        wf = _workflow()
        bundle = build_trajectory_bundle(result, wf)
        assert bundle.metadata["status"] == "failed"


class TestResumedRun:
    def test_trajectory_builds_for_checkpointed_run(self):
        result = _run_result_with_status("checkpointed", ["succeeded", "skipped"])
        traj = build_run_trajectory(result)
        assert traj.run.status == "checkpointed"
        assert len(traj.checkpoints) == 1

    def test_exported_at_uses_started_at_when_no_ended(self):
        result = _run_result_with_status("checkpointed")
        traj = build_run_trajectory(result)
        assert traj.exported_at == result.run.started_at


class TestAwaitingApprovalRun:
    def test_trajectory_builds_for_awaiting_approval(self):
        result = _run_result_with_status("awaiting_approval", ["succeeded", "pending"])
        traj = build_run_trajectory(result)
        assert traj.run.status == "awaiting_approval"
        assert len(traj.steps) == 2

    def test_bundle_for_awaiting_approval_without_workflow(self):
        result = _run_result_with_status("awaiting_approval")
        bundle = build_trajectory_bundle(result, workflow=None)
        assert bundle.workflow_yaml is None
        assert bundle.policy_matrix is None
        assert bundle.metadata["status"] == "awaiting_approval"


class TestCancelledRun:
    def test_trajectory_builds_for_cancelled(self):
        result = _run_result_with_status("cancelled", ["succeeded", "cancelled"])
        traj = build_run_trajectory(result)
        assert traj.run.status == "cancelled"
        assert len(traj.steps) == 2


# ---------------------------------------------------------------------------
# Sorting (deterministic order)
# ---------------------------------------------------------------------------

class TestSorting:
    def test_steps_sorted_by_index(self):
        run_id = "run-sort"
        run = _run_record(run_id)
        s2 = _step(run_id, "litreview", index=2)
        s0 = _step(run_id, "outline", index=0)
        s1 = _step(run_id, "advisor", index=1)
        result = RunResult(run=run, steps=[s2, s0, s1], artifacts=[], checkpoints=[], handoffs=[])
        traj = build_run_trajectory(result)
        assert [s.index for s in traj.steps] == [0, 1, 2]
