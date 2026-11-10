from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


WRITEBACK_TARGETS = {"host", "docs", "memory", "graph"}
ARTIFACT_WRITEBACK_MODES = {"reference", "inline"}
CHECKPOINT_WRITEBACK_MODES = {"reference"}


def _validate_writeback_channel_config(channel: str, config: Any, scope: str) -> None:
    if not isinstance(config, dict):
        raise ValueError(f"{scope} writeback.{channel} must be an object")

    target = config.get("target", "host")
    mode = config.get("mode", "reference")

    if target not in WRITEBACK_TARGETS:
        raise ValueError(f"{scope} writeback.{channel}.target must be one of {sorted(WRITEBACK_TARGETS)}")

    valid_modes = ARTIFACT_WRITEBACK_MODES if channel == "artifact" else CHECKPOINT_WRITEBACK_MODES
    if mode not in valid_modes:
        raise ValueError(f"{scope} writeback.{channel}.mode must be one of {sorted(valid_modes)}")


def validate_writeback_bundle(bundle: Any, scope: str) -> None:
    if bundle is None:
        return
    if not isinstance(bundle, dict):
        raise ValueError(f"{scope} writeback must be an object")

    for channel in bundle:
        if channel not in {"artifact", "checkpoint"}:
            raise ValueError(f"{scope} writeback supports only artifact/checkpoint channels")
        _validate_writeback_channel_config(channel, bundle[channel], scope)


class ApprovalGateConfig(BaseModel):
    """approval_gate 节点的专属配置。"""

    approver: str
    on_reject: Literal["retry", "halt", "branch"] = "halt"
    on_approve: Optional[str] = None
    timeout_seconds: int = Field(default=300, gt=0)


class NodeDefinition(BaseModel):
    id: str
    kind: Literal["agent", "node"] = "agent"
    type: str
    approval: Optional[ApprovalGateConfig] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    retry_policy: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_approval_gate(self) -> "NodeDefinition":
        if self.type == "approval_gate" and self.approval is None:
            raise ValueError("approval_gate node must define an 'approval' config block")
        return self


class EdgeDefinition(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    type: Literal["default", "conditional", "final"] = "default"
    condition: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class WorkflowPolicyMatrixSpec(BaseModel):
    """第 8 核心对象：工作流治理规则矩阵（compile-time 非阻塞校验）。"""

    allow_send: Dict[str, List[str]] = Field(default_factory=dict)
    allow_reject: Dict[str, List[str]] = Field(default_factory=dict)
    description: Optional[str] = None
    version: str = "1.0"

    def all_referenced_role_ids(self) -> set:
        """返回矩阵中引用的全部 role id 集合，供外部校验层使用。"""
        refs: set = set()
        for sender, receivers in self.allow_send.items():
            refs.add(sender)
            refs.update(receivers)
        for reviewer, targets in self.allow_reject.items():
            refs.add(reviewer)
            refs.update(targets)
        return refs

    @model_validator(mode="after")
    def validate_structure(self) -> "WorkflowPolicyMatrixSpec":
        for sender, receivers in self.allow_send.items():
            if not isinstance(receivers, list):
                raise ValueError(f"allow_send[{sender!r}] must be a list of role ids")
            self.allow_send[sender] = list(dict.fromkeys(receivers))
        for reviewer, targets in self.allow_reject.items():
            if not isinstance(targets, list):
                raise ValueError(f"allow_reject[{reviewer!r}] must be a list of role ids")
            self.allow_reject[reviewer] = list(dict.fromkeys(targets))
        return self


# Open executor-kind registry (P2 seam): the workflow schema validates kinds
# against this set instead of a hardcoded whitelist. ExecutorRegistry.register
# extends it automatically; custom executors may also register a kind directly.
EXECUTOR_KINDS: set = {"api", "cli", "agentteams", "rag"}


def register_executor_kind(kind: str) -> None:
    """Register a new executor kind so workflow validation accepts it."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("executor kind must be a non-empty string")
    EXECUTOR_KINDS.add(kind)


class WorkflowDefinition(BaseModel):
    workflow_id: str
    version: str
    name: str
    entrypoint: str
    nodes: List[NodeDefinition]
    edges: List[EdgeDefinition]
    policy_matrix: Optional[WorkflowPolicyMatrixSpec] = None
    defaults: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        validate_writeback_bundle(self.defaults.get("writeback"), "workflow defaults")
        node_ids = {node.id for node in self.nodes}
        nodes_by_id = {node.id: node for node in self.nodes}
        outgoing_edges: Dict[str, List[EdgeDefinition]] = {}
        incoming_edges: Dict[str, List[EdgeDefinition]] = {}
        if not self.nodes:
            raise ValueError("workflow must define at least one node")
        if self.entrypoint not in node_ids:
            raise ValueError("entrypoint must reference an existing node")

        for edge in self.edges:
            if edge.from_id not in node_ids:
                raise ValueError(f"edge.from references unknown node: {edge.from_id}")
            if edge.to_id not in node_ids and edge.to_id != "END":
                raise ValueError(f"edge.to references unknown node: {edge.to_id}")
            outgoing_edges.setdefault(edge.from_id, []).append(edge)
            incoming_edges.setdefault(edge.to_id, []).append(edge)

        for node in self.nodes:
            artifact_config = node.config.get("artifact")
            if isinstance(artifact_config, dict) and "writeback" in artifact_config:
                _validate_writeback_channel_config("artifact", artifact_config["writeback"], f"node {node.id} artifact")

            executor_config = node.config.get("executor")
            if executor_config is not None:
                if not isinstance(executor_config, dict):
                    raise ValueError(f"node {node.id} executor must be an object")
                executor_kind = executor_config.get("kind")
                if executor_kind not in EXECUTOR_KINDS:
                    raise ValueError(
                        f"node {node.id} executor.kind must be one of {sorted(EXECUTOR_KINDS)} "
                        "(extend via register_executor_kind)"
                    )

            delegated_config = node.config.get("delegated")
            if delegated_config is not None:
                if not isinstance(delegated_config, dict):
                    raise ValueError(f"node {node.id} delegated must be an object")
                delegated_workflow = delegated_config.get("workflow")
                if not isinstance(delegated_workflow, dict):
                    raise ValueError(f"node {node.id} delegated.workflow must be an object")
                WorkflowDefinition.model_validate(delegated_workflow)
                context_mode = delegated_config.get("context_mode", "inherit")
                if context_mode not in {"inherit", "isolate"}:
                    raise ValueError(f"node {node.id} delegated.context_mode must be one of ['inherit', 'isolate']")
                delegated_input = delegated_config.get("input")
                if delegated_input is not None and not isinstance(delegated_input, dict):
                    raise ValueError(f"node {node.id} delegated.input must be an object")
                delegated_context = delegated_config.get("context")
                if delegated_context is not None and not isinstance(delegated_context, dict):
                    raise ValueError(f"node {node.id} delegated.context must be an object")

            if node.type != "control.parallel":
                continue

            branches = node.config.get("branches", [])
            barrier_id = node.config.get("barrier")
            if not isinstance(branches, list) or not branches:
                raise ValueError(f"parallel node {node.id} must define a non-empty config.branches list")
            if len(branches) != len(set(branches)):
                raise ValueError(f"parallel node {node.id} branches must be unique")
            if not isinstance(barrier_id, str) or barrier_id not in node_ids:
                raise ValueError(f"parallel node {node.id} must define config.barrier referencing an existing node")

            barrier_node = nodes_by_id[barrier_id]
            if barrier_node.type != "control.barrier":
                raise ValueError(f"parallel node {node.id} barrier must reference a control.barrier node")
            if outgoing_edges.get(node.id):
                raise ValueError(f"parallel node {node.id} must not define outgoing edges; barrier continues the flow")

            for branch_id in branches:
                if branch_id not in node_ids:
                    raise ValueError(f"parallel node {node.id} branch references unknown node: {branch_id}")
                if branch_id == node.id or branch_id == barrier_id:
                    raise ValueError(f"parallel node {node.id} branch cannot reference itself or its barrier")
                if outgoing_edges.get(branch_id):
                    raise ValueError(
                        f"parallel branch node {branch_id} must not define outgoing edges in Phase 1 fan-out mode"
                    )

        for node in self.nodes:
            if node.type != "control.barrier":
                continue

            source_parallel = node.config.get("source_parallel")
            if source_parallel is not None:
                if source_parallel not in node_ids:
                    raise ValueError(f"barrier node {node.id} source_parallel references unknown node: {source_parallel}")
                if nodes_by_id[source_parallel].type != "control.parallel":
                    raise ValueError(f"barrier node {node.id} source_parallel must reference a control.parallel node")
            if incoming_edges.get(node.id):
                raise ValueError(
                    f"barrier node {node.id} must not define incoming edges in Phase 1 fan-out mode"
                )

        if self.policy_matrix is not None:
            invalid_roles = self.policy_matrix.all_referenced_role_ids() - node_ids
            if invalid_roles:
                raise ValueError(
                    f"policy_matrix references undeclared role ids: {sorted(invalid_roles)}"
                )

        for node in self.nodes:
            if node.type != "control.loop":
                continue

            # Loop body v2 (loop-engine-design §8): the body is a subgraph of
            # this DAG — config.subgraph declares the body node set and the
            # workflow's own edges between those nodes drive execution order
            # (conditions and back edges included). config.preset expands a
            # named template from the open preset registry (PDCA is a bundled
            # example, not an engine concept); config.entry overrides the DAG
            # body's walk start.
            preset = node.config.get("preset")
            subgraph = node.config.get("subgraph")
            if preset is not None:
                if not isinstance(preset, str) or not preset:
                    raise ValueError(f"loop node {node.id} config.preset must be a non-empty string")
            if not isinstance(subgraph, list) or not subgraph:
                if preset is None:
                    raise ValueError(
                        f"loop node {node.id} must define a non-empty config.subgraph list "
                        "(or a config.preset body template)"
                    )
            else:
                if len(subgraph) != len(set(subgraph)):
                    raise ValueError(f"loop node {node.id} subgraph entries must be unique")
                for body_id in subgraph:
                    if not isinstance(body_id, str) or body_id not in node_ids:
                        raise ValueError(
                            f"loop node {node.id} subgraph references unknown node: {body_id}"
                        )
                    if body_id == node.id:
                        raise ValueError(f"loop node {node.id} subgraph cannot reference itself")
                entry = node.config.get("entry")
                if entry is not None:
                    if not isinstance(entry, str) or entry not in subgraph:
                        raise ValueError(
                            f"loop node {node.id} config.entry must reference a subgraph node"
                        )
            if node.config.get("kind") not in {"while", "foreach", "until_converge", "fixed_count"}:
                raise ValueError(
                    f"loop node {node.id} config.kind must be one of "
                    "['while','foreach','until_converge','fixed_count']"
                )
            budget = node.config.get("iteration_budget", 1)
            if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
                raise ValueError(
                    f"loop node {node.id} config.iteration_budget must be a positive integer"
                )

        return self


class RuntimeRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: f"req-{uuid4().hex[:12]}")
    workflow: WorkflowDefinition
    input: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    memory_scope: Literal["session", "user", "global"] = "session"
    execution_mode: Literal["sync", "async"] = "sync"
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def apply_idempotency_key(self) -> "RuntimeRequest":
        if self.idempotency_key is None:
            self.idempotency_key = self.request_id
        validate_writeback_bundle(self.metadata.get("writeback"), "runtime request metadata")
        return self


class ChildRunRequest(BaseModel):
    workflow: WorkflowDefinition
    input: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    memory_scope: Literal["session", "user", "global"] = "session"
    execution_mode: Literal["sync", "async"] = "sync"
    idempotency_key: Optional[str] = None
    context_mode: Literal["inherit", "isolate"] = "inherit"
    parent_step_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    task_title: Optional[str] = None
    handoff_goal: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def apply_idempotency_key(self) -> "ChildRunRequest":
        if self.idempotency_key is None:
            self.idempotency_key = f"child-{uuid4().hex[:12]}"
        validate_writeback_bundle(self.metadata.get("writeback"), "child run request metadata")
        return self


class TaskRecord(BaseModel):
    task_id: str
    run_id: str
    root_task_id: str
    parent_task_id: Optional[str] = None
    title: Optional[str] = None
    focus: Optional[str] = None
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "waiting", "waiting_user", "awaiting_approval", "invalidated"] = "accepted"
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    request_id: str
    workflow_id: str
    task_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    status: Literal["accepted", "validated", "running", "succeeded", "failed", "cancelled", "checkpointed", "waiting", "waiting_user", "awaiting_approval", "paused"]
    started_at: datetime
    ended_at: Optional[datetime] = None
    entrypoint: str
    current_step_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WritebackRef(BaseModel):
    channel: Literal["artifact", "checkpoint"]
    target: Literal["host", "docs", "memory", "graph"] = "host"
    mode: Literal["reference", "inline"] = "reference"
    host_action: Literal["persist_artifact_ref", "persist_checkpoint_ref"]
    content_field: Optional[str] = None
    resume_supported: Optional[bool] = None
    next_node_id: Optional[str] = None


class ArtifactRef(BaseModel):
    artifact_id: str
    kind: Literal["text", "json", "document", "report", "patch", "log"] = "json"
    name: str
    uri: str
    producer_step_id: str
    writeback: WritebackRef
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loop Engine contracts (LE-0) — cybernetic loop / PDCA fractal / iterations.
# Fills RUNTIME_CONTRACT_SPEC §5.3 (有限循环/重试节点) and the "真正循环恢复"
# gap listed under WORKFLOW_SCHEMA §6. See docs/architecture/loop-engine-design.md.
# ---------------------------------------------------------------------------

LoopKind = Literal["while", "foreach", "until_converge", "fixed_count"]


class LoopFrame(BaseModel):
    """One frame on the loop call-stack; pushed when a control.loop body iterates."""

    loop_id: str
    index: int = 0
    local_visited: List[str] = Field(default_factory=list)
    setpoint: Dict[str, Any] = Field(default_factory=dict)
    iteration_ref: Optional[str] = None
    depth: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class IterationRecord(BaseModel):
    """Evidence for a single loop iteration — the atom of causal attribution."""

    iteration_id: str = Field(default_factory=lambda: f"iter-{uuid4().hex[:12]}")
    loop_id: str
    run_id: str
    index: int
    evidence: Dict[str, Any] = Field(default_factory=dict)
    changed_factors: List[str] = Field(default_factory=list)
    metrics: Dict[str, float] = Field(default_factory=dict)
    verifier_score: Optional[float] = None
    validated_by: Optional[str] = None
    trace_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LoopRecord(BaseModel):
    """First-class loop object; parent_loop_id forms the fractal nesting tree."""

    loop_id: str = Field(default_factory=lambda: f"loop-{uuid4().hex[:12]}")
    run_id: str
    parent_loop_id: Optional[str] = None
    kind: LoopKind = "until_converge"
    setpoint: Dict[str, Any] = Field(default_factory=dict)
    # Invariant (enforced by the LE-1 runtime write path): iteration_count ==
    # len(iterations). Kept as a denormalized summary for cheap reads; the
    # runtime must append to `iterations` and bump `iteration_count` in the
    # same atomic write so causal-attribution / convergence consumers never
    # see a stale count.
    iteration_count: int = 0
    iterations: List[IterationRecord] = Field(default_factory=list)
    status: Literal["running", "converged", "oscillating", "diverged", "halted"] = "running"
    sensor_binding: Optional[str] = None
    actuator_binding: Optional[str] = None
    depth: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CheckpointState(BaseModel):
    current_node_id: Optional[str] = None
    next_node_id: Optional[str] = None
    visited_nodes: List[str] = Field(default_factory=list)
    last_output: Dict[str, Any] = Field(default_factory=dict)
    state: Dict[str, Any] = Field(default_factory=dict)
    loop_frames: List[LoopFrame] = Field(default_factory=list)


class CheckpointRef(BaseModel):
    checkpoint_id: str
    run_id: str
    step_id: Optional[str] = None
    state_ref: Optional[str] = None
    state: CheckpointState
    created_at: datetime = Field(default_factory=utc_now)
    writeback: WritebackRef
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StepRecord(BaseModel):
    step_id: str
    run_id: str
    node_id: str
    status: Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled", "invalidated"]
    index: int
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts: List[ArtifactRef] = Field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    attempt_number: int = 1
    iteration_ref: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActivationRecord(BaseModel):
    activation_id: str
    run_id: str
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    node_id: str
    mode: Literal["always", "local", "manual"] = "always"
    decision: Literal["activated", "suppressed", "deferred"] = "activated"
    tags: List[str] = Field(default_factory=list)
    activate_when: List[str] = Field(default_factory=list)
    suppress_when: List[str] = Field(default_factory=list)
    delegate_candidates: List[str] = Field(default_factory=list)
    subgoal_triggers: List[str] = Field(default_factory=list)
    retry_gates: List[str] = Field(default_factory=list)
    review_gates: List[str] = Field(default_factory=list)
    feedback_channels: List[str] = Field(default_factory=list)
    signal_sources: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActivationCandidate(BaseModel):
    candidate_id: str
    run_id: str
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    node_id: str
    candidate_type: Literal["node", "agent", "delegate_target", "subgoal"] = "node"
    candidate_ref: str
    source_signals: List[str] = Field(default_factory=list)
    score: float = 1.0
    selected: bool = False
    suppressed_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutionFeedbackRecord(BaseModel):
    feedback_id: str
    run_id: str
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    node_id: Optional[str] = None
    source_type: Literal["step", "artifact", "checkpoint", "handoff", "run"] = "step"
    status: Literal["observed", "succeeded", "failed", "triggered", "suppressed"] = "observed"
    summary: str
    signals: Dict[str, Any] = Field(default_factory=dict)
    reward_hints: Dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HandoffRef(BaseModel):
    handoff_id: str
    run_id: str
    from_step_id: str
    from_node_id: str
    to_node_id: Optional[str] = None
    goal: Optional[str] = None
    artifact_ids: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MemoryEvent(BaseModel):
    event_id: str
    run_id: str
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    category: Literal[
        "task",
        "step_result",
        "artifact",
        "checkpoint",
        "handoff",
        "activation",
        "feedback_signal",
        "run_summary",
    ] = "step_result"
    summary: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    run: RunRecord
    tasks: List[TaskRecord] = Field(default_factory=list)
    steps: List[StepRecord] = Field(default_factory=list)
    final_output: Dict[str, Any] = Field(default_factory=dict)
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts: List[ArtifactRef] = Field(default_factory=list)
    checkpoints: List[CheckpointRef] = Field(default_factory=list)
    handoffs: List[HandoffRef] = Field(default_factory=list)
    activation_candidates: List[ActivationCandidate] = Field(default_factory=list)
    activations: List[ActivationRecord] = Field(default_factory=list)
    feedback: List[ExecutionFeedbackRecord] = Field(default_factory=list)
    memory_events: List[MemoryEvent] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class PolicyWarning(BaseModel):
    code: str
    pattern: str
    reason: str
    reference_url: str = ""


class WorkflowValidationResult(BaseModel):
    valid: bool
    workflow_id: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    policy_warnings: List[PolicyWarning] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    checkpoint_id: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run_id: str
    request_id: str
    workflow_id: str
    status: Literal["accepted", "validated", "running", "succeeded", "failed", "cancelled", "checkpointed", "waiting", "waiting_user", "awaiting_approval", "paused"]
    started_at: datetime
    ended_at: Optional[datetime] = None
    current_step_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProjectionScope(BaseModel):
    workflow_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    artifact_id: Optional[str] = None
    checkpoint_id: Optional[str] = None


class ProjectionNodeTimestamps(BaseModel):
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ProjectionNode(BaseModel):
    id: str
    entity_type: Literal[
        "workflow_node",
        "run",
        "task",
        "step",
        "artifact",
        "checkpoint",
        "memory_event",
        "handoff",
        "activation_candidate",
        "activation",
        "feedback_signal",
    ]
    label: str
    status: Optional[str] = None
    parent_id: Optional[str] = None
    refs: Dict[str, Any] = Field(default_factory=dict)
    timestamps: ProjectionNodeTimestamps = Field(default_factory=ProjectionNodeTimestamps)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProjectionEdge(BaseModel):
    id: str
    edge_type: Literal[
        "control_flow",
        "conditional_flow",
        "belongs_to_run",
        "belongs_to_task",
        "executes_node",
        "delegation",
        "handoff_to",
        "produces_artifact",
        "emits_memory_event",
        "candidate_for_activation",
        "activates",
        "records_feedback",
        "creates_checkpoint",
        "derived_from_checkpoint",
        "resume_from",
        "retry_of",
    ]
    from_id: str
    to_id: str
    intervention: bool = False
    condition: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProjectionGraph(BaseModel):
    projection_kind: str
    version: str = "v1"
    scope: ProjectionScope = Field(default_factory=ProjectionScope)
    summary: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[ProjectionNode] = Field(default_factory=list)
    edges: List[ProjectionEdge] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskTreeProjection(ProjectionGraph):
    projection_kind: Literal["task_tree"] = "task_tree"

class ArtifactLineageProjection(ProjectionGraph):
    projection_kind: Literal["artifact_lineage_graph"] = "artifact_lineage_graph"


class MemoryRelationProjection(ProjectionGraph):
    projection_kind: Literal["memory_relation_graph"] = "memory_relation_graph"


class CheckpointLineageProjection(ProjectionGraph):
    projection_kind: Literal["checkpoint_lineage_graph"] = "checkpoint_lineage_graph"


class ActivationTrainingSample(BaseModel):
    sample_id: str
    run_id: str
    workflow_id: str
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    node_id: str
    step_status: str
    activation_mode: str
    activation_decision: str
    candidate_count: int = 0
    selected_candidate_count: int = 0
    selected_candidate_ids: List[str] = Field(default_factory=list)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    feedback_ids: List[str] = Field(default_factory=list)
    reward_hints: Dict[str, float] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    # Phase 2: assembly-level wire-back fields.
    # Populated when the workflow was produced by AssemblyCompiler from a goal.
    assembly_block_id: Optional[str] = None
    assembly_goal: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActivationTrainingDataset(BaseModel):
    dataset_kind: Literal["activation_training_dataset"] = "activation_training_dataset"
    version: str = "v1"
    scope: ProjectionScope = Field(default_factory=ProjectionScope)
    summary: Dict[str, Any] = Field(default_factory=dict)
    samples: List[ActivationTrainingSample] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowGraphNode(BaseModel):
    id: str
    label: str
    kind: str
    type: str
    entity_type: Literal["workflow_node"] = "workflow_node"
    entrypoint: bool = False
    refs: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowGraphEdge(BaseModel):
    from_id: str
    to_id: str
    type: str
    edge_type: Optional[str] = None
    condition: Optional[str] = None
    intervention: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowGraph(BaseModel):
    projection_kind: Literal["workflow_graph"] = "workflow_graph"
    version: str = "v1"
    workflow_id: str
    name: str
    entrypoint: str
    scope: ProjectionScope = Field(default_factory=ProjectionScope)
    summary: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[WorkflowGraphNode] = Field(default_factory=list)
    edges: List[WorkflowGraphEdge] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunGraphNode(BaseModel):
    id: str
    label: str
    kind: str
    type: str
    entity_type: Literal["workflow_node"] = "workflow_node"
    status: Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled", "not_started"] = "not_started"
    step_id: Optional[str] = None
    index: Optional[int] = None
    entrypoint: bool = False
    refs: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunGraph(BaseModel):
    projection_kind: Literal["run_graph"] = "run_graph"
    version: str = "v1"
    run_id: str
    workflow_id: str
    status: str
    entrypoint: str
    scope: ProjectionScope = Field(default_factory=ProjectionScope)
    summary: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[RunGraphNode] = Field(default_factory=list)
    edges: List[WorkflowGraphEdge] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: f"msg-{uuid4().hex[:12]}")
    role: Literal["system", "user", "assistant"]
    content: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatSessionRecord(BaseModel):
    session_id: str
    title: Optional[str] = None
    status: Literal["active", "archived"] = "active"
    executor: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatSession(BaseModel):
    session: ChatSessionRecord
    messages: List[ChatMessage] = Field(default_factory=list)


class ChatSessionCreateRequest(BaseModel):
    title: Optional[str] = None
    executor: Dict[str, Any]
    system_prompt: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_executor(self) -> "ChatSessionCreateRequest":
        if not isinstance(self.executor, dict):
            raise ValueError("executor must be an object")
        kind = self.executor.get("kind")
        if kind not in {"cli", "api"}:
            raise ValueError("executor.kind must be one of ['api', 'cli']")
        return self


class ChatMessageRequest(BaseModel):
    content: str
    context: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatTurnResult(BaseModel):
    session: ChatSessionRecord
    user_message: ChatMessage
    assistant_message: ChatMessage
    response_text: str
    raw_output: Dict[str, Any] = Field(default_factory=dict)
    trace: List[Dict[str, Any]] = Field(default_factory=list)


# ---------- Story 1.5: Trajectory Export ----------

class RunTrajectory(BaseModel):
    """Structured trajectory for a completed run (AC#1: GET /workflow/runs/{run_id})."""

    run: RunRecord
    steps: List[StepRecord] = Field(default_factory=list)
    handoffs: List[HandoffRef] = Field(default_factory=list)
    checkpoints: List[CheckpointRef] = Field(default_factory=list)
    final_artifacts: List[ArtifactRef] = Field(default_factory=list)
    exported_at: datetime = Field(default_factory=utc_now)


class TrajectoryBundle(BaseModel):
    """Full bundle suitable for 0G Storage archival (AC#2: format=trajectory).

    Story 9.2 added the top-level `citations` list — one entry per
    `CitationTrace`, empty when the run has no provenance to surface.
    """

    trajectory: RunTrajectory
    workflow_yaml: Optional[str] = None
    policy_matrix: Optional[WorkflowPolicyMatrixSpec] = None
    bundle_version: str = "1.0"
    exported_at: datetime = Field(default_factory=utc_now)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------- Story 2.1: Agent Plugin Contract ----------

Kind = Literal["api", "cli", "mcp", "acp"]


class AgentCapabilities(BaseModel):
    streaming: bool = False
    approval_required: bool = False
    session_resume: bool = False
    tool_calls: bool = False
    loop_capable: bool = False


class AgentTask(BaseModel):
    task_id: str = Field(default_factory=lambda: f"atask-{uuid4().hex[:12]}")
    run_id: str
    node_id: str
    agent_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentHandle(BaseModel):
    handle_id: str = Field(default_factory=lambda: f"ahandle-{uuid4().hex[:12]}")
    run_id: str
    node_id: str
    agent_id: str
    status: Literal["pending", "running", "done", "failed", "degraded"] = "pending"
    metadata: Dict[str, Any] = Field(default_factory=dict)


AgentEventTypeLiteral = Literal[
    "agent.dispatched",
    "agent.thinking",
    "agent.tool_called",
    "agent.tool_result",
    "agent.completed",
    "agent.failed",
    "agent.rejected",
    "agent.output",
    "agent.degraded",
    "agent.approval_requested",
    # Story 2.9 — ExternalMemoryBridge events
    "agent.memory_drink",
    "agent.memory_pour",
    "agent.memory_feedback",
    "agent.memory_bridge_circuit_break",
    "agent.memory_bridge_circuit_recover",
]


class AgentEvent(BaseModel):
    run_id: str
    node_id: str
    agent_id: str
    type: AgentEventTypeLiteral
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utc_now)


class ProviderPreset(BaseModel):
    """CLI provider preset loaded from provider_presets.yaml."""

    command: List[str]
    args_template: List[str] = Field(default_factory=list)
    stdin_format: Literal["raw", "json", "none"] = "raw"
    parse_format: Literal["stdout-json", "stdout-text", "jsonl-tail", "codex-jsonl"] = "stdout-text"
    workspace_template: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)


class AgentSpec(BaseModel):
    """Agent declaration in a workflow template YAML."""

    id: str
    soul: Optional[str] = None
    tools: List[str] = Field(default_factory=list)
    executor: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> str:
        return str(self.executor.get("kind", "api"))

    @property
    def provider(self) -> str:
        return str(self.executor.get("provider", ""))


# ---------------------------------------------------------------------------
# WorkflowAssemblySpec — Story 3.4 (AR42: Block Catalog / Stage / Lane 一等公民)
# ---------------------------------------------------------------------------

BLOCK_KINDS = {"plan", "parallel", "barrier", "retry_gate", "approval_gate", "writeback"}


class BlockDef(BaseModel):
    """AR24–29 6 种 Workflow Block 的单条目录项。"""

    id: str
    kind: Literal["plan", "parallel", "barrier", "retry_gate", "approval_gate", "writeback"]
    role: str
    config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LaneDef(BaseModel):
    """Stage 内的单条泳道 — 一个 role 负责的块序列。"""

    id: str
    role: str
    blocks: List[str] = Field(default_factory=list)


class StageDef(BaseModel):
    """顺序阶段 — 内含若干并行泳道，泳道间靠 barrier 汇合。"""

    id: str
    name: str = ""
    lanes: List[LaneDef] = Field(default_factory=list)


class WorkflowDefaults(BaseModel):
    llm: Optional[str] = None
    timeout_seconds: int = 300
    retry_policy: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowAssemblySpec(BaseModel):
    """顶层 assembly spec — 通过 compile() 展开为 WorkflowDefinition。"""

    workflow_id: str
    version: str = "1.0"
    name: str = ""
    block_catalog: List[BlockDef] = Field(default_factory=list)
    stages: List[StageDef] = Field(default_factory=list)
    policy_matrix: Optional[WorkflowPolicyMatrixSpec] = None
    defaults: WorkflowDefaults = Field(default_factory=WorkflowDefaults)
    metadata: Dict[str, Any] = Field(default_factory=dict)
