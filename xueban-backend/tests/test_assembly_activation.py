"""
Tests for Phase 1 assembly activation:
  - WorkflowBlockSpec.capabilities field (Step 2)
  - build_builtin_block_catalog() tags + capabilities (Step 2)
  - ActivationSelector + ConnectionResolver (Step 3)
  - CLI `registry list --kind blocks` (Step 4)
  - CLI `assemble --goal` (Step 5)
  - End-to-end: assemble → compile → WorkflowDefinition (Step 7)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")

# ---------------------------------------------------------------------------
# Step 2 — WorkflowBlockSpec.capabilities field
# ---------------------------------------------------------------------------

def test_workflow_block_spec_has_capabilities_field():
    from xueban.highlevel import WorkflowBlockSpec

    spec = WorkflowBlockSpec(
        block_id="test",
        kind="worker",
        type="test",
        label="Test",
        compile={"node_kind": "node", "node_type": "test.run"},
    )
    assert hasattr(spec, "capabilities")
    assert spec.capabilities == []


def test_workflow_block_spec_capabilities_can_be_set():
    from xueban.highlevel import WorkflowBlockSpec

    spec = WorkflowBlockSpec(
        block_id="test",
        kind="worker",
        type="test",
        label="Test",
        compile={"node_kind": "node", "node_type": "test.run"},
        capabilities=["planning", "task_decomposition"],
    )
    assert spec.capabilities == ["planning", "task_decomposition"]


# ---------------------------------------------------------------------------
# Step 2 — build_builtin_block_catalog() tags + capabilities
# ---------------------------------------------------------------------------

def test_builtin_catalog_worker_blocks_have_local_activation_tags():
    from xueban.highlevel import build_builtin_block_catalog

    catalog = build_builtin_block_catalog()
    worker_ids = ["plan", "review", "execute"]
    for bid in worker_ids:
        block = catalog[bid]
        assert block.local_activation is not None, f"{bid}: local_activation is None"
        assert len(block.local_activation.tags) > 0, f"{bid}: tags is empty"


def test_builtin_catalog_worker_blocks_have_capabilities():
    from xueban.highlevel import build_builtin_block_catalog

    catalog = build_builtin_block_catalog()
    worker_ids = ["plan", "review", "execute"]
    for bid in worker_ids:
        block = catalog[bid]
        assert len(block.capabilities) > 0, f"{bid}: capabilities is empty"


def test_builtin_catalog_all_eight_blocks_present():
    from xueban.highlevel import build_builtin_block_catalog

    catalog = build_builtin_block_catalog()
    expected = {"plan", "review", "execute", "parallel", "barrier", "delegate", "artifact", "checkpoint"}
    assert set(catalog.keys()) == expected


# ---------------------------------------------------------------------------
# Step 3 — ActivationSelector
# ---------------------------------------------------------------------------

def test_activation_selector_english_goal_matches_capabilities():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    result = ActivationSelector().select("plan and execute the task", catalog)
    block_ids = {c.block_id for c in result.candidates}
    assert "plan" in block_ids or "execute" in block_ids


def test_activation_selector_chinese_goal_matches_tags():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    result = ActivationSelector().select("规划并执行任务", catalog)
    block_ids = {c.block_id for c in result.candidates}
    assert len(block_ids) >= 1


def test_activation_selector_complete_true_when_all_capabilities_covered():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    result = ActivationSelector().select("plan the task", catalog)
    # "plan" goal should be fully coverable by the plan block
    assert result.complete is True
    assert result.missing_capabilities == []


def test_activation_selector_complete_false_with_missing_capabilities():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    # A goal that mentions a capability no block has
    result = ActivationSelector().select("quantum entanglement teleportation", catalog)
    assert result.complete is False
    assert len(result.missing_capabilities) > 0


def test_activation_selector_ood_goal_returns_surface_to_user():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    result = ActivationSelector().select("xyzzy frobnicate quux", catalog)
    assert result.complete is False
    assert result.missing_capabilities == ["unknown"]
    assert result.fallback_policy == "surface_to_user"


def test_activation_selector_greedy_prefers_fewer_blocks():
    from xueban.highlevel import build_builtin_block_catalog
    from xueban.assembly.activation import ActivationSelector

    catalog = build_builtin_block_catalog()
    # A simple single-capability goal should only activate one block
    result = ActivationSelector().select("plan the work", catalog)
    assert result.complete is True
    # plan block alone should cover "planning" — should not add unnecessary blocks
    assert len(result.candidates) >= 1
    # All candidates should have matched_capabilities set
    for c in result.candidates:
        assert len(c.matched_capabilities) > 0


# ---------------------------------------------------------------------------
# Step 3 — ConnectionResolver
# ---------------------------------------------------------------------------

def test_connection_resolver_empty_candidates_returns_empty():
    from xueban.assembly.activation import ConnectionResolver

    links = ConnectionResolver().resolve([])
    assert links == []


def test_connection_resolver_single_block_links_to_end():
    from xueban.assembly.activation import CatalogActivationCandidate, ConnectionResolver

    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
    ]
    links = ConnectionResolver().resolve(candidates)
    assert len(links) == 1
    assert links[0].from_id == "plan"
    assert links[0].to_id == "END"


def test_connection_resolver_multi_block_produces_linear_chain():
    from xueban.assembly.activation import CatalogActivationCandidate, ConnectionResolver

    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
        CatalogActivationCandidate(block_id="execute", matched_capabilities=["execution"]),
    ]
    links = ConnectionResolver().resolve(candidates)
    assert len(links) == 2
    assert links[0].from_id == "plan"
    assert links[0].to_id == "execute"
    assert links[1].from_id == "execute"
    assert links[1].to_id == "END"


# ---------------------------------------------------------------------------
# Step 4 — CLI `registry list --kind blocks`
# ---------------------------------------------------------------------------

def test_cli_registry_list_kind_blocks():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "registry",
            "list",
            "--registry-root",
            str(ROOT / "example_registry"),
            "--kind",
            "blocks",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    # Should include builtin block IDs
    block_ids = [item.get("block_id") or item.get("id") for item in payload]
    assert "plan" in block_ids or any("plan" in str(i) for i in block_ids)


# ---------------------------------------------------------------------------
# Step 5 — CLI `assemble --goal`
# ---------------------------------------------------------------------------

def test_cli_assemble_goal_exits_0_on_complete():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "assemble",
            "--goal",
            "plan and execute the task",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["complete"] is True
    assert "assembly" in payload
    assert "candidates" in payload


def test_cli_assemble_goal_exits_1_on_incomplete():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "assemble",
            "--goal",
            "xyzzy frobnicate quux",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["complete"] is False
    assert "missing_capabilities" in payload


# ---------------------------------------------------------------------------
# Step A — assemble --goal --compile (one-shot goal → WorkflowDefinition)
# ---------------------------------------------------------------------------

def test_cli_assemble_goal_with_compile_outputs_workflow():
    """When --compile --provider --executor-kind are given, output a full WorkflowDefinition."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "assemble",
            "--goal",
            "plan and execute the task",
            "--compile",
            "--provider",
            "claude",
            "--executor-kind",
            "cli",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    # Should be a compiled WorkflowDefinition
    assert "workflow_id" in payload
    assert "nodes" in payload
    assert "entrypoint" in payload
    # Should carry assembly metadata
    assert payload["metadata"]["assembly_goal"] == "plan and execute the task"
    assert "assembly_block_node_map" in payload["metadata"]


def test_cli_assemble_compile_incomplete_goal_exits_1():
    """--compile with an OOD goal should still exit 1."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "assemble",
            "--goal",
            "xyzzy frobnicate quux",
            "--compile",
            "--provider",
            "claude",
            "--executor-kind",
            "cli",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["complete"] is False


# ---------------------------------------------------------------------------
# Step 7 — End-to-end: assemble → compile → WorkflowDefinition
# ---------------------------------------------------------------------------

def test_e2e_assemble_to_compile_produces_valid_workflow(tmp_path):
    """
    Full pipeline: goal → ActivationSelector → ConnectionResolver →
    WorkflowAssemblySpec → AssemblyCompiler.compile() → WorkflowDefinition.
    """
    from xueban.highlevel import (
        AssemblyCompiler,
        SpecRegistry,
        WorkflowAssemblyBlockSpec,
        WorkflowAssemblySpec,
        build_builtin_block_catalog,
    )
    from xueban.assembly.activation import ActivationSelector, ConnectionResolver
    from xueban.runtime.contracts import WorkflowDefinition

    # Set up a minimal registry with a generic agent
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

    registry = SpecRegistry.load_from_root(registry_root)
    catalog = build_builtin_block_catalog()

    # Phase 1: activate
    goal = "规划并执行任务"
    activation = ActivationSelector().select(goal, catalog)
    assert activation.complete is True

    # Phase 1: connect
    links = ConnectionResolver().resolve(activation.candidates)

    # Build WorkflowAssemblySpec — bind all agent-kind blocks to worker_agent
    assembly_blocks = []
    for c in activation.candidates:
        block_spec = catalog[c.block_id]
        agent_binding = "worker_agent" if block_spec.compile.node_kind == "agent" else None
        assembly_blocks.append(
            WorkflowAssemblyBlockSpec(id=c.block_id, ref=c.block_id, agent=agent_binding)
        )

    assembly = WorkflowAssemblySpec(
        assembly_id="e2e-test",
        name="e2e test assembly",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    # Phase 0: compile
    compiler = AssemblyCompiler(registry)
    workflow = compiler.compile(assembly)

    assert isinstance(workflow, WorkflowDefinition)
    assert workflow.workflow_id == "e2e-test"
    assert len(workflow.nodes) == len(activation.candidates)
    assert workflow.entrypoint is not None
