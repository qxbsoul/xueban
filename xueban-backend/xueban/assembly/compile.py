"""WorkflowAssemblySpec → WorkflowDefinition compile主链 (Story 3.4, AR42/AR43).

compile() does pure schema transformation — it does NOT execute or call any
runtime service. ActivationSelector / ConnectionResolver remain separate concerns
invoked only at run-time.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from xueban.runtime.contracts import (
    AgentSpec,
    ApprovalGateConfig,
    BlockDef,
    EdgeDefinition,
    NodeDefinition,
    WorkflowAssemblySpec,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
    WorkflowDefaults,
)
from xueban.runtime.errors import PolicyMismatch
from xueban.runtime.executors import ExecutorRegistry, UnknownExecutorError


class CompilationError(Exception):
    """Raised when template compilation fails (e.g. unregistered executor)."""


# ---------------------------------------------------------------------------
# Existing compile_agents helper (Story 2.1 AC#2) — kept as-is
# ---------------------------------------------------------------------------

def parse_agent_specs(raw: Dict[str, Any]) -> List[AgentSpec]:
    """Parse top-level `agents:` list from a raw YAML/dict into AgentSpec objects."""
    agents_raw = raw.get("agents", [])
    if not isinstance(agents_raw, list):
        raise CompilationError("'agents' must be a list")
    specs = []
    for item in agents_raw:
        if not isinstance(item, dict):
            raise CompilationError(f"each agent entry must be an object, got {type(item).__name__}")
        specs.append(AgentSpec.model_validate(item))
    return specs


def compile_agents(
    raw: Dict[str, Any],
    registry: ExecutorRegistry,
) -> List[AgentSpec]:
    """Parse agent specs and validate each against the registry."""
    specs = parse_agent_specs(raw)
    for spec in specs:
        try:
            registry.resolve(spec.kind, spec.provider)
        except UnknownExecutorError as exc:
            available_str = ", ".join(f"({k}, {p})" for k, p in exc.available) or "(none registered)"
            raise CompilationError(
                f"Agent '{spec.id}': executor kind={spec.kind!r} provider={spec.provider!r} "
                f"is not registered. Available: {available_str}"
            ) from exc
    return specs


# ---------------------------------------------------------------------------
# Story 3.4: WorkflowAssemblySpec → WorkflowDefinition
# ---------------------------------------------------------------------------

_BLOCK_KIND_TO_NODE_TYPE: Dict[str, str] = {
    "plan": "plan",
    "parallel": "control.parallel",
    "barrier": "control.barrier",
    "retry_gate": "retry_gate",
    "approval_gate": "approval_gate",
    "writeback": "writeback",
}


def _block_to_node(block: BlockDef, defaults: WorkflowDefaults) -> NodeDefinition:
    """Convert a BlockDef catalog entry into a NodeDefinition."""
    node_type = _BLOCK_KIND_TO_NODE_TYPE.get(block.kind, block.kind)
    config: Dict[str, Any] = dict(block.config)

    approval: Optional[ApprovalGateConfig] = None
    if block.kind == "approval_gate":
        approver = config.pop("approver", block.role)
        on_reject = config.pop("on_reject", "halt")
        on_approve = config.pop("on_approve", None)
        timeout_seconds = int(config.pop("timeout_seconds", defaults.timeout_seconds))
        approval = ApprovalGateConfig(
            approver=approver,
            on_reject=on_reject,
            on_approve=on_approve,
            timeout_seconds=timeout_seconds,
        )

    return NodeDefinition(
        id=block.id,
        kind="agent",
        type=node_type,
        approval=approval,
        config=config,
        metadata={**block.metadata, "role": block.role},
    )


def _validate_policy_consistency(
    nodes: List[NodeDefinition],
    policy_matrix: WorkflowPolicyMatrixSpec,
    warnings: List[str],
) -> None:
    """Validate policy matrix against node roles (compile-time check).

    Hard violations (raise PolicyMismatch):
    - reviewer_role equals subject_role (self-review)

    Soft violations (append to warnings):
    - target/sender not found among node ids
    """
    node_ids = {n.id for n in nodes}
    # Also collect roles stored in node metadata
    node_roles = {n.metadata.get("role") for n in nodes if n.metadata.get("role")}

    for reviewer, targets in policy_matrix.allow_reject.items():
        for target in targets:
            if reviewer == target:
                raise PolicyMismatch(
                    f"reviewer_role '{reviewer}' equals subject_role '{target}' — self-review is forbidden",
                    details={"reviewer": reviewer, "target": target},
                )
            if target not in node_ids and target not in node_roles and target != "END":
                warnings.append(
                    f"policy_matrix.allow_reject['{reviewer}'] → '{target}' "
                    f"is not a known node id or role"
                )

    for sender, receivers in policy_matrix.allow_send.items():
        for receiver in receivers:
            if receiver not in node_ids and receiver not in node_roles and receiver != "END":
                warnings.append(
                    f"policy_matrix.allow_send['{sender}'] → '{receiver}' "
                    f"is not a known node id or role"
                )


def compile(
    spec: WorkflowAssemblySpec,
) -> Tuple[WorkflowDefinition, List[str]]:
    """Compile a WorkflowAssemblySpec into a (WorkflowDefinition, warnings) tuple.

    Steps:
    1. Expand block_catalog into NodeDefinitions (AR24–29)
    2. Build stage × lane DAG edges (sequential within lane, barrier join across lanes)
    3. Validate policy consistency (raises PolicyMismatch on hard violations)
    4. Return (definition, warnings) — warnings are non-blocking suggestions

    Raises:
        PolicyMismatch: on hard policy violations (self-review, etc.)
        ValueError: on invalid spec structure
    """
    warnings: List[str] = []

    # Step 1 — expand block catalog into nodes
    nodes: List[NodeDefinition] = [
        _block_to_node(block, spec.defaults) for block in spec.block_catalog
    ]

    if not nodes:
        raise ValueError("WorkflowAssemblySpec must contain at least one block in block_catalog")

    node_ids = {n.id for n in nodes}
    edges: List[EdgeDefinition] = []

    # Step 2 — build stage × lane DAG
    # Blocks within a lane run sequentially; lanes within a stage run in parallel
    # with a synthetic control.parallel + control.barrier pair injected between stages.
    prev_stage_exit: Optional[str] = None

    for stage in spec.stages:
        non_empty_lanes = [l for l in stage.lanes if l.blocks]
        if not non_empty_lanes:
            continue

        if len(non_empty_lanes) == 1:
            lane = non_empty_lanes[0]
            stage_entry: Optional[str] = None
            prev_in_lane: Optional[str] = None
            for block_ref in lane.blocks:
                if block_ref not in node_ids:
                    warnings.append(
                        f"Stage '{stage.id}' lane '{lane.id}' references unknown block '{block_ref}'"
                    )
                    continue
                if stage_entry is None:
                    stage_entry = block_ref
                if prev_in_lane is not None:
                    edges.append(EdgeDefinition(**{"from": prev_in_lane, "to": block_ref}))
                prev_in_lane = block_ref

            if prev_stage_exit is not None and stage_entry is not None:
                edges.append(EdgeDefinition(**{"from": prev_stage_exit, "to": stage_entry}))
            prev_stage_exit = prev_in_lane  # last block in the lane

        else:
            # Multi-lane: inject synthetic parallel + barrier nodes
            parallel_id = f"{stage.id}__parallel"
            barrier_id = f"{stage.id}__barrier"
            branch_entries: List[str] = []

            for lane in non_empty_lanes:
                prev_in_lane2: Optional[str] = None
                for block_ref in lane.blocks:
                    if block_ref not in node_ids:
                        warnings.append(
                            f"Stage '{stage.id}' lane '{lane.id}' references unknown block '{block_ref}'"
                        )
                        continue
                    if prev_in_lane2 is None:
                        branch_entries.append(block_ref)
                    else:
                        edges.append(EdgeDefinition(**{"from": prev_in_lane2, "to": block_ref}))
                    prev_in_lane2 = block_ref

            parallel_node = NodeDefinition(
                id=parallel_id,
                kind="agent",
                type="control.parallel",
                config={"branches": branch_entries, "barrier": barrier_id},
            )
            barrier_node = NodeDefinition(
                id=barrier_id,
                kind="agent",
                type="control.barrier",
                config={"source_parallel": parallel_id},
            )
            nodes.extend([parallel_node, barrier_node])
            node_ids.update({parallel_id, barrier_id})

            if prev_stage_exit is not None:
                edges.append(EdgeDefinition(**{"from": prev_stage_exit, "to": parallel_id}))

            prev_stage_exit = barrier_id

    # Determine entrypoint — first block of first non-empty lane of first stage; else first node
    entrypoint = nodes[0].id
    for stage in spec.stages:
        for lane in stage.lanes:
            if lane.blocks and lane.blocks[0] in node_ids:
                entrypoint = lane.blocks[0]
                break
        else:
            continue
        break

    # Terminal edge
    if prev_stage_exit is not None:
        edges.append(EdgeDefinition(**{"from": prev_stage_exit, "to": "END", "type": "final"}))
    else:
        edges.append(EdgeDefinition(**{"from": entrypoint, "to": "END", "type": "final"}))

    # Step 3 — validate policy consistency
    policy_matrix = spec.policy_matrix
    if policy_matrix is not None:
        _validate_policy_consistency(nodes, policy_matrix, warnings)

    # Build WorkflowDefinition.
    # policy_matrix is NOT passed to WorkflowDefinition here because
    # WorkflowDefinition.validate_graph requires policy roles to match node ids,
    # but assembly-level roles are logical names. Store in metadata for transparency.
    definition = WorkflowDefinition(
        workflow_id=spec.workflow_id,
        version=spec.version,
        name=spec.name or spec.workflow_id,
        entrypoint=entrypoint,
        nodes=nodes,
        edges=edges,
        policy_matrix=None,
        defaults=spec.defaults.model_dump(),
        metadata={
            **spec.metadata,
            **({"assembly_policy_matrix": policy_matrix.model_dump()} if policy_matrix else {}),
        },
    )

    return definition, warnings
