"""Story 4.5 + 4.6 — runtime policy hot-swap + reconfigure tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.contracts import (
    RuntimeRequest,
    RunRecord,
    RunResult,
    StepRecord,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
    utc_now,
)
from xueban.runtime.events import RunEventBus, POLICY_UPDATED, RUN_RECONFIGURED
from xueban.runtime.service import RuntimeService


def _make_workflow() -> dict:
    return {
        "workflow_id": "wf-4.5",
        "version": "1.0",
        "name": "Test",
        "entrypoint": "agent_a",
        "nodes": [
            {"id": "agent_a", "type": "agent"},
            {"id": "agent_b", "type": "agent"},
        ],
        "edges": [{"from": "agent_a", "to": "agent_b", "type": "final"}],
    }


def _service_with_run(run_id: str = "run-4.5-test"):
    bus = RunEventBus()
    svc = RuntimeService(event_bus=bus)
    wf = WorkflowDefinition.model_validate(_make_workflow())
    svc._requests_by_run_id[run_id] = RuntimeRequest(workflow=wf, input={"goal": "x"})
    return svc, bus


class TestUpdatePolicy:
    """Story 4.5 AC2 — POST /workflow/runs/{id}/policy runtime update."""

    def test_returns_status_updated(self):
        svc, _ = _service_with_run()
        result = svc.update_policy("run-4.5-test", {"allow_send": {"agent_a": ["agent_b"]}})
        assert result["status"] == "updated"
        assert "affected_downstream_nodes" in result

    def test_publishes_policy_updated_event(self):
        svc, bus = _service_with_run()
        svc.update_policy("run-4.5-test", {"allow_send": {"agent_a": ["agent_b"]}})
        events = bus.get_events("run-4.5-test")
        assert any(e[1]["type"] == POLICY_UPDATED for e in events)
        # Verify the matrix was actually applied as a proper Pydantic model
        req = svc._requests_by_run_id["run-4.5-test"]
        assert isinstance(req.workflow.policy_matrix, WorkflowPolicyMatrixSpec)
        assert req.workflow.policy_matrix.allow_send == {"agent_a": ["agent_b"]}

    def test_rejects_invalid_matrix(self):
        """model_validate should reject structurally broken input."""
        svc, _ = _service_with_run()
        with pytest.raises((ValidationError, ValueError)):
            svc.update_policy("run-4.5-test", {"allow_send": "not-a-dict"})

    def test_affected_downstream_excludes_completed(self):
        svc, _ = _service_with_run()
        # With no completed steps yet, every node in workflow is "affected"
        result = svc.update_policy("run-4.5-test", {})
        assert set(result["affected_downstream_nodes"]) == {"agent_a", "agent_b"}

    @pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "skipped", "cancelled", "invalidated"])
    def test_affected_downstream_excludes_all_terminal_statuses(self, terminal_status):
        """Nodes in any terminal status are excluded from affected_downstream_nodes."""
        svc, _ = _service_with_run()
        run_id = "run-4.5-test"
        now = utc_now()
        svc._runs[run_id] = RunResult(
            run=RunRecord(run_id=run_id, request_id="req-1", workflow_id="wf-4.5", status="running", started_at=now, entrypoint="agent_a"),
            steps=[
                StepRecord(step_id="s1", run_id=run_id, node_id="agent_a", status=terminal_status, index=1, started_at=now),
                StepRecord(step_id="s2", run_id=run_id, node_id="agent_b", status=terminal_status, index=2, started_at=now),
            ],
        )
        result = svc.update_policy(run_id, {})
        assert result["affected_downstream_nodes"] == []

    def test_completed_node_output_preserved_after_policy_update(self):
        """AC3 invariant: completed node outputs must survive policy hot-swap."""
        svc, _ = _service_with_run()
        run_id = "run-4.5-test"
        now = utc_now()
        original_output = {"message": "agent_a result", "score": 42}
        svc._runs[run_id] = RunResult(
            run=RunRecord(run_id=run_id, request_id="req-1", workflow_id="wf-4.5", status="running", started_at=now, entrypoint="agent_a"),
            steps=[
                StepRecord(
                    step_id="s1", run_id=run_id, node_id="agent_a",
                    status="succeeded", index=1, started_at=now,
                    output=original_output,
                ),
            ],
        )
        result = svc.update_policy(run_id, {"allow_send": {"agent_a": ["agent_b"]}})
        # agent_a is terminal (succeeded) → not in affected list
        assert "agent_a" not in result["affected_downstream_nodes"]
        # output is untouched
        step = svc._runs[run_id].steps[0]
        assert step.output == original_output
        assert step.status == "succeeded"

    def test_raises_value_error_for_unknown_run(self):
        svc, _ = _service_with_run()
        with pytest.raises(ValueError):
            svc.update_policy("no-such-run", {})


class TestReconfigure:
    """Story 4.6 AC2 — POST /workflow/runs/{id}/reconfigure."""

    def test_returns_reused_and_new_nodes(self):
        svc, _ = _service_with_run()
        new_def = {
            "agents": [
                {"id": "agent_a"},
                {"id": "agent_b"},
                {"id": "fact_checker"},
            ],
            "edges": [],
            "policy_matrix": {},
        }
        result = svc.reconfigure("run-4.5-test", new_def)
        assert result["status"] == "reconfigured"
        assert "fact_checker" in result["new_nodes"]
        assert "agent_a" not in result["new_nodes"]

    def test_publishes_run_reconfigured(self):
        svc, bus = _service_with_run()
        new_def = {"agents": [{"id": "agent_a"}], "edges": [], "policy_matrix": {}}
        svc.reconfigure("run-4.5-test", new_def)
        events = bus.get_events("run-4.5-test")
        types = [e[1].get("type") for e in events]
        assert RUN_RECONFIGURED in types

    def test_detects_removed_nodes(self):
        svc, _ = _service_with_run()
        # Remove agent_b
        new_def = {"agents": [{"id": "agent_a"}], "edges": [], "policy_matrix": {}}
        result = svc.reconfigure("run-4.5-test", new_def)
        assert "agent_b" in result["removed_nodes"]

    def test_reports_policy_failure_instead_of_swallowing(self):
        """Reconfigure with invalid policy_matrix must surface the error, not swallow it."""
        svc, _ = _service_with_run()
        new_def = {
            "agents": [{"id": "agent_a"}],
            "edges": [],
            "policy_matrix": {"allow_send": "invalid"},
        }
        result = svc.reconfigure("run-4.5-test", new_def)
        assert result["status"] == "reconfigured"
        assert "policy_failure" in result

    def test_raises_value_error_for_unknown_run(self):
        svc, _ = _service_with_run()
        with pytest.raises(ValueError):
            svc.reconfigure("missing", {"agents": []})
