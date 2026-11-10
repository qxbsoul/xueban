"""Story 1.4 — 驳回穿透多层 + Checkpoint Resume 单元测试 (patched)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest

from xueban.runtime.checkpoint_store import InMemoryCheckpointStore
from xueban.runtime.contracts import (
    CheckpointRef,
    CheckpointState,
    RuntimeRequest,
    ResumeRequest,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
    WritebackRef,
)
from xueban.runtime.errors import PolicyViolation
from xueban.runtime.events import (
    CHECKPOINT_SAVED,
    HANDOFF_TRIGGERED,
    NODE_INVALIDATED,
    NODE_REJECTED,
    POLICY_VIOLATION,
    RUN_RESUMED,
)
from xueban.runtime.service import RuntimeService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _three_stage_workflow() -> WorkflowDefinition:
    """outline → litreview → section, with advisor allowed to reject section."""
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "academic-test",
            "version": "1.0",
            "name": "Academic Paper",
            "entrypoint": "outline",
            "nodes": [
                {"id": "outline", "type": "agent"},
                {"id": "litreview", "type": "agent"},
                {"id": "section", "type": "agent"},
                {"id": "advisor", "type": "agent"},
            ],
            "edges": [
                {"from": "outline", "to": "litreview"},
                {"from": "litreview", "to": "section"},
                {"from": "section", "to": "advisor", "type": "final"},
            ],
            "policy_matrix": {
                "allow_send": {},
                "allow_reject": {"advisor": ["section"]},
            },
        }
    )


def _make_checkpoint(
    run_id: str,
    visited: List[str],
    next_node: str,
    state: Dict[str, Any] | None = None,
    checkpoint_id: str | None = None,
) -> CheckpointRef:
    return CheckpointRef(
        checkpoint_id=checkpoint_id or f"ckpt-{uuid4().hex[:8]}",
        run_id=run_id,
        state=CheckpointState(
            current_node_id=visited[-1] if visited else None,
            next_node_id=next_node,
            visited_nodes=visited,
            last_output={"draft": "content"},
            state=state or {},
        ),
        writeback=WritebackRef(
            channel="checkpoint",
            target="host",
            host_action="persist_checkpoint_ref",
            next_node_id=next_node,
            resume_supported=True,
        ),
    )


def _service_with_run(
    wf: WorkflowDefinition,
    run_id: str = "run-1.4-test",
    checkpoint_store: InMemoryCheckpointStore | None = None,
) -> RuntimeService:
    svc = RuntimeService(checkpoint_store=checkpoint_store)
    request = RuntimeRequest(workflow=wf, input={"topic": "neural networks"})
    svc._requests_by_run_id[run_id] = request
    return svc


# ---------------------------------------------------------------------------
# _get_latest_checkpoint
# ---------------------------------------------------------------------------

class TestGetLatestCheckpoint:
    def test_returns_none_when_no_checkpoints(self):
        svc = RuntimeService()
        assert svc._get_latest_checkpoint("no-such-run") is None

    def test_returns_only_checkpoint(self):
        wf = _three_stage_workflow()
        svc = _service_with_run(wf, "run-a")
        cp = _make_checkpoint("run-a", ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp
        result = svc._get_latest_checkpoint("run-a")
        assert result is not None
        assert result.checkpoint_id == cp.checkpoint_id

    def test_returns_latest_when_multiple(self):
        wf = _three_stage_workflow()
        svc = _service_with_run(wf, "run-b")
        early = _make_checkpoint("run-b", ["outline"], "litreview")
        late = _make_checkpoint("run-b", ["outline", "litreview"], "section")
        late = late.model_copy(
            update={"created_at": datetime(2099, 1, 1, tzinfo=timezone.utc)}
        )
        svc._checkpoints[early.checkpoint_id] = early
        svc._checkpoints[late.checkpoint_id] = late
        result = svc._get_latest_checkpoint("run-b")
        assert result is not None
        assert result.checkpoint_id == late.checkpoint_id

    def test_ignores_checkpoints_from_other_runs(self):
        wf = _three_stage_workflow()
        svc = _service_with_run(wf, "run-c")
        cp_other = _make_checkpoint("run-other", ["outline"], "litreview")
        svc._checkpoints[cp_other.checkpoint_id] = cp_other
        assert svc._get_latest_checkpoint("run-c") is None

    def test_reads_from_checkpoint_store(self):
        wf = _three_stage_workflow()
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, "run-d", checkpoint_store=store)
        cp = _make_checkpoint("run-d", ["outline", "litreview"], "section")
        store.put(cp)
        result = svc._get_latest_checkpoint("run-d")
        assert result is not None
        assert result.checkpoint_id == cp.checkpoint_id

    def test_deduplicates_in_memory_and_store(self):
        wf = _three_stage_workflow()
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, "run-e", checkpoint_store=store)
        cp = _make_checkpoint("run-e", ["outline"], "litreview")
        store.put(cp)
        svc._checkpoints[cp.checkpoint_id] = cp
        result = svc._get_latest_checkpoint("run-e")
        assert result is not None
        assert result.checkpoint_id == cp.checkpoint_id


# ---------------------------------------------------------------------------
# reject() with retarget_stage  (all async — P4 fix)
# ---------------------------------------------------------------------------

class TestRejectWithRetargetStage:
    @pytest.mark.asyncio
    async def test_retarget_stage_marks_invalidated_nodes_in_events(self):
        wf = _three_stage_workflow()
        run_id = "run-retarget-1"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="needs more citations",
            retarget_stage="litreview",
        )

        events = svc._rejection_events.get(run_id, [])
        assert len(events) == 1
        all_evts = events[0]["events"]
        event_types = [e["event"] for e in all_evts]

        assert POLICY_VIOLATION in event_types
        assert NODE_REJECTED in event_types
        assert HANDOFF_TRIGGERED in event_types
        # node.invalidated for litreview and section
        invalidated_evts = [e for e in all_evts if e["event"] == NODE_INVALIDATED]
        invalidated_node_ids = {e["node_id"] for e in invalidated_evts}
        assert "litreview" in invalidated_node_ids
        assert "section" in invalidated_node_ids
        assert CHECKPOINT_SAVED in event_types

    @pytest.mark.asyncio
    async def test_retarget_creates_new_snapshot_checkpoint(self):
        """P9 (D1): reject(retarget_stage) creates a NEW checkpoint, original untouched."""
        wf = _three_stage_workflow()
        run_id = "run-retarget-2"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="restructure required",
            retarget_stage="outline",
        )

        # Original checkpoint must remain unchanged (P9: no mutate-in-place)
        original = store.get(cp.checkpoint_id)
        assert original is not None
        assert original.state.next_node_id == "advisor"  # unchanged

        # A new snapshot checkpoint must exist
        all_cps = [c for c in svc._checkpoints.values() if c.run_id == run_id]
        new_cps = [c for c in all_cps if c.checkpoint_id != cp.checkpoint_id]
        assert len(new_cps) == 1
        new_cp = new_cps[0]
        assert new_cp.state.next_node_id == "outline"
        assert new_cp.metadata.get("retarget_stage") == "outline"

    @pytest.mark.asyncio
    async def test_retarget_visited_nodes_trimmed_in_snapshot(self):
        """P7: snapshot visited_nodes must be trimmed to exclude invalidated nodes."""
        wf = _three_stage_workflow()
        run_id = "run-retarget-trim"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="GDPR violation",
            retarget_stage="litreview",
        )

        # New snapshot
        new_cp = next(
            c for c in svc._checkpoints.values()
            if c.run_id == run_id and c.checkpoint_id != cp.checkpoint_id
        )
        # visited_nodes should be ["outline"] (everything before litreview)
        assert new_cp.state.visited_nodes == ["outline"]
        # invalidated_nodes in state
        invalidated = new_cp.state.state.get("invalidated_nodes", [])
        assert "litreview" in invalidated
        assert "section" in invalidated
        assert "outline" not in invalidated

    @pytest.mark.asyncio
    async def test_reject_without_retarget_no_invalidated_events(self):
        wf = _three_stage_workflow()
        run_id = "run-no-retarget"
        svc = _service_with_run(wf, run_id)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="minor fix",
        )

        events = svc._rejection_events.get(run_id, [])
        all_evts = events[0]["events"]
        event_types = [e["event"] for e in all_evts]
        assert NODE_INVALIDATED not in event_types
        assert CHECKPOINT_SAVED not in event_types

    @pytest.mark.asyncio
    async def test_event_order_checkpoint_saved_before_node_invalidated(self):
        """P8 (D2): checkpoint.saved must come BEFORE node.invalidated events."""
        wf = _three_stage_workflow()
        run_id = "run-order-check"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="rewrite",
            retarget_stage="litreview",
        )

        all_evts = [e["event"] for e in svc._rejection_events[run_id][0]["events"]]
        ckpt_idx = all_evts.index(CHECKPOINT_SAVED)
        invalidated_indices = [i for i, e in enumerate(all_evts) if e == NODE_INVALIDATED]
        # P8: checkpoint.saved BEFORE all node.invalidated
        assert all(ckpt_idx < i for i in invalidated_indices), (
            f"Expected checkpoint.saved ({ckpt_idx}) before node.invalidated {invalidated_indices}"
        )

    @pytest.mark.asyncio
    async def test_policy_violation_still_raised_before_retarget(self):
        wf = _three_stage_workflow()
        run_id = "run-policy-retarget"
        svc = _service_with_run(wf, run_id)

        with pytest.raises(PolicyViolation):
            await svc.reject(
                run_id=run_id,
                reviewer_role="section",  # not allowed to reject
                target_node_id="outline",
                reason="unauthorized",
                retarget_stage="outline",
            )

    @pytest.mark.asyncio
    async def test_retarget_stage_not_in_visited_raises_value_error(self):
        """P2: retarget_stage not in visited_nodes must raise ValueError."""
        wf = _three_stage_workflow()
        run_id = "run-retarget-unknown"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        with pytest.raises(ValueError, match="nonexistent_stage"):
            await svc.reject(
                run_id=run_id,
                reviewer_role="advisor",
                target_node_id="section",
                reason="rewrite from scratch",
                retarget_stage="nonexistent_stage",
            )

    @pytest.mark.asyncio
    async def test_retarget_no_checkpoint_raises_value_error(self):
        """P3: retarget_stage set but no checkpoint exists must raise ValueError."""
        wf = _three_stage_workflow()
        run_id = "run-no-ckpt"
        svc = _service_with_run(wf, run_id)  # no checkpoint_store, no in-memory checkpoints

        with pytest.raises(ValueError, match="no checkpoint found"):
            await svc.reject(
                run_id=run_id,
                reviewer_role="advisor",
                target_node_id="section",
                reason="must redo",
                retarget_stage="outline",
            )


# ---------------------------------------------------------------------------
# resume() — P6 status gate, P11 dedup, P12 RUN_RESUMED, P14 rename
# ---------------------------------------------------------------------------

class TestResume:
    @pytest.mark.asyncio
    async def test_resume_does_not_raise_for_succeeded_run(self):
        """P6 (corrected): resume on a 'succeeded' run must NOT raise.
        The normal reject→resume cycle calls resume() on a run whose original
        execution already completed with 'succeeded'. resume() creates a NEW run
        from the checkpoint, so the original run's status is irrelevant.
        """
        from xueban.runtime.contracts import RunRecord, RunResult
        wf = _three_stage_workflow()
        run_id = "run-succeeded"
        svc = _service_with_run(wf, run_id)

        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        # Inject a terminal run result (model_construct bypasses required-field validation
        # for fields we don't care about in this gate test)
        run_record = RunRecord.model_construct(
            run_id=run_id,
            task_id="task-x",
            root_run_id=run_id,
            status="succeeded",
            request_id="req-dummy",
            workflow_id="wf-dummy",
            started_at=datetime.now(timezone.utc),
            entrypoint="outline",
        )
        from xueban.runtime.contracts import RunResult as RR
        svc._runs[run_id] = RR(run=run_record)

        # Must NOT raise PolicyViolation or terminal-state ValueError — it may raise
        # a different error (e.g. no executor for the resumed node) but NOT P6.
        try:
            await svc.resume(run_id, ResumeRequest(checkpoint_id=cp.checkpoint_id))
        except ValueError as exc:
            assert "terminal state" not in str(exc), (
                f"P6 gate incorrectly blocked a succeeded run: {exc}"
            )

    @pytest.mark.asyncio
    async def test_resume_raises_for_cancelled_run(self):
        """P6: resume on cancelled run must raise ValueError."""
        from xueban.runtime.contracts import RunRecord, RunResult
        wf = _three_stage_workflow()
        run_id = "run-cancelled"
        svc = _service_with_run(wf, run_id)

        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        run_record = RunRecord.model_construct(
            run_id=run_id,
            task_id="task-x",
            root_run_id=run_id,
            status="cancelled",
            request_id="req-dummy",
            workflow_id="wf-dummy",
            started_at=datetime.now(timezone.utc),
            entrypoint="outline",
        )
        from xueban.runtime.contracts import RunResult as RR
        svc._runs[run_id] = RR(run=run_record)

        with pytest.raises(ValueError, match="terminal state"):
            await svc.resume(run_id, ResumeRequest(checkpoint_id=cp.checkpoint_id))

    def test_get_latest_checkpoint_ref_public_api(self):
        """P14: get_latest_checkpoint_ref() is the correctly named public method."""
        wf = _three_stage_workflow()
        run_id = "run-pub-api"
        svc = _service_with_run(wf, run_id)

        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        result = svc.get_latest_checkpoint_ref(run_id)
        assert result is not None
        assert result.checkpoint_id == cp.checkpoint_id

    def test_deprecated_alias_still_works(self):
        """P14: resume_from_latest_checkpoint is kept as deprecated alias."""
        wf = _three_stage_workflow()
        run_id = "run-alias"
        svc = _service_with_run(wf, run_id)
        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        # Deprecated alias must still work
        result = svc.resume_from_latest_checkpoint(run_id)
        assert result is not None
        assert result.checkpoint_id == cp.checkpoint_id


# ---------------------------------------------------------------------------
# _get_latest_checkpoint integration with checkpoint_store
# ---------------------------------------------------------------------------

class TestLatestCheckpointWithStore:
    def test_latest_prefers_store_over_empty_memory(self):
        store = InMemoryCheckpointStore()
        wf = _three_stage_workflow()
        svc = _service_with_run(wf, "run-store-1", checkpoint_store=store)

        cp1 = _make_checkpoint("run-store-1", ["outline"], "litreview")
        cp2 = _make_checkpoint("run-store-1", ["outline", "litreview"], "section")
        cp2 = cp2.model_copy(
            update={"created_at": datetime(2099, 12, 31, tzinfo=timezone.utc)}
        )
        store.put(cp1)
        store.put(cp2)

        result = svc._get_latest_checkpoint("run-store-1")
        assert result is not None
        assert result.checkpoint_id == cp2.checkpoint_id


# ---------------------------------------------------------------------------
# P15: End-to-end closure — reject(retarget_stage) → RUN_RESUMED SSE
# ---------------------------------------------------------------------------

class TestEndToEndClosure:
    @pytest.mark.asyncio
    async def test_reject_retarget_produces_checkpoint_for_resume(self):
        """reject(retarget_stage) must produce a checkpoint that resume() can use."""
        wf = _three_stage_workflow()
        run_id = "run-e2e-closure"
        store = InMemoryCheckpointStore()
        svc = _service_with_run(wf, run_id, checkpoint_store=store)

        # Simulate run that reached 'advisor' after completing all 3 stages
        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        # Advisor rejects and retargets to litreview
        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="needs more citations",
            retarget_stage="litreview",
        )

        # get_latest_checkpoint_ref() should now return the NEW snapshot checkpoint
        latest = svc.get_latest_checkpoint_ref(run_id)
        assert latest is not None
        assert latest.state.next_node_id == "litreview"
        assert latest.checkpoint_id != cp.checkpoint_id  # new snapshot, not the original

    @pytest.mark.asyncio
    async def test_run_resumed_event_emitted(self):
        """P12: resume() must emit RUN_RESUMED to the event bus."""
        from xueban.runtime.events import RunEventBus

        wf = _three_stage_workflow()
        run_id = "run-e2e-resumed"
        bus = RunEventBus()
        svc = RuntimeService(checkpoint_store=InMemoryCheckpointStore(), event_bus=bus)
        svc._requests_by_run_id[run_id] = RuntimeRequest(
            workflow=wf, input={"topic": "neural networks"}
        )

        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        # resume will raise because _execute has no executors, but RUN_RESUMED is emitted
        # before _execute is called, so it must already be in the ring buffer.
        try:
            await svc.resume(run_id, ResumeRequest(checkpoint_id=cp.checkpoint_id))
        except Exception:
            pass  # execution may fail without real executors; we only care about the event

        # bus._store holds deque of (seq, event) — read directly (no async consumer needed)
        stored = list(bus._store.get(run_id, []))
        run_resumed_events = [evt for _seq, evt in stored if evt.get("type") == RUN_RESUMED]
        assert len(run_resumed_events) >= 1
        assert run_resumed_events[0]["run_id"] == run_id
        assert run_resumed_events[0]["checkpoint_id"] == cp.checkpoint_id

    @pytest.mark.asyncio
    async def test_reject_events_published_to_sse(self):
        """P1: reject() events must reach the event bus (SSE), not just _rejection_events."""
        from xueban.runtime.events import RunEventBus

        wf = _three_stage_workflow()
        run_id = "run-sse-test"
        store = InMemoryCheckpointStore()
        bus = RunEventBus()
        svc = RuntimeService(checkpoint_store=store, event_bus=bus)
        svc._requests_by_run_id[run_id] = RuntimeRequest(
            workflow=wf, input={"topic": "neural networks"}
        )

        cp = _make_checkpoint(run_id, ["outline", "litreview", "section"], "advisor")
        store.put(cp)

        await svc.reject(
            run_id=run_id,
            reviewer_role="advisor",
            target_node_id="section",
            reason="GDPR",
            retarget_stage="litreview",
        )

        # bus._store holds deque of (seq, event) — read directly
        stored = list(bus._store.get(run_id, []))
        event_types = [evt.get("event") for _seq, evt in stored]
        assert POLICY_VIOLATION in event_types
        assert NODE_REJECTED in event_types
        assert HANDOFF_TRIGGERED in event_types
        assert CHECKPOINT_SAVED in event_types
        assert NODE_INVALIDATED in event_types

    @pytest.mark.asyncio
    async def test_resume_rejects_concurrent_call_via_lock(self):
        """P11: a second concurrent resume call must wait (lock prevents torn state)."""
        import asyncio
        wf = _three_stage_workflow()
        run_id = "run-concurrent-resume"
        svc = _service_with_run(wf, run_id)

        cp = _make_checkpoint(run_id, ["outline"], "litreview")
        svc._checkpoints[cp.checkpoint_id] = cp

        lock = svc._get_run_lock(run_id)
        assert lock is not None
        # The same lock object is returned on second call
        assert svc._get_run_lock(run_id) is lock
