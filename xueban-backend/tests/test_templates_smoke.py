"""Smoke tests for 6 seed templates (Story 3.6 T8).

Tests:
1. All 6 template YAMLs load as WorkflowTemplateSpec without error.
2. Academic Paper assembly spec compiles to a WorkflowDefinition with all 6 block kinds.
3. A minimal compiled definition from the assembly spec can be run end-to-end
   via RuntimeService.run() without exceptions.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import List

import pytest
import yaml

from xueban.assembly.compile import compile as assembly_compile
from xueban.highlevel import WorkflowTemplateSpec
from xueban.runtime import RuntimeRequest, RuntimeService
from xueban.runtime.contracts import (
    BlockDef,
    LaneDef,
    StageDef,
    WorkflowAssemblySpec,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
)

TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"

SEED_TEMPLATE_FILES = [
    "solo-company.yaml",
    "academic-paper.yaml",
    "newsroom.yaml",
    "modern-startup.yaml",
    "consulting.yaml",
    "blank.yaml",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _academic_paper_assembly_spec() -> WorkflowAssemblySpec:
    """Build the Academic Paper WorkflowAssemblySpec (6 block kinds)."""
    return WorkflowAssemblySpec(
        workflow_id="academic-paper",
        version="0.1",
        name="Academic Paper",
        block_catalog=[
            BlockDef(id="plan-outline", kind="plan", role="pi",
                     config={"prompt": "Draft research outline."}),
            BlockDef(id="write-intro",   kind="plan", role="section-writer",
                     config={"prompt": "Write introduction."}),
            BlockDef(id="write-methods", kind="plan", role="section-writer",
                     config={"prompt": "Write methods."}),
            BlockDef(id="write-results", kind="plan", role="section-writer",
                     config={"prompt": "Write results."}),
            BlockDef(id="retry-revision", kind="retry_gate", role="section-writer",
                     config={"max_retries": 2}),
            BlockDef(id="pi-approval", kind="approval_gate", role="pi",
                     config={"approver": "pi", "on_reject": "retry", "timeout_seconds": 10}),
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
            allow_send={"pi": ["section-writer"]},
            allow_reject={"pi": ["section-writer"]},
        ),
    )


# ---------------------------------------------------------------------------
# AC1 — 6 template files load as WorkflowTemplateSpec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", SEED_TEMPLATE_FILES)
def test_seed_template_yaml_loads(filename: str):
    """Each template YAML must exist and parse as WorkflowTemplateSpec."""
    path = TEMPLATES_DIR / filename
    assert path.exists(), f"Seed template missing: {filename}"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = WorkflowTemplateSpec.model_validate(raw)
    assert spec.template_id, f"{filename}: template_id must be set"
    assert spec.name, f"{filename}: name must be set"


# ---------------------------------------------------------------------------
# AC1 — Academic Paper assembly spec: all 6 block kinds present
# ---------------------------------------------------------------------------

def test_academic_paper_assembly_hits_all_6_block_kinds():
    """Academic Paper compiled from WorkflowAssemblySpec must include all 6 block
    kinds (AR24–29, Technical Success § 6)."""
    spec = _academic_paper_assembly_spec()
    definition, warnings = assembly_compile(spec)

    node_types = {n.type for n in definition.nodes}

    assert "plan" in node_types,             f"missing plan; got {node_types}"
    assert "control.parallel" in node_types, f"missing parallel; got {node_types}"
    assert "control.barrier" in node_types,  f"missing barrier; got {node_types}"
    assert "retry_gate" in node_types,       f"missing retry_gate; got {node_types}"
    assert "approval_gate" in node_types,    f"missing approval_gate; got {node_types}"
    assert "writeback" in node_types,        f"missing writeback; got {node_types}"


def test_academic_paper_assembly_compile_chain_runs():
    """compile() → RuntimeService.run() chain completes without exception (AR42/AR43)."""
    spec = _academic_paper_assembly_spec()
    definition, _warnings = assembly_compile(spec)

    service = RuntimeService()
    result = asyncio.run(
        service.run(RuntimeRequest(workflow=definition, input={"goal": "Write a paper on ML"}))
    )
    # approval_gate pauses for human approval; paused/awaiting_approval are valid terminal states
    assert result.run.status in {"succeeded", "waiting", "checkpointed", "paused", "awaiting_approval"}


# ---------------------------------------------------------------------------
# T8 — Blank template smoke run
# ---------------------------------------------------------------------------

def test_blank_template_has_at_least_one_agent():
    """blank.yaml must have the agent_1 node declared so it can serve as a scaffold.

    Note: blank.yaml intentionally has agent_roster=[] (0 roster entries per Story 3.6.7
    spec); the scaffold agent lives in spec.agents (flow graph). We assert spec.agents
    because len(spec.agent_roster) >= 0 is trivially True for any list, including empty.
    """
    path = TEMPLATES_DIR / "blank.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = WorkflowTemplateSpec.model_validate(raw)
    # blank template has 1 agent spec in agents list (not agent_roster)
    assert len(spec.agents) >= 1, (
        "blank.yaml must declare at least one agent in `agents` so it can run as a scaffold"
    )


def test_academic_paper_template_has_6_roster_entries():
    """academic-paper.yaml must have exactly 6 agent roster entries."""
    path = TEMPLATES_DIR / "academic-paper.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = WorkflowTemplateSpec.model_validate(raw)
    assert len(spec.agent_roster) == 6


def test_solo_company_template_has_policy_matrix():
    """solo-company.yaml must have a policy_matrix (for AC2 rejection scenario)."""
    path = TEMPLATES_DIR / "solo-company.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = WorkflowTemplateSpec.model_validate(raw)
    # policy_matrix.agents should be populated (at least empty dict is fine)
    assert spec.policy_matrix is not None
