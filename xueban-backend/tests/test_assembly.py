"""Tests for WorkflowAssemblySpec → compile() main chain (Story 3.4)."""

from __future__ import annotations

import pytest

from xueban.assembly.compile import compile
from xueban.runtime.contracts import (
    BlockDef,
    LaneDef,
    StageDef,
    WorkflowAssemblySpec,
    WorkflowDefaults,
    WorkflowPolicyMatrixSpec,
)
from xueban.runtime.errors import PolicyMismatch


# ---------------------------------------------------------------------------
# Helper: build an Academic Paper WorkflowAssemblySpec in Python
# (mirrors the 6-role academic-paper template, Technical Success § 6/7)
# ---------------------------------------------------------------------------

def _academic_paper_spec() -> WorkflowAssemblySpec:
    """Create an Academic Paper assembly spec with all 6 block kinds (AR24–29)."""
    return WorkflowAssemblySpec(
        workflow_id="academic-paper",
        version="0.1",
        name="Academic Paper",
        block_catalog=[
            BlockDef(id="plan-outline", kind="plan", role="pi",
                     config={"prompt": "Draft research outline."}),
            BlockDef(id="write-sections", kind="parallel", role="section-writer",
                     config={"branches": ["write-intro", "write-methods", "write-results"],
                             "barrier": "review-barrier"}),
            BlockDef(id="write-intro",   kind="plan", role="section-writer",
                     config={"prompt": "Write introduction."}),
            BlockDef(id="write-methods", kind="plan", role="section-writer",
                     config={"prompt": "Write methods."}),
            BlockDef(id="write-results", kind="plan", role="section-writer",
                     config={"prompt": "Write results."}),
            BlockDef(id="review-barrier", kind="barrier", role="pi",
                     config={"source_parallel": "write-sections"}),
            BlockDef(id="retry-revision", kind="retry_gate", role="section-writer",
                     config={"max_retries": 2}),
            BlockDef(id="pi-approval", kind="approval_gate", role="pi",
                     config={"approver": "pi", "on_reject": "retry",
                             "timeout_seconds": 600}),
            BlockDef(id="submit-archive", kind="writeback", role="submission-manager",
                     config={"target": "docs", "mode": "reference"}),
        ],
        stages=[
            StageDef(id="planning", name="Research Planning", lanes=[
                LaneDef(id="pi-lane", role="pi", blocks=["plan-outline"]),
            ]),
            StageDef(id="writing", name="Parallel Writing", lanes=[
                LaneDef(id="intro-lane",    role="section-writer", blocks=["write-intro"]),
                LaneDef(id="methods-lane",  role="section-writer", blocks=["write-methods"]),
                LaneDef(id="results-lane",  role="section-writer", blocks=["write-results"]),
            ]),
            StageDef(id="review", name="Review & Gate", lanes=[
                LaneDef(id="review-lane", role="pi", blocks=["retry-revision", "pi-approval"]),
            ]),
            StageDef(id="submission", name="Submission", lanes=[
                LaneDef(id="submit-lane", role="submission-manager", blocks=["submit-archive"]),
            ]),
        ],
        policy_matrix=WorkflowPolicyMatrixSpec(
            allow_send={"pi": ["section-writer", "citation-reviewer", "method-reviewer"]},
            allow_reject={"pi": ["section-writer", "editorial-polisher"]},
        ),
    )


# ---------------------------------------------------------------------------
# T4 tests
# ---------------------------------------------------------------------------

def test_compile_academic_paper_happy_path():
    """Happy path: academic-paper spec compiles to a WorkflowDefinition with
    all 6 Workflow Block kinds present (AR24–29, Technical Success § 6)."""
    spec = _academic_paper_spec()
    definition, warnings = compile(spec)

    assert definition.workflow_id == "academic-paper"
    assert definition.entrypoint

    node_types = {n.type for n in definition.nodes}
    assert "plan" in node_types,          f"missing plan; got {node_types}"
    assert "control.parallel" in node_types, f"missing parallel; got {node_types}"
    assert "control.barrier" in node_types,  f"missing barrier; got {node_types}"
    assert "retry_gate" in node_types,    f"missing retry_gate; got {node_types}"
    assert "approval_gate" in node_types, f"missing approval_gate; got {node_types}"
    assert "writeback" in node_types,     f"missing writeback; got {node_types}"

    assert definition.nodes
    assert definition.edges


def test_compile_academic_paper_has_terminal_edge():
    """The compiled graph must have at least one edge ending at END."""
    spec = _academic_paper_spec()
    definition, _ = compile(spec)
    terminal_edges = [e for e in definition.edges if e.to_id == "END"]
    assert terminal_edges, "expected at least one edge to END"


def test_policy_consistency_detect_self_review():
    """reviewer_role == subject_role → PolicyMismatch is raised (blocking error)."""
    spec = WorkflowAssemblySpec(
        workflow_id="self-review-test",
        block_catalog=[
            BlockDef(id="writer", kind="plan", role="writer"),
        ],
        stages=[
            StageDef(
                id="s1",
                name="Writing",
                lanes=[LaneDef(id="l1", role="writer", blocks=["writer"])],
            )
        ],
        policy_matrix=WorkflowPolicyMatrixSpec(
            allow_reject={"writer": ["writer"]},  # self-review!
        ),
    )

    with pytest.raises(PolicyMismatch) as exc_info:
        compile(spec)

    assert "self-review" in str(exc_info.value).lower()


def test_compile_warnings_non_blocking():
    """Policy matrix referencing unknown node ids → warnings returned, definition still built."""
    spec = WorkflowAssemblySpec(
        workflow_id="warnings-test",
        block_catalog=[
            BlockDef(id="planner", kind="plan", role="pi"),
        ],
        stages=[
            StageDef(
                id="s1",
                name="Planning",
                lanes=[LaneDef(id="l1", role="pi", blocks=["planner"])],
            )
        ],
        policy_matrix=WorkflowPolicyMatrixSpec(
            allow_send={"pi": ["ghost-role"]},  # ghost-role does not exist
        ),
    )

    definition, warnings = compile(spec)

    assert definition is not None
    assert any("ghost-role" in w for w in warnings), f"expected ghost-role warning; got {warnings}"


def test_compile_single_block_no_stages():
    """A spec with blocks but no stages compiles to a definition with a terminal edge."""
    spec = WorkflowAssemblySpec(
        workflow_id="minimal",
        block_catalog=[
            BlockDef(id="step1", kind="plan", role="agent"),
        ],
    )
    definition, warnings = compile(spec)
    assert definition.entrypoint == "step1"
    assert any(e.to_id == "END" for e in definition.edges)


def test_compile_approval_gate_generates_approval_config():
    """approval_gate block must produce a NodeDefinition with .approval set."""
    spec = WorkflowAssemblySpec(
        workflow_id="gate-test",
        block_catalog=[
            BlockDef(
                id="gate",
                kind="approval_gate",
                role="pi",
                config={"approver": "pi", "on_reject": "halt", "timeout_seconds": 120},
            ),
        ],
    )
    definition, _ = compile(spec)
    gate_node = next(n for n in definition.nodes if n.id == "gate")
    assert gate_node.approval is not None
    assert gate_node.approval.approver == "pi"
    assert gate_node.approval.timeout_seconds == 120


def test_compile_multi_lane_stage_injects_parallel_barrier():
    """A stage with 2 lanes must inject synthetic parallel + barrier nodes."""
    spec = WorkflowAssemblySpec(
        workflow_id="parallel-test",
        block_catalog=[
            BlockDef(id="write-a", kind="plan", role="writer-a"),
            BlockDef(id="write-b", kind="plan", role="writer-b"),
        ],
        stages=[
            StageDef(
                id="writing",
                name="Writing",
                lanes=[
                    LaneDef(id="lane-a", role="writer-a", blocks=["write-a"]),
                    LaneDef(id="lane-b", role="writer-b", blocks=["write-b"]),
                ],
            )
        ],
    )
    definition, _ = compile(spec)
    node_ids = {n.id for n in definition.nodes}
    assert "writing__parallel" in node_ids
    assert "writing__barrier" in node_ids


# ---------------------------------------------------------------------------
# T5: Integration — compile() output is structurally compatible with RuntimeRequest
# ---------------------------------------------------------------------------

def test_compile_output_compatible_with_runtime_request():
    """T5: compiled WorkflowDefinition can be wrapped in RuntimeRequest without validation errors.

    This verifies the structural contract between compile() output and the runtime
    layer (WorkflowDefinition → RuntimeRequest). Full execution with mock LLM is
    deferred to test_runtime_contract.py once plan/barrier executor types are registered.
    """
    from xueban.runtime.contracts import RuntimeRequest

    spec = WorkflowAssemblySpec(
        workflow_id="t5-integration",
        version="0.1",
        name="T5 Integration Test",
        block_catalog=[
            BlockDef(id="plan-step", kind="plan", role="agent",
                     config={"prompt": "Analyze the topic."}),
            BlockDef(id="writeback-step", kind="writeback", role="agent",
                     config={"target": "docs", "mode": "reference"}),
        ],
        stages=[
            StageDef(id="stage1", name="Analysis", lanes=[
                LaneDef(id="lane1", role="agent", blocks=["plan-step", "writeback-step"]),
            ]),
        ],
    )

    definition, warnings = compile(spec)

    # Definition must be structurally valid (validate_graph passes)
    assert definition.workflow_id == "t5-integration"
    assert definition.entrypoint == "plan-step"
    assert any(e.to_id == "END" for e in definition.edges), "must have terminal edge"

    # Can be wrapped in RuntimeRequest (Pydantic model_validate must succeed)
    request = RuntimeRequest(workflow=definition, input={"goal": "Analyse topic"})
    assert request.workflow.workflow_id == "t5-integration"
    assert request.workflow.entrypoint == "plan-step"

    # defaults are fully populated from spec.defaults.model_dump() (P2-α fix)
    assert isinstance(definition.defaults, dict)
    assert "timeout_seconds" in definition.defaults
