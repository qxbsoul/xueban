"""
Tests for Step B: ConnectionResolver v2 capability-dependency graph inference.

Test plan from step-b-v2-topology-inference.md:
1. WorkflowBlockSpec.input_requirements field exists and defaults to []
2. 8 builtin blocks all have input_requirements
3. v2 linear inference: plan + execute → plan→execute→END
4. v2 linear inference: plan + execute + review → plan→execute→review→END
5. v2 fan-out: plan → execute, plan → review (both need planning)
6. v2 fan-in: execute + review → barrier
7. v2 with artifact: plan → execute → artifact → END
8. Isolated block: selected but no dependency → connects to END
9. Cycle detection: artificial cycle → raises ValueError
10. Backward compat: strategy="linear" unchanged
11. catalog=None fallback to v1 linear chain
12. Default resolve() call (no new params) still works
"""
from __future__ import annotations

import pytest

from xueban.assembly.activation import (
    CatalogActivationCandidate,
    ConnectionResolver,
)
from xueban.highlevel import WorkflowBlockSpec, build_builtin_block_catalog


# ---------------------------------------------------------------------------
# Task 1: input_requirements field
# ---------------------------------------------------------------------------

def test_input_requirements_field_exists_and_defaults_empty():
    spec = WorkflowBlockSpec(
        block_id="test",
        kind="worker",
        type="test",
        label="Test",
        compile={"node_kind": "node", "node_type": "test.run"},
    )
    assert spec.input_requirements == []


def test_input_requirements_can_be_set():
    spec = WorkflowBlockSpec(
        block_id="test",
        kind="worker",
        type="test",
        label="Test",
        compile={"node_kind": "node", "node_type": "test.run"},
        input_requirements=["planning", "execution"],
    )
    assert spec.input_requirements == ["planning", "execution"]


# ---------------------------------------------------------------------------
# Task 2: builtin blocks have input_requirements
# ---------------------------------------------------------------------------

def test_builtin_blocks_all_have_input_requirements_field():
    catalog = build_builtin_block_catalog()
    for bid, block in catalog.items():
        assert hasattr(block, "input_requirements"), f"{bid} missing input_requirements"


def test_builtin_plan_is_entry_point():
    catalog = build_builtin_block_catalog()
    assert catalog["plan"].input_requirements == []


def test_builtin_execute_requires_planning():
    catalog = build_builtin_block_catalog()
    assert "planning" in catalog["execute"].input_requirements


def test_builtin_review_requires_execution():
    catalog = build_builtin_block_catalog()
    assert "execution" in catalog["review"].input_requirements


def test_builtin_barrier_requires_parallelism():
    catalog = build_builtin_block_catalog()
    assert "parallelism" in catalog["barrier"].input_requirements


def test_builtin_artifact_requires_execution():
    catalog = build_builtin_block_catalog()
    assert "execution" in catalog["artifact"].input_requirements


def test_builtin_checkpoint_requires_execution():
    catalog = build_builtin_block_catalog()
    assert "execution" in catalog["checkpoint"].input_requirements


def test_builtin_delegate_requires_planning():
    catalog = build_builtin_block_catalog()
    assert "planning" in catalog["delegate"].input_requirements


# ---------------------------------------------------------------------------
# Task 3: v2 capability inference — linear cases
# ---------------------------------------------------------------------------

def test_v2_plan_execute_linear():
    """plan + execute → plan→execute→END"""
    catalog = build_builtin_block_catalog()
    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
        CatalogActivationCandidate(block_id="execute", matched_capabilities=["execution"]),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    assert ("plan", "execute") in edge_set
    assert ("execute", "END") in edge_set
    # plan has outgoing edge to execute, so it should NOT connect to END
    assert ("plan", "END") not in edge_set
    assert len(links) == 2


def test_v2_plan_execute_review_linear():
    """plan + execute + review → plan→execute→review→END"""
    catalog = build_builtin_block_catalog()
    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
        CatalogActivationCandidate(block_id="execute", matched_capabilities=["execution"]),
        CatalogActivationCandidate(block_id="review", matched_capabilities=["review"]),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    assert ("plan", "execute") in edge_set
    assert ("execute", "review") in edge_set
    assert ("review", "END") in edge_set


# ---------------------------------------------------------------------------
# Task 4: fan-out and fan-in
# ---------------------------------------------------------------------------

def _make_block(block_id, kind, caps, input_reqs):
    return WorkflowBlockSpec(
        block_id=block_id,
        kind=kind,
        type=block_id,
        label=block_id,
        compile={"node_kind": "node", "node_type": f"{block_id}.run"},
        capabilities=caps,
        input_requirements=input_reqs,
    )


def test_v2_fan_out_plan_to_execute_and_review():
    """plan fans out to execute AND review when review also needs planning."""
    catalog = {
        "plan": _make_block("plan", "worker", ["planning"], []),
        "execute": _make_block("execute", "worker", ["execution"], ["planning"]),
        "review": _make_block("review", "worker", ["review"], ["planning"]),
    }
    candidates = [
        CatalogActivationCandidate(block_id="plan"),
        CatalogActivationCandidate(block_id="execute"),
        CatalogActivationCandidate(block_id="review"),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    # fan-out: plan → execute, plan → review
    assert ("plan", "execute") in edge_set
    assert ("plan", "review") in edge_set
    # both execute and review are terminal → connect to END
    assert ("execute", "END") in edge_set
    assert ("review", "END") in edge_set


def test_v2_fan_in_barrier():
    """execute + review → barrier (fan-in)."""
    catalog = {
        "plan": _make_block("plan", "worker", ["planning"], []),
        "execute": _make_block("execute", "worker", ["execution"], ["planning"]),
        "review": _make_block("review", "worker", ["review"], ["planning"]),
        "barrier": _make_block("barrier", "control", ["synchronization"], ["execution", "review"]),
    }
    candidates = [
        CatalogActivationCandidate(block_id="plan"),
        CatalogActivationCandidate(block_id="execute"),
        CatalogActivationCandidate(block_id="review"),
        CatalogActivationCandidate(block_id="barrier"),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    # fan-out: plan → execute, plan → review
    assert ("plan", "execute") in edge_set
    assert ("plan", "review") in edge_set
    # fan-in: execute → barrier, review → barrier
    assert ("execute", "barrier") in edge_set
    assert ("review", "barrier") in edge_set
    # barrier is terminal
    assert ("barrier", "END") in edge_set
    # plan should NOT connect to END (it has outgoing edges)
    assert ("plan", "END") not in edge_set


# ---------------------------------------------------------------------------
# Task 5: edge cases
# ---------------------------------------------------------------------------

def test_v2_with_artifact():
    """plan → execute → artifact → END"""
    catalog = build_builtin_block_catalog()
    candidates = [
        CatalogActivationCandidate(block_id="plan"),
        CatalogActivationCandidate(block_id="execute"),
        CatalogActivationCandidate(block_id="artifact"),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    assert ("plan", "execute") in edge_set
    assert ("execute", "artifact") in edge_set
    assert ("artifact", "END") in edge_set


def test_v2_isolated_block_connects_to_end():
    """A block with unmet input_requirements is isolated → connects to END."""
    catalog = {
        "plan": _make_block("plan", "worker", ["planning"], []),
        "orphan": _make_block("orphan", "worker", ["magic"], ["quantum"]),
    }
    candidates = [
        CatalogActivationCandidate(block_id="plan"),
        CatalogActivationCandidate(block_id="orphan"),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    edge_set = {(l.from_id, l.to_id) for l in links}
    # Both are terminal (plan has no consumer, orphan has no provider)
    assert ("plan", "END") in edge_set
    assert ("orphan", "END") in edge_set


def test_v2_cycle_detection_raises():
    """Artificial cycle → ValueError."""
    catalog = {
        "a": _make_block("a", "worker", ["cap_a"], ["cap_b"]),
        "b": _make_block("b", "worker", ["cap_b"], ["cap_a"]),
    }
    candidates = [
        CatalogActivationCandidate(block_id="a"),
        CatalogActivationCandidate(block_id="b"),
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_backward_compat_strategy_linear():
    """strategy='linear' produces same result as v1."""
    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
        CatalogActivationCandidate(block_id="execute", matched_capabilities=["execution"]),
    ]
    links = ConnectionResolver().resolve(candidates, strategy="linear")
    assert len(links) == 2
    assert links[0].from_id == "plan"
    assert links[0].to_id == "execute"
    assert links[1].from_id == "execute"
    assert links[1].to_id == "END"


def test_backward_compat_no_catalog_fallback():
    """catalog=None falls back to v1 linear chain."""
    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
        CatalogActivationCandidate(block_id="execute", matched_capabilities=["execution"]),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=None, strategy="capability")
    # Should fallback to linear
    assert len(links) == 2
    assert links[0].from_id == "plan"
    assert links[0].to_id == "execute"


def test_backward_compat_default_call():
    """resolve(candidates) with no extra params still works (v1 behavior)."""
    candidates = [
        CatalogActivationCandidate(block_id="plan", matched_capabilities=["planning"]),
    ]
    links = ConnectionResolver().resolve(candidates)
    assert len(links) == 1
    assert links[0].from_id == "plan"
    assert links[0].to_id == "END"


def test_v2_empty_candidates():
    catalog = build_builtin_block_catalog()
    links = ConnectionResolver().resolve([], catalog=catalog, strategy="capability")
    assert links == []


def test_v2_single_entry_block():
    """Single block with no input_requirements → just connects to END."""
    catalog = build_builtin_block_catalog()
    candidates = [
        CatalogActivationCandidate(block_id="plan"),
    ]
    links = ConnectionResolver().resolve(candidates, catalog=catalog, strategy="capability")
    assert len(links) == 1
    assert links[0].from_id == "plan"
    assert links[0].to_id == "END"
