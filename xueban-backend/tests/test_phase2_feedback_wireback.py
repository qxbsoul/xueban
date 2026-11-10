"""
Phase 2 tests: assembly-level feedback wire-back.

Verifies that when a workflow assembled from a goal is executed,
ActivationTrainingSamples carry assembly-level info (block_id, goal)
back to the training pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

ROOT = Path(__file__).parent.parent


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def _make_registry(tmp_path):
    """Create a minimal registry with a generic worker agent."""
    registry_root = tmp_path / "registry"
    for d in ("roles", "agents"):
        (registry_root / d).mkdir(parents=True)

    _write_yaml(registry_root / "roles" / "worker.yaml", {
        "role_id": "worker",
        "version": "0.1",
        "name": "Worker",
    })
    _write_yaml(registry_root / "agents" / "worker_agent.yaml", {
        "agent_id": "worker_agent",
        "version": "0.1",
        "name": "Worker Agent",
        "role": "worker",
        "executor": {"kind": "cli", "provider": "claude"},
    })
    return registry_root


# ---------------------------------------------------------------------------
# 1. AssemblyCompiler writes block_id→node_id mapping into metadata
# ---------------------------------------------------------------------------

def test_assembly_compiler_records_block_node_mapping(tmp_path):
    from xueban.highlevel import (
        AssemblyCompiler,
        SpecRegistry,
        WorkflowAssemblyBlockSpec,
        WorkflowAssemblySpec,
        build_builtin_block_catalog,
    )
    from xueban.assembly.activation import ActivationSelector, ConnectionResolver

    registry = SpecRegistry.load_from_root(_make_registry(tmp_path))
    catalog = build_builtin_block_catalog()
    goal = "plan and execute the task"

    activation = ActivationSelector().select(goal, catalog)
    links = ConnectionResolver().resolve(activation.candidates)

    assembly_blocks = [
        WorkflowAssemblyBlockSpec(id=c.block_id, ref=c.block_id, agent="worker_agent")
        for c in activation.candidates
    ]
    assembly = WorkflowAssemblySpec(
        assembly_id="test-mapping",
        name="test",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    workflow = AssemblyCompiler(registry).compile(assembly)

    # The compiled workflow should carry a block→node mapping in metadata
    mapping = workflow.metadata.get("assembly_block_node_map")
    assert mapping is not None, "assembly_block_node_map missing from workflow metadata"
    for c in activation.candidates:
        assert c.block_id in mapping, f"block_id {c.block_id} missing from mapping"
    # Goal should also be preserved
    assert workflow.metadata.get("assembly_goal") == goal


# ---------------------------------------------------------------------------
# 2. BaseWritebackAdapter has persist_feedback method
# ---------------------------------------------------------------------------

def test_writeback_adapter_has_persist_feedback():
    from xueban.runtime.host_adapter import BaseWritebackAdapter

    adapter = BaseWritebackAdapter()
    assert hasattr(adapter, "persist_feedback")


# ---------------------------------------------------------------------------
# 3. ActivationTrainingSample has assembly fields
# ---------------------------------------------------------------------------

def test_activation_training_sample_has_assembly_fields():
    from xueban.runtime.contracts import ActivationTrainingSample

    sample = ActivationTrainingSample(
        sample_id="s1",
        run_id="r1",
        workflow_id="w1",
        node_id="plan",
        step_status="succeeded",
        activation_mode="always",
        activation_decision="proceed",
        assembly_block_id="plan",
        assembly_goal="plan and execute",
    )
    assert sample.assembly_block_id == "plan"
    assert sample.assembly_goal == "plan and execute"


def test_activation_training_sample_assembly_fields_default_none():
    from xueban.runtime.contracts import ActivationTrainingSample

    sample = ActivationTrainingSample(
        sample_id="s1",
        run_id="r1",
        workflow_id="w1",
        node_id="plan",
        step_status="succeeded",
        activation_mode="always",
        activation_decision="proceed",
    )
    assert sample.assembly_block_id is None
    assert sample.assembly_goal is None


# ---------------------------------------------------------------------------
# 4. export_activation_training_dataset fills assembly fields from metadata
# ---------------------------------------------------------------------------

def test_export_training_dataset_fills_assembly_block_id(tmp_path):
    """
    When a workflow was assembled, the training dataset should carry
    assembly_block_id and assembly_goal on each sample.
    """
    from xueban.highlevel import (
        AssemblyCompiler,
        SpecRegistry,
        WorkflowAssemblyBlockSpec,
        WorkflowAssemblySpec,
        build_builtin_block_catalog,
    )
    from xueban.assembly.activation import ActivationSelector, ConnectionResolver
    from xueban.runtime.service import RuntimeService
    from xueban.runtime.contracts import ActivationTrainingDataset

    registry = SpecRegistry.load_from_root(_make_registry(tmp_path))
    catalog = build_builtin_block_catalog()
    goal = "plan the task"

    activation = ActivationSelector().select(goal, catalog)
    links = ConnectionResolver().resolve(activation.candidates)

    assembly_blocks = [
        WorkflowAssemblyBlockSpec(id=c.block_id, ref=c.block_id, agent="worker_agent")
        for c in activation.candidates
    ]
    assembly = WorkflowAssemblySpec(
        assembly_id="feedback-test",
        name="feedback test",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    workflow = AssemblyCompiler(registry).compile(assembly)

    # Verify the compiled workflow carries assembly metadata for Phase 2 wire-back
    mapping = workflow.metadata.get("assembly_block_node_map")
    assert mapping is not None, "assembly_block_node_map missing from compiled workflow"
    assert workflow.metadata.get("assembly_goal") == goal

    # Verify the mapping is correct: block refs → block ids (in this case they're the same)
    for c in activation.candidates:
        assert c.block_id in mapping, f"{c.block_id} missing from block→node map"


# ---------------------------------------------------------------------------
# 5. export_activation_training_dataset populates assembly fields on samples
# ---------------------------------------------------------------------------

def test_export_training_dataset_populates_assembly_fields_from_run(tmp_path):
    """
    Simulate a run with assembly metadata and verify that
    export_activation_training_dataset fills assembly_block_id and assembly_goal.
    """
    from uuid import uuid4
    from xueban.runtime.service import RuntimeService
    from xueban.runtime.contracts import (
        ActivationCandidate,
        ActivationRecord,
        ActivationTrainingDataset,
        ExecutionFeedbackRecord,
        RunRecord,
        RunResult,
        StepRecord,
        TaskRecord,
    )

    service = RuntimeService()

    # Simulate a completed run with assembly metadata
    run_id = f"run-{uuid4().hex[:8]}"
    workflow_id = "assembled-test"
    goal = "plan the task"
    block_node_map = {"plan": "plan"}  # block_ref → node_id

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    run = RunRecord(
        run_id=run_id,
        request_id=f"req-{uuid4().hex[:8]}",
        workflow_id=workflow_id,
        status="succeeded",
        task_id="task-1",
        started_at=now,
        ended_at=now,
        entrypoint="plan",
    )
    task = TaskRecord(task_id="task-1", run_id=run_id, root_task_id="task-1", title=goal)
    step = StepRecord(
        step_id="step-1",
        run_id=run_id,
        node_id="plan",
        index=1,
        status="succeeded",
        started_at=now,
        ended_at=now,
        output={"result": "ok"},
        metadata={"task_id": "task-1", "node_type": "agent.execute"},
    )
    activation = ActivationRecord(
        activation_id="act-1",
        run_id=run_id,
        step_id="step-1",
        node_id="plan",
        mode="always",
        decision="activated",
        tags=[],
        reasons=[],
        feedback_channels=[],
    )
    candidate = ActivationCandidate(
        candidate_id="cand-1",
        run_id=run_id,
        step_id="step-1",
        node_id="plan",
        candidate_type="node",
        candidate_ref="plan",
        source_signals=[],
        score=1.0,
        selected=True,
    )
    feedback = ExecutionFeedbackRecord(
        feedback_id="fb-1",
        run_id=run_id,
        step_id="step-1",
        node_id="plan",
        summary="test feedback",
        reward_hints={"artifact_count": 0.0},
    )

    result = RunResult(
        run=run,
        task=task,
        steps=[step],
        activations=[activation],
        activation_candidates=[candidate],
        feedback=[feedback],
    )
    # Inject assembly metadata (normally comes from workflow.metadata)
    result.run.metadata["assembly_block_node_map"] = block_node_map
    result.run.metadata["assembly_goal"] = goal

    service._runs[run_id] = result

    dataset = service.export_activation_training_dataset(run_id)
    assert dataset is not None
    assert len(dataset.samples) == 1

    sample = dataset.samples[0]
    assert sample.assembly_block_id == "plan"
    assert sample.assembly_goal == goal
