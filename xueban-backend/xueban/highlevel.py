from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Type, TypeVar

import yaml
from pydantic import BaseModel, Field, model_validator

from xueban.runtime.contracts import EdgeDefinition, NodeDefinition, WorkflowDefinition

T = TypeVar("T", bound=BaseModel)
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_\-.]+)\s*}}")


def _load_serialized_file(path: str | Path) -> Any:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as handle:
        if file_path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(handle)
        return json.load(handle)


def load_spec_file(path: str | Path, model: Type[T]) -> T:
    return model.model_validate(_load_serialized_file(path))


def save_spec_file(path: str | Path, payload: Dict[str, Any]) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    file_path.write_text(rendered, encoding="utf-8")
    return file_path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
    return slug or "xueban"


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged
    return deepcopy(override)


def _render_template_value(value: Any, parameters: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(lambda match: str(parameters.get(match.group(1), match.group(0))), value)
    if isinstance(value, list):
        return [_render_template_value(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: _render_template_value(item, parameters) for key, item in value.items()}
    return value


class ToolIOSpec(BaseModel):
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)


class ToolPolicySpec(BaseModel):
    trust_level: Literal["internal", "external"] = "external"
    side_effects: Literal["read_only", "write", "mixed"] = "read_only"


class ToolSpec(BaseModel):
    tool_id: str
    version: str
    kind: Literal["cli", "mcp", "api", "builtin"]
    name: str
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    runtime: Dict[str, Any] = Field(default_factory=dict)
    io: ToolIOSpec = Field(default_factory=ToolIOSpec)
    policy: ToolPolicySpec = Field(default_factory=ToolPolicySpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_runtime(self) -> "ToolSpec":
        if self.kind == "cli" and "command" not in self.runtime:
            raise ValueError("cli tool runtime.command is required")
        if self.kind == "mcp" and "endpoint" not in self.runtime:
            raise ValueError("mcp tool runtime.endpoint is required")
        if self.kind == "api" and "provider" not in self.runtime:
            raise ValueError("api tool runtime.provider is required")
        if self.kind == "builtin" and "builtin" not in self.runtime:
            raise ValueError("builtin tool runtime.builtin is required")
        return self


class SkillIntentSpec(BaseModel):
    category: str = "general"
    triggers: List[str] = Field(default_factory=list)


class SkillInstructionSpec(BaseModel):
    system: Optional[str] = None
    procedure: List[str] = Field(default_factory=list)


class SkillQualityBarSpec(BaseModel):
    must_check: List[str] = Field(default_factory=list)


class SkillFallbackSpec(BaseModel):
    on_missing_context: Dict[str, Any] = Field(default_factory=dict)


class SkillOutputContractSpec(BaseModel):
    format: Optional[str] = None
    sections: List[str] = Field(default_factory=list)


class SkillSpec(BaseModel):
    skill_id: str
    version: str
    name: str
    description: str = ""
    intent: SkillIntentSpec = Field(default_factory=SkillIntentSpec)
    instructions: SkillInstructionSpec = Field(default_factory=SkillInstructionSpec)
    quality_bar: SkillQualityBarSpec = Field(default_factory=SkillQualityBarSpec)
    recommended_tools: List[str] = Field(default_factory=list)
    output_contract: SkillOutputContractSpec = Field(default_factory=SkillOutputContractSpec)
    fallback: SkillFallbackSpec = Field(default_factory=SkillFallbackSpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RoleStyleSpec(BaseModel):
    tone: Optional[str] = None
    verbosity: Optional[str] = None
    audience: Optional[str] = None
    format_preference: Optional[str] = None


class RoleDecisionSpec(BaseModel):
    priorities: List[str] = Field(default_factory=list)
    heuristics: List[str] = Field(default_factory=list)
    escalation_triggers: List[str] = Field(default_factory=list)


class RoleCollaborationSpec(BaseModel):
    expects: List[str] = Field(default_factory=list)
    handoff_outputs: List[str] = Field(default_factory=list)
    asks_for_help_when: List[str] = Field(default_factory=list)


class RoleSpec(BaseModel):
    role_id: str
    version: str
    name: str
    extends: Optional[str] = None
    description: str = ""
    objectives: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    style: RoleStyleSpec = Field(default_factory=RoleStyleSpec)
    decision_policy: RoleDecisionSpec = Field(default_factory=RoleDecisionSpec)
    collaboration: RoleCollaborationSpec = Field(default_factory=RoleCollaborationSpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutorProfileSpec(BaseModel):
    kind: Literal["cli", "api"]
    provider: str
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    parse: Optional[str] = None
    timeout_seconds: Optional[float] = None
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    stdin: Optional[Literal["json", "text", "none"]] = None
    cwd: Optional[str] = None
    extra_args: List[str] = Field(default_factory=list)


class AgentMemorySpec(BaseModel):
    scope: Literal["session", "user", "global"] = "session"
    writeback_target: Optional[Literal["host", "docs", "memory", "graph"]] = None


class AgentPolicySpec(BaseModel):
    autonomy: Literal["low", "medium", "high"] = "medium"
    allow_side_effects: bool = False
    max_steps: Optional[int] = None


class AgentIOSpec(BaseModel):
    accepts: List[str] = Field(default_factory=list)
    produces: List[str] = Field(default_factory=list)


class AgentSpec(BaseModel):
    agent_id: str
    version: str
    name: str
    extends: Optional[str] = None
    role: str
    skills: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    prompt_template: Optional[str] = None
    node_type: str = "agent.execute"
    executor: ExecutorProfileSpec
    memory: AgentMemorySpec = Field(default_factory=AgentMemorySpec)
    policy: AgentPolicySpec = Field(default_factory=AgentPolicySpec)
    io: AgentIOSpec = Field(default_factory=AgentIOSpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TemplateParameterSpec(BaseModel):
    type: Literal["string", "number", "boolean", "json"] = "string"
    required: bool = True
    default: Any = None
    description: Optional[str] = None


class TemplateAgentSpec(BaseModel):
    id: str
    ref: str
    assignment: Dict[str, Any] = Field(default_factory=dict)
    overrides: Dict[str, Any] = Field(default_factory=dict)
    config_patch: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    local_activation: Optional["LocalActivationSpec"] = None


class LocalActivationSpec(BaseModel):
    mode: Literal["always", "local", "manual"] = "always"
    tags: List[str] = Field(default_factory=list)
    activate_when: List[str] = Field(default_factory=list)
    suppress_when: List[str] = Field(default_factory=list)
    delegate_candidates: List[str] = Field(default_factory=list)
    subgoal_triggers: List[str] = Field(default_factory=list)
    retry_gates: List[str] = Field(default_factory=list)
    review_gates: List[str] = Field(default_factory=list)
    feedback_channels: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TemplateAgentPolicySpec(BaseModel):
    tools: List[str] = Field(default_factory=list)
    side_effects: Literal["inherit", "read_only", "write", "mixed"] = "inherit"
    requires_confirmation: bool = False
    writeback_targets: List[Literal["host", "docs", "memory", "graph"]] = Field(default_factory=list)
    notes: Optional[str] = None


class WorkflowPolicyMatrixSpec(BaseModel):
    agents: Dict[str, TemplateAgentPolicySpec] = Field(default_factory=dict)


class WorkflowActivationSpec(BaseModel):
    enabled: bool = True
    default_mode: Literal["always", "local", "manual"] = "always"
    signal_sources: List[Literal["goal", "context", "state", "artifact", "memory", "feedback"]] = Field(
        default_factory=lambda: ["goal", "context", "feedback"]
    )
    selection_threshold: float = 0.0
    top_k: Optional[int] = None
    budget: Optional[int] = None
    signal_weights: Dict[str, float] = Field(default_factory=dict)
    candidate_type_weights: Dict[str, float] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TemplateNodeSpec(BaseModel):
    id: str
    type: str
    label: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    retry_policy: Dict[str, Any] = Field(default_factory=dict)
    local_activation: Optional["LocalActivationSpec"] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentRosterEntry(BaseModel):
    """UI-layer agent roster entry — lightweight display info for Inbox/AgentDM/TemplateSwitcher."""

    id: str
    name: str
    soul: str = ""
    llm: str = ""
    tools: List[str] = Field(default_factory=list)


class GroupTemplateSpec(BaseModel):
    """Default group-chat template within a template — used by Inbox '+ New Group' flow."""

    id: str
    name: str
    agents: List[str] = Field(default_factory=list)
    policy_matrix: str = ""


class WorkflowStageSpec(BaseModel):
    stage_id: str
    name: str = ""
    lane: str = "default"
    agents: List[str] = Field(default_factory=list)
    barrier: bool = False
    approval_required: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TemplateEdgeSpec(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    type: Literal["default", "conditional", "final"] = "default"
    condition: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class TemplateFlowSpec(BaseModel):
    entrypoint: str
    edges: List[TemplateEdgeSpec] = Field(default_factory=list)
    enforce_stage_order: bool = False


class WorkflowTemplateSpec(BaseModel):
    template_id: str
    version: str
    name: str
    description: str = ""
    parameters: Dict[str, TemplateParameterSpec] = Field(default_factory=dict)
    agents: List[TemplateAgentSpec] = Field(default_factory=list)
    nodes: List[TemplateNodeSpec] = Field(default_factory=list)
    flow: TemplateFlowSpec
    policy_matrix: WorkflowPolicyMatrixSpec = Field(default_factory=WorkflowPolicyMatrixSpec)
    activation: WorkflowActivationSpec = Field(default_factory=WorkflowActivationSpec)
    stages: List[WorkflowStageSpec] = Field(default_factory=list)
    # Collaboration Quad-View fields (Epic 7 / 2026-04-16 addendum)
    user_role: str = "Owner"
    default_ops_room_name: str = ""
    brief_board_alias: str = "BriefBoard"
    agent_roster: List[AgentRosterEntry] = Field(default_factory=list)
    group_roster: List[GroupTemplateSpec] = Field(default_factory=list)
    theme_color: str = "#6366F1"
    defaults: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_template(self) -> "WorkflowTemplateSpec":
        agent_ids = {agent.id for agent in self.agents}
        node_spec_ids = {node.id for node in self.nodes}
        duplicate_ids = sorted(agent_ids.intersection(node_spec_ids))
        if duplicate_ids:
            raise ValueError(f"template ids must be unique across agents/nodes: {', '.join(duplicate_ids)}")
        graph_node_ids = agent_ids | node_spec_ids
        if self.flow.entrypoint not in graph_node_ids:
            raise ValueError("template flow.entrypoint must reference an existing template agent id")
        for edge in self.flow.edges:
            if edge.from_id not in graph_node_ids:
                raise ValueError(f"template edge.from references unknown node: {edge.from_id}")
            if edge.to_id != "END" and edge.to_id not in graph_node_ids:
                raise ValueError(f"template edge.to references unknown node: {edge.to_id}")
        unknown_policy_agents = sorted(set(self.policy_matrix.agents) - agent_ids)
        if unknown_policy_agents:
            raise ValueError(
                f"template policy_matrix references unknown agent ids: {', '.join(unknown_policy_agents)}"
            )
        for agent in self.agents:
            if agent.local_activation is None:
                continue
            unknown_delegate_candidates = sorted(set(agent.local_activation.delegate_candidates) - graph_node_ids)
            if unknown_delegate_candidates:
                raise ValueError(
                    f"template agent {agent.id} local_activation references unknown delegate candidates: "
                    f"{', '.join(unknown_delegate_candidates)}"
                )
        for node in self.nodes:
            if node.local_activation is None:
                continue
            unknown_delegate_candidates = sorted(set(node.local_activation.delegate_candidates) - graph_node_ids)
            if unknown_delegate_candidates:
                raise ValueError(
                    f"template node {node.id} local_activation references unknown delegate candidates: "
                    f"{', '.join(unknown_delegate_candidates)}"
                )
        if self.stages:
            stage_ids: set[str] = set()
            seen_stage_agents: set[str] = set()
            stage_order_by_agent: Dict[str, int] = {}
            for index, stage in enumerate(self.stages):
                if stage.stage_id in stage_ids:
                    raise ValueError(f"template stage_id must be unique: {stage.stage_id}")
                stage_ids.add(stage.stage_id)
                unknown_stage_agents = sorted(set(stage.agents) - graph_node_ids)
                if unknown_stage_agents:
                    raise ValueError(
                        f"template stage {stage.stage_id} references unknown node ids: {', '.join(unknown_stage_agents)}"
                    )
                overlap = seen_stage_agents.intersection(stage.agents)
                if overlap:
                    raise ValueError(
                        f"template nodes may only appear in one stage: {', '.join(sorted(overlap))}"
                    )
                seen_stage_agents.update(stage.agents)
                for agent_id in stage.agents:
                    stage_order_by_agent[agent_id] = index
            missing_stage_agents = sorted(graph_node_ids - seen_stage_agents)
            if missing_stage_agents:
                raise ValueError(
                    f"template stages must cover every template node id: {', '.join(missing_stage_agents)}"
                )
            first_stage_agents = set(self.stages[0].agents)
            if self.flow.entrypoint not in first_stage_agents:
                raise ValueError("template flow.entrypoint must appear in the first stage")
            if self.flow.enforce_stage_order:
                for edge in self.flow.edges:
                    if edge.to_id == "END":
                        continue
                    from_stage = stage_order_by_agent[edge.from_id]
                    to_stage = stage_order_by_agent[edge.to_id]
                    if to_stage < from_stage:
                        raise ValueError(
                            f"template edge {edge.from_id}->{edge.to_id} moves backward across stages"
                        )
        # Validate agent_roster id uniqueness
        roster_ids = [entry.id for entry in self.agent_roster]
        if len(roster_ids) != len(set(roster_ids)):
            seen: set[str] = set()
            duplicates = sorted({rid for rid in roster_ids if rid in seen or seen.add(rid)})  # type: ignore[func-returns-value]
            raise ValueError(f"agent_roster ids must be unique: {', '.join(duplicates)}")
        # Validate group_roster id uniqueness
        group_ids = [g.id for g in self.group_roster]
        if len(group_ids) != len(set(group_ids)):
            seen_g: set[str] = set()
            dup_g = sorted({gid for gid in group_ids if gid in seen_g or seen_g.add(gid)})  # type: ignore[func-returns-value]
            raise ValueError(f"group_roster ids must be unique: {', '.join(dup_g)}")
        # Validate group_roster agent references exist in agent_roster or agents
        all_known_ids = {entry.id for entry in self.agent_roster} | agent_ids
        for group in self.group_roster:
            unknown = sorted(set(group.agents) - all_known_ids)
            if unknown:
                raise ValueError(
                    f"group_roster '{group.id}' references unknown agent ids: {', '.join(unknown)}"
                )
        return self


class WorkflowBlockUsesSpec(BaseModel):
    agents: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)


class WorkflowBlockPolicySpec(BaseModel):
    side_effects: Literal["inherit", "read_only", "write", "mixed"] = "inherit"
    requires_confirmation: bool = False
    writeback_targets: List[Literal["host", "docs", "memory", "graph"]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowBlockCompileSpec(BaseModel):
    node_kind: Literal["agent", "node"] = "node"
    node_type: str
    route_strategy: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowBlockSpec(BaseModel):
    block_id: str
    kind: Literal["worker", "control", "delegation", "persistence"]
    type: str
    label: str
    description: str = ""
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    uses: WorkflowBlockUsesSpec = Field(default_factory=WorkflowBlockUsesSpec)
    policy: WorkflowBlockPolicySpec = Field(default_factory=WorkflowBlockPolicySpec)
    compile: WorkflowBlockCompileSpec
    local_activation: Optional["LocalActivationSpec"] = None
    # capabilities: system-facing capability declarations (English only).
    # Used by ActivationSelector for greedy set-cover matching.
    # Distinct from local_activation.tags (user-facing, bilingual, keyword search).
    capabilities: List[str] = Field(default_factory=list)
    # input_requirements: capabilities this block needs from upstream blocks.
    # Used by ConnectionResolver v2 for capability-dependency graph inference.
    # Empty means the block can serve as an entry point (no upstream dependency).
    input_requirements: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowAssemblyBlockSpec(BaseModel):
    id: str
    ref: str
    agent: Optional[str] = None
    assignment: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    local_activation: Optional["LocalActivationSpec"] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowAssemblyLinkSpec(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    type: Literal["default", "conditional", "final"] = "default"
    condition: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class AssemblyConstraintSpec(BaseModel):
    allow_cycles: bool = False
    require_connected_graph: bool = True
    require_explicit_entrypoint: bool = False
    require_terminal_path: bool = False
    allowed_overlays: List[str] = Field(default_factory=list)
    allowed_block_kinds: List[Literal["worker", "control", "delegation", "persistence"]] = Field(
        default_factory=list
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowAssemblySpec(BaseModel):
    assembly_id: str
    version: str = "0.1"
    name: str
    description: str = ""
    goal: Optional[str] = None
    parameters: Dict[str, TemplateParameterSpec] = Field(default_factory=dict)
    blocks: List[WorkflowAssemblyBlockSpec] = Field(default_factory=list)
    links: List[WorkflowAssemblyLinkSpec] = Field(default_factory=list)
    entrypoint: Optional[str] = None
    overlays: List[str] = Field(default_factory=list)
    constraints: AssemblyConstraintSpec = Field(default_factory=AssemblyConstraintSpec)
    defaults: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assembly(self) -> "WorkflowAssemblySpec":
        if not self.blocks:
            raise ValueError("assembly must define at least one block")
        block_ids = [block.id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("assembly block ids must be unique")
        known_ids = set(block_ids)
        if self.entrypoint is not None and self.entrypoint not in known_ids:
            raise ValueError("assembly entrypoint must reference an existing block id")
        if self.constraints.require_explicit_entrypoint and not self.entrypoint:
            raise ValueError("assembly constraints require an explicit entrypoint")
        for link in self.links:
            if link.from_id not in known_ids:
                raise ValueError(f"assembly link.from references unknown block: {link.from_id}")
            if link.to_id != "END" and link.to_id not in known_ids:
                raise ValueError(f"assembly link.to references unknown block: {link.to_id}")

        if not self.constraints.allow_cycles:
            adjacency: Dict[str, List[str]] = {block_id: [] for block_id in known_ids}
            for link in self.links:
                if link.to_id != "END":
                    adjacency[link.from_id].append(link.to_id)
            visiting: set[str] = set()
            visited: set[str] = set()

            def _dfs(node_id: str) -> bool:
                if node_id in visiting:
                    return True
                if node_id in visited:
                    return False
                visiting.add(node_id)
                for neighbor in adjacency.get(node_id, []):
                    if _dfs(neighbor):
                        return True
                visiting.remove(node_id)
                visited.add(node_id)
                return False

            for block_id in known_ids:
                if _dfs(block_id):
                    raise ValueError("assembly defines a cycle but constraints.allow_cycles is false")
        return self


def build_builtin_block_catalog() -> Dict[str, WorkflowBlockSpec]:
    return {
        "plan": WorkflowBlockSpec.model_validate(
            {
                "block_id": "plan",
                "kind": "worker",
                "type": "plan",
                "label": "Plan",
                "inputs": ["goal", "context"],
                "outputs": ["plan"],
                "policy": {"side_effects": "read_only"},
                "compile": {
                    "node_kind": "agent",
                    "node_type": "agent.execute",
                    "route_strategy": "plan",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["plan", "planning", "规划", "计划"]},
                "capabilities": ["planning", "task_decomposition"],
                "input_requirements": [],
            }
        ),
        "review": WorkflowBlockSpec.model_validate(
            {
                "block_id": "review",
                "kind": "worker",
                "type": "review",
                "label": "Review",
                "inputs": ["draft"],
                "outputs": ["review_feedback"],
                "policy": {"side_effects": "read_only", "requires_confirmation": True},
                "compile": {
                    "node_kind": "agent",
                    "node_type": "agent.execute",
                    "route_strategy": "review_gate",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["review", "check", "审查", "检查"]},
                "capabilities": ["review", "quality_check"],
                "input_requirements": ["execution"],
            }
        ),
        "execute": WorkflowBlockSpec.model_validate(
            {
                "block_id": "execute",
                "kind": "worker",
                "type": "execute",
                "label": "Execute",
                "inputs": ["task"],
                "outputs": ["result"],
                "policy": {"side_effects": "mixed"},
                "compile": {
                    "node_kind": "agent",
                    "node_type": "agent.execute",
                    "route_strategy": "execute",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["execute", "run", "执行", "运行"]},
                "capabilities": ["execution", "action"],
                "input_requirements": ["planning"],
            }
        ),
        "parallel": WorkflowBlockSpec.model_validate(
            {
                "block_id": "parallel",
                "kind": "control",
                "type": "parallel",
                "label": "Parallel Fan-out",
                "outputs": ["branch_results"],
                "compile": {
                    "node_kind": "node",
                    "node_type": "control.parallel",
                    "route_strategy": "parallel",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["parallel", "并行", "fan-out"]},
                "capabilities": ["parallelism", "fan_out"],
                "input_requirements": [],
            }
        ),
        "barrier": WorkflowBlockSpec.model_validate(
            {
                "block_id": "barrier",
                "kind": "control",
                "type": "barrier",
                "label": "Barrier",
                "inputs": ["branch_results"],
                "outputs": ["merged_result"],
                "compile": {
                    "node_kind": "node",
                    "node_type": "control.barrier",
                    "route_strategy": "barrier",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["barrier", "merge", "汇聚", "同步"]},
                "capabilities": ["synchronization", "fan_in"],
                "input_requirements": ["parallelism"],
            }
        ),
        "delegate": WorkflowBlockSpec.model_validate(
            {
                "block_id": "delegate",
                "kind": "delegation",
                "type": "delegate",
                "label": "Delegate",
                "inputs": ["goal", "context"],
                "outputs": ["child_result"],
                "policy": {"side_effects": "mixed"},
                "compile": {
                    "node_kind": "agent",
                    "node_type": "agent.execute",
                    "route_strategy": "delegate",
                    "metadata": {"builtin": True, "delegated": True},
                },
                "local_activation": {"tags": ["delegate", "sub-agent", "委托", "分派"]},
                "capabilities": ["delegation", "sub_workflow"],
                "input_requirements": ["planning"],
            }
        ),
        "artifact": WorkflowBlockSpec.model_validate(
            {
                "block_id": "artifact",
                "kind": "persistence",
                "type": "artifact",
                "label": "Artifact",
                "inputs": ["result"],
                "outputs": ["artifact_ref"],
                "policy": {"side_effects": "write"},
                "compile": {
                    "node_kind": "node",
                    "node_type": "artifact.emit",
                    "route_strategy": "artifact",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["artifact", "output", "输出", "产物"]},
                "capabilities": ["artifact_emit", "persistence"],
                "input_requirements": ["execution"],
            }
        ),
        "checkpoint": WorkflowBlockSpec.model_validate(
            {
                "block_id": "checkpoint",
                "kind": "persistence",
                "type": "checkpoint",
                "label": "Checkpoint",
                "inputs": ["state"],
                "outputs": ["checkpoint_ref"],
                "policy": {"side_effects": "write"},
                "compile": {
                    "node_kind": "node",
                    "node_type": "checkpoint.write",
                    "route_strategy": "checkpoint",
                    "metadata": {"builtin": True},
                },
                "local_activation": {"tags": ["checkpoint", "save", "保存", "检查点"]},
                "capabilities": ["checkpoint_write", "state_persistence"],
                "input_requirements": ["execution"],
            }
        ),
    }


class SpecRegistry:
    def __init__(
        self,
        *,
        tools: Optional[Dict[str, ToolSpec]] = None,
        skills: Optional[Dict[str, SkillSpec]] = None,
        roles: Optional[Dict[str, RoleSpec]] = None,
        agents: Optional[Dict[str, AgentSpec]] = None,
        blocks: Optional[Dict[str, WorkflowBlockSpec]] = None,
        templates: Optional[Dict[str, WorkflowTemplateSpec]] = None,
        assemblies: Optional[Dict[str, WorkflowAssemblySpec]] = None,
        raw_role_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        raw_agent_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.tools = tools or {}
        self.skills = skills or {}
        self.roles = roles or {}
        self.agents = agents or {}
        self.blocks = blocks or {}
        self.templates = templates or {}
        self.assemblies = assemblies or {}
        self._raw_role_payloads = raw_role_payloads or {}
        self._raw_agent_payloads = raw_agent_payloads or {}

    @classmethod
    def load_from_root(cls, root: str | Path) -> "SpecRegistry":
        root_path = Path(root)
        tools: Dict[str, ToolSpec] = {}
        skills: Dict[str, SkillSpec] = {}
        blocks: Dict[str, WorkflowBlockSpec] = {}
        templates: Dict[str, WorkflowTemplateSpec] = {}
        assemblies: Dict[str, WorkflowAssemblySpec] = {}
        raw_role_payloads: Dict[str, Dict[str, Any]] = {}
        raw_agent_payloads: Dict[str, Dict[str, Any]] = {}

        def iter_spec_files(directory: Path) -> List[Path]:
            if not directory.exists():
                return []
            paths: List[Path] = []
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}:
                    paths.append(path)
            return sorted(paths)

        for path in iter_spec_files(root_path / "tools"):
            tool = load_spec_file(path, ToolSpec)
            tools[tool.tool_id] = tool
        for path in iter_spec_files(root_path / "skills"):
            skill = load_spec_file(path, SkillSpec)
            skills[skill.skill_id] = skill
        for path in iter_spec_files(root_path / "blocks"):
            block = load_spec_file(path, WorkflowBlockSpec)
            blocks[block.block_id] = block
        for path in iter_spec_files(root_path / "roles"):
            payload = _load_serialized_file(path)
            if not isinstance(payload, dict):
                raise ValueError(f"role spec file must contain an object: {path}")
            role_id = payload.get("role_id")
            if not isinstance(role_id, str) or not role_id:
                raise ValueError(f"role spec missing role_id: {path}")
            raw_role_payloads[role_id] = payload
        for path in iter_spec_files(root_path / "templates"):
            template = load_spec_file(path, WorkflowTemplateSpec)
            templates[template.template_id] = template
        for path in iter_spec_files(root_path / "assemblies"):
            assembly = load_spec_file(path, WorkflowAssemblySpec)
            assemblies[assembly.assembly_id] = assembly
        for path in iter_spec_files(root_path / "agents"):
            payload = _load_serialized_file(path)
            if not isinstance(payload, dict):
                raise ValueError(f"agent spec file must contain an object: {path}")
            agent_id = payload.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError(f"agent spec missing agent_id: {path}")
            raw_agent_payloads[agent_id] = payload

        registry = cls(
            tools=tools,
            skills=skills,
            blocks=blocks,
            templates=templates,
            assemblies=assemblies,
            raw_role_payloads=raw_role_payloads,
            raw_agent_payloads=raw_agent_payloads,
        )
        registry._hydrate_roles()
        registry._hydrate_agents()
        return registry

    def _hydrate_roles(self) -> None:
        self.roles = {role_id: self.resolve_role(role_id) for role_id in self._raw_role_payloads}

    def resolve_role(self, role_id: str) -> RoleSpec:
        return RoleSpec.model_validate(self._resolve_role_payload(role_id, stack=[]))

    def _resolve_role_payload(self, role_id: str, stack: List[str]) -> Dict[str, Any]:
        if role_id in stack:
            cycle = " -> ".join(stack + [role_id])
            raise ValueError(f"role extends cycle detected: {cycle}")
        payload = self._raw_role_payloads.get(role_id)
        if payload is None:
            raise KeyError(f"role spec not found: {role_id}")

        current = deepcopy(payload)
        base_id = current.get("extends")
        if not base_id:
            return current

        base_payload = self._resolve_role_payload(str(base_id), stack + [role_id])
        merged = _deep_merge(base_payload, current)
        merged["role_id"] = current.get("role_id", role_id)
        return merged

    def _hydrate_agents(self) -> None:
        self.agents = {agent_id: self.resolve_agent(agent_id) for agent_id in self._raw_agent_payloads}

    def resolve_agent(self, agent_id: str) -> AgentSpec:
        return AgentSpec.model_validate(self._resolve_agent_payload(agent_id, stack=[]))

    def _resolve_agent_payload(self, agent_id: str, stack: List[str]) -> Dict[str, Any]:
        if agent_id in stack:
            cycle = " -> ".join(stack + [agent_id])
            raise ValueError(f"agent extends cycle detected: {cycle}")
        payload = self._raw_agent_payloads.get(agent_id)
        if payload is None:
            raise KeyError(f"agent spec not found: {agent_id}")

        current = deepcopy(payload)
        base_id = current.get("extends")
        if not base_id:
            return current

        base_payload = self._resolve_agent_payload(str(base_id), stack + [agent_id])
        merged = _deep_merge(base_payload, current)
        merged["agent_id"] = current.get("agent_id", agent_id)
        return merged

    def get_tool(self, tool_id: str) -> ToolSpec:
        tool = self.tools.get(tool_id)
        if tool is None:
            raise KeyError(f"tool spec not found: {tool_id}")
        return tool

    def get_skill(self, skill_id: str) -> SkillSpec:
        skill = self.skills.get(skill_id)
        if skill is None:
            raise KeyError(f"skill spec not found: {skill_id}")
        return skill

    def get_role(self, role_id: str) -> RoleSpec:
        role = self.roles.get(role_id)
        if role is None:
            raise KeyError(f"role spec not found: {role_id}")
        return role

    def get_agent(self, agent_id: str) -> AgentSpec:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise KeyError(f"agent spec not found: {agent_id}")
        return agent

    def get_block(self, block_id: str) -> WorkflowBlockSpec:
        block = self.blocks.get(block_id)
        if block is None:
            raise KeyError(f"block spec not found: {block_id}")
        return block

    def get_template(self, template_id: str) -> WorkflowTemplateSpec:
        template = self.templates.get(template_id)
        if template is None:
            raise KeyError(f"template spec not found: {template_id}")
        return template

    def get_assembly(self, assembly_id: str) -> WorkflowAssemblySpec:
        assembly = self.assemblies.get(assembly_id)
        if assembly is None:
            raise KeyError(f"assembly spec not found: {assembly_id}")
        return assembly

    def counts(self) -> Dict[str, int]:
        return {
            "tools": len(self.tools),
            "skills": len(self.skills),
            "roles": len(self.roles),
            "agents": len(self.agents),
            "blocks": len(self.blocks),
            "templates": len(self.templates),
            "assemblies": len(self.assemblies),
        }

    def list_kind(self, kind: str) -> List[BaseModel]:
        mapping = {
            "tools": self.tools,
            "skills": self.skills,
            "roles": self.roles,
            "agents": self.agents,
            "blocks": self.blocks,
            "templates": self.templates,
            "assemblies": self.assemblies,
        }
        if kind not in mapping:
            raise KeyError(f"unsupported registry kind: {kind}")
        return list(mapping[kind].values())

    def get_kind(self, kind: str, spec_id: str) -> BaseModel:
        getter_map = {
            "tools": self.get_tool,
            "skills": self.get_skill,
            "roles": self.get_role,
            "agents": self.get_agent,
            "blocks": self.get_block,
            "templates": self.get_template,
            "assemblies": self.get_assembly,
        }
        if kind not in getter_map:
            raise KeyError(f"unsupported registry kind: {kind}")
        return getter_map[kind](spec_id)


class TemplateCompiler:
    def __init__(self, registry: SpecRegistry) -> None:
        self.registry = registry

    def compile(
        self,
        template: WorkflowTemplateSpec,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> WorkflowDefinition:
        resolved_parameters = self._resolve_parameters(template, parameters or {})
        stage_index = self._build_stage_index(template)
        self._validate_template_governance(template, stage_index)
        agent_nodes = [
            self._compile_agent_node(
                agent_spec,
                resolved_parameters,
                stage=stage_index.get(agent_spec.id),
                policy=template.policy_matrix.agents.get(agent_spec.id),
            )
            for agent_spec in template.agents
        ]
        static_nodes = [
            self._compile_static_node(
                node_spec,
                resolved_parameters,
                stage=stage_index.get(node_spec.id),
            )
            for node_spec in template.nodes
        ]
        edges = [
            EdgeDefinition.model_validate(
                {
                    "from": edge.from_id,
                    "to": edge.to_id,
                    "type": edge.type,
                    "condition": _render_template_value(edge.condition, resolved_parameters),
                    "metadata": _render_template_value(edge.metadata, resolved_parameters),
                }
            )
            for edge in template.flow.edges
        ]

        return WorkflowDefinition.model_validate(
            {
                "workflow_id": template.template_id,
                "version": template.version,
                "name": _render_template_value(template.name, resolved_parameters),
                "entrypoint": template.flow.entrypoint,
                "nodes": [
                    node.model_dump(mode="python", by_alias=True) for node in [*agent_nodes, *static_nodes]
                ],
                "edges": [edge.model_dump(mode="python", by_alias=True) for edge in edges],
                "defaults": _render_template_value(template.defaults, resolved_parameters),
                "metadata": {
                    **_render_template_value(template.metadata, resolved_parameters),
                    "template_id": template.template_id,
                    "template_version": template.version,
                    "template_parameters": resolved_parameters,
                    "template_activation": _render_template_value(
                        template.activation.model_dump(mode="python"), resolved_parameters
                    ),
                    "template_stages": [
                        _render_template_value(stage.model_dump(mode="python"), resolved_parameters) for stage in template.stages
                    ],
                    "template_policy_matrix": _render_template_value(
                        template.policy_matrix.model_dump(mode="python"), resolved_parameters
                    ),
                },
            }
        )

    def _build_stage_index(self, template: WorkflowTemplateSpec) -> Dict[str, WorkflowStageSpec]:
        stage_index: Dict[str, WorkflowStageSpec] = {}
        for stage in template.stages:
            for agent_id in stage.agents:
                stage_index[agent_id] = stage
        return stage_index

    def _resolve_parameters(
        self,
        template: WorkflowTemplateSpec,
        provided: Dict[str, Any],
    ) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {}
        for key, spec in template.parameters.items():
            if key in provided:
                resolved[key] = self._coerce_parameter_value(key, provided[key], spec)
                continue
            if spec.default is not None:
                resolved[key] = deepcopy(spec.default)
                continue
            if spec.required:
                raise ValueError(f"missing required template parameter: {key}")
        for key, value in provided.items():
            if key not in resolved:
                resolved[key] = value
        return resolved

    def _coerce_parameter_value(self, key: str, value: Any, spec: TemplateParameterSpec) -> Any:
        if spec.type == "string":
            if isinstance(value, str):
                return value
            return str(value)
        if spec.type == "number":
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                return float(value) if "." in value else int(value)
            raise ValueError(f"template parameter {key} must be a number")
        if spec.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes"}:
                    return True
                if normalized in {"false", "0", "no"}:
                    return False
            raise ValueError(f"template parameter {key} must be a boolean")
        return value

    def _compile_agent_node(
        self,
        template_agent: TemplateAgentSpec,
        parameters: Dict[str, Any],
        *,
        stage: Optional[WorkflowStageSpec],
        policy: Optional[TemplateAgentPolicySpec],
    ) -> NodeDefinition:
        base_payload = self.registry.get_agent(template_agent.ref).model_dump(mode="python")
        merged_payload = _deep_merge(base_payload, template_agent.overrides)
        merged_payload["agent_id"] = merged_payload.get("agent_id", template_agent.ref)
        agent = AgentSpec.model_validate(merged_payload)
        role = self.registry.get_role(agent.role)
        skills = [self.registry.get_skill(skill_id) for skill_id in agent.skills]
        tools = [self.registry.get_tool(tool_id) for tool_id in agent.tools]

        assignment_payload = deepcopy(template_agent.assignment)
        if stage is not None and stage.approval_required and not assignment_payload.get("approval_required"):
            assignment_payload["approval_required"] = True
        assignment = _render_template_value(assignment_payload, parameters)
        local_activation_payload = (
            template_agent.local_activation.model_dump(mode="python")
            if template_agent.local_activation is not None
            else {
                "mode": "always",
                "tags": [],
                "activate_when": [],
                "suppress_when": [],
                "delegate_candidates": [],
                "subgoal_triggers": [],
                "retry_gates": [],
                "review_gates": [],
                "feedback_channels": [],
                "metadata": {},
            }
        )
        local_activation = _render_template_value(local_activation_payload, parameters)
        prompt = self._build_prompt(agent=agent, role=role, skills=skills, tools=tools, assignment=assignment)
        config = {
            "role": role.role_id,
            "prompt": prompt,
            "executor": agent.executor.model_dump(exclude_none=True),
            "agent_ref": agent.agent_id,
            "role_ref": role.role_id,
            "assignment": assignment,
            "skill_refs": [skill.skill_id for skill in skills],
            "tool_refs": [tool.tool_id for tool in tools],
            "agent_policy": agent.policy.model_dump(exclude_none=True),
            "agent_memory": agent.memory.model_dump(exclude_none=True),
            "agent_io": agent.io.model_dump(exclude_none=True),
            "template_stage": stage.model_dump(mode="python") if stage is not None else None,
            "template_policy": policy.model_dump(mode="python") if policy is not None else None,
            "local_activation": local_activation,
        }
        config = _deep_merge(config, template_agent.config_patch)
        config = _render_template_value(config, parameters)
        metadata = {
            "agent_name": agent.name,
            "role_name": role.name,
            "template_agent_ref": template_agent.ref,
            "assignment": assignment,
            "activation_mode": local_activation.get("mode", "always"),
            "activation_tags": local_activation.get("tags", []),
            "delegate_candidates": local_activation.get("delegate_candidates", []),
            "template_stage_id": stage.stage_id if stage is not None else None,
            "template_lane": stage.lane if stage is not None else None,
        }
        metadata = _deep_merge(metadata, template_agent.metadata)
        metadata = _render_template_value(metadata, parameters)
        return NodeDefinition.model_validate(
            {
                "id": template_agent.id,
                "kind": "agent",
                "type": agent.node_type,
                "config": config,
                "inputs": agent.io.accepts,
                "outputs": agent.io.produces,
                "metadata": metadata,
            }
        )

    def _compile_static_node(
        self,
        node_spec: TemplateNodeSpec,
        parameters: Dict[str, Any],
        *,
        stage: Optional[WorkflowStageSpec],
    ) -> NodeDefinition:
        local_activation_payload = (
            node_spec.local_activation.model_dump(mode="python")
            if node_spec.local_activation is not None
            else {
                "mode": "always",
                "tags": [],
                "activate_when": [],
                "suppress_when": [],
                "delegate_candidates": [],
                "subgoal_triggers": [],
                "retry_gates": [],
                "review_gates": [],
                "feedback_channels": [],
                "metadata": {},
            }
        )
        local_activation = _render_template_value(local_activation_payload, parameters)
        config = _render_template_value(
            {
                **deepcopy(node_spec.config),
                "template_stage": stage.model_dump(mode="python") if stage is not None else None,
                "local_activation": local_activation,
            },
            parameters,
        )
        metadata = _render_template_value(
            {
                **deepcopy(node_spec.metadata),
                "node_label": node_spec.label,
                "activation_mode": local_activation.get("mode", "always"),
                "activation_tags": local_activation.get("tags", []),
                "delegate_candidates": local_activation.get("delegate_candidates", []),
                "template_stage_id": stage.stage_id if stage is not None else None,
                "template_lane": stage.lane if stage is not None else None,
            },
            parameters,
        )
        return NodeDefinition.model_validate(
            {
                "id": node_spec.id,
                "kind": "node",
                "type": _render_template_value(node_spec.type, parameters),
                "config": config,
                "inputs": _render_template_value(node_spec.inputs, parameters),
                "outputs": _render_template_value(node_spec.outputs, parameters),
                "retry_policy": _render_template_value(node_spec.retry_policy, parameters),
                "metadata": metadata,
            }
        )

    def _validate_template_governance(
        self,
        template: WorkflowTemplateSpec,
        stage_index: Dict[str, WorkflowStageSpec],
    ) -> None:
        for template_agent in template.agents:
            base_payload = self.registry.get_agent(template_agent.ref).model_dump(mode="python")
            merged_payload = _deep_merge(base_payload, template_agent.overrides)
            merged_payload["agent_id"] = merged_payload.get("agent_id", template_agent.ref)
            agent = AgentSpec.model_validate(merged_payload)
            tools = [self.registry.get_tool(tool_id) for tool_id in agent.tools]
            policy = template.policy_matrix.agents.get(template_agent.id)
            stage = stage_index.get(template_agent.id)
            if policy is not None:
                if policy.tools:
                    allowed_tools = set(policy.tools)
                    unknown_tools = sorted(allowed_tools - set(agent.tools))
                    if unknown_tools:
                        raise ValueError(
                            f"template agent {template_agent.id} policy_matrix allows unknown/unattached tools: {', '.join(unknown_tools)}"
                        )
                    disallowed_tools = sorted(set(agent.tools) - allowed_tools)
                    if disallowed_tools:
                        raise ValueError(
                            f"template agent {template_agent.id} references tools outside policy_matrix: {', '.join(disallowed_tools)}"
                        )
                if policy.side_effects == "read_only":
                    violating_tools = [
                        tool.tool_id for tool in tools if tool.policy.side_effects in {"write", "mixed"}
                    ]
                    if violating_tools:
                        raise ValueError(
                            f"template agent {template_agent.id} is read_only but references write-capable tools: "
                            f"{', '.join(violating_tools)}"
                        )
                    if agent.policy.allow_side_effects:
                        raise ValueError(
                            f"template agent {template_agent.id} is read_only but agent policy allows side effects"
                        )
                if policy.side_effects in {"write", "mixed"} and not agent.policy.allow_side_effects:
                    raise ValueError(
                        f"template agent {template_agent.id} requires side effects but agent policy disallows them"
                    )
                if policy.writeback_targets:
                    writeback_target = agent.memory.writeback_target
                    if writeback_target is not None and writeback_target not in policy.writeback_targets:
                        raise ValueError(
                            f"template agent {template_agent.id} writeback target {writeback_target} is not allowed by policy_matrix"
                        )

    def _build_prompt(
        self,
        *,
        agent: AgentSpec,
        role: RoleSpec,
        skills: List[SkillSpec],
        tools: List[ToolSpec],
        assignment: Optional[Dict[str, Any]] = None,
    ) -> str:
        assignment_lines = self._build_assignment_lines(assignment or {})
        if agent.prompt_template:
            if assignment_lines:
                return "\n".join([agent.prompt_template, "", *assignment_lines])
            return agent.prompt_template

        lines: List[str] = [f"Role: {role.name}"]
        if role.description:
            lines.append(f"Role Description: {role.description}")
        if role.objectives:
            lines.append("Objectives:")
            lines.extend(f"- {item}" for item in role.objectives)
        if role.responsibilities:
            lines.append("Responsibilities:")
            lines.extend(f"- {item}" for item in role.responsibilities)
        if role.constraints:
            lines.append("Constraints:")
            lines.extend(f"- {item}" for item in role.constraints)
        if role.decision_policy.priorities:
            lines.append("Decision Priorities:")
            lines.extend(f"- {item}" for item in role.decision_policy.priorities)
        if role.decision_policy.heuristics:
            lines.append("Decision Heuristics:")
            lines.extend(f"- {item}" for item in role.decision_policy.heuristics)
        if role.decision_policy.escalation_triggers:
            lines.append("Escalate When:")
            lines.extend(f"- {item}" for item in role.decision_policy.escalation_triggers)
        if role.collaboration.expects:
            lines.append("Expected Inputs:")
            lines.extend(f"- {item}" for item in role.collaboration.expects)
        if role.collaboration.handoff_outputs:
            lines.append("Handoff Outputs:")
            lines.extend(f"- {item}" for item in role.collaboration.handoff_outputs)
        if role.collaboration.asks_for_help_when:
            lines.append("Ask For Help When:")
            lines.extend(f"- {item}" for item in role.collaboration.asks_for_help_when)
        style_segments: List[str] = []
        if role.style.tone:
            style_segments.append(f"tone={role.style.tone}")
        if role.style.verbosity:
            style_segments.append(f"verbosity={role.style.verbosity}")
        if role.style.audience:
            style_segments.append(f"audience={role.style.audience}")
        if role.style.format_preference:
            style_segments.append(f"format={role.style.format_preference}")
        if style_segments:
            lines.append(f"Style: {', '.join(style_segments)}")
        if skills:
            lines.append("Skills:")
            for skill in skills:
                lines.append(f"- {skill.name}: {skill.description or skill.intent.category}")
                if skill.instructions.system:
                    lines.append(f"  System: {skill.instructions.system}")
                for step in skill.instructions.procedure:
                    lines.append(f"  Step: {step}")
        if tools:
            lines.append("Available Tools:")
            for tool in tools:
                tool_caps = ", ".join(tool.capabilities) if tool.capabilities else tool.kind
                lines.append(f"- {tool.name} ({tool.tool_id}): {tool_caps}")
        if assignment_lines:
            lines.extend(assignment_lines)
        lines.append("Produce a clear, structured result for the current task input.")
        return "\n".join(lines)

    def _build_assignment_lines(self, assignment: Dict[str, Any]) -> List[str]:
        if not assignment:
            return []
        lines = ["Current Assignment:"]
        focus = assignment.get("focus")
        if isinstance(focus, str) and focus:
            lines.append(f"- Focus: {focus}")
        deliverable = assignment.get("deliverable")
        if isinstance(deliverable, str) and deliverable:
            lines.append(f"- Deliverable: {deliverable}")
        handoff_goal = assignment.get("handoff_goal")
        if isinstance(handoff_goal, str) and handoff_goal:
            lines.append(f"- Handoff Goal: {handoff_goal}")
        owned_topics = assignment.get("owned_topics")
        if isinstance(owned_topics, list) and owned_topics:
            lines.append("- Owned Topics:")
            lines.extend(f"  - {item}" for item in owned_topics if isinstance(item, str) and item)
        additional_notes = assignment.get("notes")
        if isinstance(additional_notes, str) and additional_notes:
            lines.append(f"- Notes: {additional_notes}")
        return lines


class AssemblyCompiler:
    def __init__(
        self,
        registry: SpecRegistry,
        *,
        builtin_blocks: Optional[Dict[str, WorkflowBlockSpec]] = None,
    ) -> None:
        self.registry = registry
        self.builtin_blocks = builtin_blocks or build_builtin_block_catalog()
        self.template_compiler = TemplateCompiler(registry)

    def compile_to_template(
        self,
        assembly: WorkflowAssemblySpec,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> WorkflowTemplateSpec:
        resolved_parameters = self._resolve_parameters(assembly, parameters or {})
        entrypoint = self._resolve_entrypoint(assembly)
        self._validate_assembly_constraints(assembly)
        template_agents: List[TemplateAgentSpec] = []
        template_nodes: List[TemplateNodeSpec] = []
        for block in assembly.blocks:
            block_spec = self._resolve_block_spec(block.ref)
            compile_config = _deep_merge(block_spec.compile.config, block.config)
            compile_metadata = _deep_merge(block_spec.compile.metadata, block.metadata)
            local_activation = block.local_activation or block_spec.local_activation

            if block_spec.compile.node_kind == "agent":
                agent_ref = block.agent or (block_spec.uses.agents[0] if block_spec.uses.agents else None)
                if not agent_ref:
                    raise ValueError(f"assembly block {block.id} requires an agent binding")
                self.registry.get_agent(agent_ref)
                assignment = _deep_merge(
                    {
                        "block_label": block_spec.label,
                        "block_kind": block_spec.kind,
                        "block_type": block_spec.type,
                        "assembly_goal": assembly.goal,
                    },
                    block.assignment,
                )
                template_agents.append(
                    TemplateAgentSpec.model_validate(
                        {
                            "id": block.id,
                            "ref": agent_ref,
                            "assignment": _render_template_value(assignment, resolved_parameters),
                            "config_patch": self._materialize_agent_config_patch(
                                block_spec,
                                block,
                                compile_config,
                                resolved_parameters,
                            ),
                            "metadata": _render_template_value(
                                {
                                    "assembly_block_ref": block.ref,
                                    "assembly_block_kind": block_spec.kind,
                                    "assembly_block_type": block_spec.type,
                                    "assembly_builtin_block": bool(block_spec.metadata.get("builtin")),
                                    "assembly_block_metadata": compile_metadata,
                                },
                                resolved_parameters,
                            ),
                            "local_activation": local_activation.model_dump(mode="python")
                            if local_activation is not None
                            else None,
                        }
                    )
                )
                continue

            template_nodes.append(
                TemplateNodeSpec.model_validate(
                    {
                        "id": block.id,
                        "type": block_spec.compile.node_type,
                        "label": block_spec.label,
                        "config": _render_template_value(
                            self._materialize_static_node_config(block_spec, block, compile_config),
                            resolved_parameters,
                        ),
                        "inputs": block_spec.inputs,
                        "outputs": block_spec.outputs,
                        "local_activation": local_activation.model_dump(mode="python")
                        if local_activation is not None
                        else None,
                        "metadata": _render_template_value(
                            {
                                "assembly_block_ref": block.ref,
                                "assembly_block_kind": block_spec.kind,
                                "assembly_block_type": block_spec.type,
                                "assembly_builtin_block": bool(block_spec.metadata.get("builtin")),
                                "assembly_block_metadata": compile_metadata,
                            },
                            resolved_parameters,
                        ),
                    }
                )
            )

        return WorkflowTemplateSpec.model_validate(
            {
                "template_id": assembly.assembly_id,
                "version": assembly.version,
                "name": _render_template_value(assembly.name, resolved_parameters),
                "description": _render_template_value(assembly.description, resolved_parameters),
                "parameters": {
                    key: value.model_dump(mode="python") for key, value in assembly.parameters.items()
                },
                "agents": [agent.model_dump(mode="python") for agent in template_agents],
                "nodes": [node.model_dump(mode="python") for node in template_nodes],
                "flow": {
                    "entrypoint": entrypoint,
                    "edges": [
                        {
                            "from": link.from_id,
                            "to": link.to_id,
                            "type": link.type,
                            "condition": _render_template_value(link.condition, resolved_parameters),
                            "metadata": _render_template_value(link.metadata, resolved_parameters),
                        }
                        for link in assembly.links
                    ],
                },
                "defaults": _render_template_value(assembly.defaults, resolved_parameters),
                "metadata": {
                    **_render_template_value(assembly.metadata, resolved_parameters),
                    "assembly_id": assembly.assembly_id,
                    "assembly_version": assembly.version,
                    "assembly_goal": _render_template_value(assembly.goal, resolved_parameters),
                    "assembly_entrypoint": entrypoint,
                    "assembly_overlays": list(assembly.overlays),
                    "assembly_constraints": assembly.constraints.model_dump(mode="python"),
                    "assembly_blocks": [
                        {
                            "id": block.id,
                            "ref": block.ref,
                            "agent": block.agent,
                            "config": _render_template_value(block.config, resolved_parameters),
                        }
                        for block in assembly.blocks
                    ],
                    # Phase 2 wire-back: block_id→node_id mapping for
                    # ActivationTrainingSample assembly field population.
                    "assembly_block_node_map": {
                        block.ref: block.id for block in assembly.blocks
                    },
                },
            }
        )

    def compile(
        self,
        assembly: WorkflowAssemblySpec,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> WorkflowDefinition:
        template = self.compile_to_template(assembly, parameters=parameters)
        return self.template_compiler.compile(template, parameters=parameters)

    def _resolve_parameters(
        self,
        assembly: WorkflowAssemblySpec,
        provided: Dict[str, Any],
    ) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {}
        for key, spec in assembly.parameters.items():
            if key in provided:
                resolved[key] = self.template_compiler._coerce_parameter_value(key, provided[key], spec)
                continue
            if spec.default is not None:
                resolved[key] = deepcopy(spec.default)
                continue
            if spec.required:
                raise ValueError(f"missing required assembly parameter: {key}")
        for key, value in provided.items():
            if key not in resolved:
                resolved[key] = value
        return resolved

    def _resolve_block_spec(self, block_ref: str) -> WorkflowBlockSpec:
        block = self.registry.blocks.get(block_ref)
        if block is not None:
            return block
        builtin = self.builtin_blocks.get(block_ref)
        if builtin is not None:
            return builtin
        raise KeyError(f"block spec not found: {block_ref}")

    def _resolve_entrypoint(self, assembly: WorkflowAssemblySpec) -> str:
        if assembly.entrypoint:
            return assembly.entrypoint
        inbound_ids = {link.to_id for link in assembly.links if link.to_id != "END"}
        candidates = [block.id for block in assembly.blocks if block.id not in inbound_ids]
        if len(candidates) != 1:
            raise ValueError("assembly must define a unique entrypoint or infer exactly one root block")
        return candidates[0]

    def _validate_assembly_constraints(self, assembly: WorkflowAssemblySpec) -> None:
        if assembly.constraints.allowed_overlays:
            unsupported_overlays = sorted(set(assembly.overlays) - set(assembly.constraints.allowed_overlays))
            if unsupported_overlays:
                raise ValueError(
                    f"assembly overlays are not allowed by constraints: {', '.join(unsupported_overlays)}"
                )
        if assembly.constraints.allowed_block_kinds:
            allowed_kinds = set(assembly.constraints.allowed_block_kinds)
            invalid_blocks = []
            for block in assembly.blocks:
                block_spec = self._resolve_block_spec(block.ref)
                if block_spec.kind not in allowed_kinds:
                    invalid_blocks.append(f"{block.id}:{block_spec.kind}")
            if invalid_blocks:
                raise ValueError(
                    "assembly references block kinds outside constraints.allowed_block_kinds: "
                    + ", ".join(invalid_blocks)
                )

    def _materialize_agent_config_patch(
        self,
        block_spec: WorkflowBlockSpec,
        block: WorkflowAssemblyBlockSpec,
        config: Dict[str, Any],
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        runtime_config = deepcopy(config)
        child_template_id = runtime_config.pop("child_template", None)
        child_parameters = runtime_config.pop("child_parameters", {})
        if child_template_id:
            child_template = self.registry.get_template(str(child_template_id))
            merged_child_parameters = _deep_merge(parameters, child_parameters)
            child_workflow = self.template_compiler.compile(child_template, parameters=merged_child_parameters)
            delegated_payload: Dict[str, Any] = {
                "workflow": child_workflow.model_dump(mode="python", by_alias=True),
                "context_mode": runtime_config.pop("context_mode", "inherit"),
            }
            delegated_input = runtime_config.pop("input", None)
            delegated_context = runtime_config.pop("context", None)
            if delegated_input is not None:
                delegated_payload["input"] = delegated_input
            if delegated_context is not None:
                delegated_payload["context"] = delegated_context
            if "handoff_goal" in block.assignment:
                delegated_payload["handoff_goal"] = block.assignment["handoff_goal"]
            runtime_config["delegated"] = delegated_payload
        runtime_config.setdefault(
            "assembly_block",
            {
                "block_ref": block.ref,
                "block_kind": block_spec.kind,
                "block_type": block_spec.type,
            },
        )
        return runtime_config

    def _materialize_static_node_config(
        self,
        block_spec: WorkflowBlockSpec,
        block: WorkflowAssemblyBlockSpec,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        runtime_config = deepcopy(config)
        runtime_config.setdefault(
            "assembly_block",
            {
                "block_ref": block.ref,
                "block_kind": block_spec.kind,
                "block_type": block_spec.type,
            },
        )
        return runtime_config


def summarize_workflow_definition(workflow: WorkflowDefinition) -> Dict[str, Any]:
    tool_refs: List[str] = []
    skill_refs: List[str] = []
    agents: List[Dict[str, Any]] = []
    stage_ids: List[str] = []
    lanes: List[str] = []
    activation_modes: List[str] = []
    activation_tags: List[str] = []
    delegate_candidates: List[str] = []
    for node in workflow.nodes:
        config = node.config or {}
        node_tools = list(config.get("tool_refs", []) or [])
        node_skills = list(config.get("skill_refs", []) or [])
        node_activation = config.get("local_activation", {}) or {}
        tool_refs.extend(node_tools)
        skill_refs.extend(node_skills)
        stage = config.get("template_stage", {}) or {}
        if isinstance(stage.get("stage_id"), str) and stage["stage_id"]:
            stage_ids.append(stage["stage_id"])
        if isinstance(stage.get("lane"), str) and stage["lane"]:
            lanes.append(stage["lane"])
        if isinstance(node_activation.get("mode"), str) and node_activation["mode"]:
            activation_modes.append(node_activation["mode"])
        node_tags = node_activation.get("tags", []) or []
        if isinstance(node_tags, list):
            activation_tags.extend(item for item in node_tags if isinstance(item, str) and item)
        node_delegate_candidates = node_activation.get("delegate_candidates", []) or []
        if isinstance(node_delegate_candidates, list):
            delegate_candidates.extend(
                item for item in node_delegate_candidates if isinstance(item, str) and item
            )
        executor = config.get("executor", {}) or {}
        agents.append(
            {
                "node_id": node.id,
                "type": node.type,
                "agent_ref": config.get("agent_ref"),
                "role_ref": config.get("role_ref"),
                "assignment": config.get("assignment", {}),
                "stage": stage,
                "policy": config.get("template_policy"),
                "local_activation": node_activation,
                "tool_refs": node_tools,
                "skill_refs": node_skills,
                "executor": {
                    "kind": executor.get("kind"),
                    "provider": executor.get("provider"),
                    "model": executor.get("model"),
                },
            }
        )

    return {
        "workflow_id": workflow.workflow_id,
        "entrypoint": workflow.entrypoint,
        "node_count": len(workflow.nodes),
        "edge_count": len(workflow.edges),
        "agents": agents,
        "tool_refs": sorted(set(tool_refs)),
        "skill_refs": sorted(set(skill_refs)),
        "stage_ids": sorted(set(stage_ids)),
        "lanes": sorted(set(lanes)),
        "activation_modes": sorted(set(activation_modes)),
        "activation_tags": sorted(set(activation_tags)),
        "delegate_candidates": sorted(set(delegate_candidates)),
    }


def render_workflow_summary_text(summary: Dict[str, Any]) -> str:
    lines = [
        f"Workflow: {summary['workflow_id']}",
        f"Entrypoint: {summary['entrypoint']}",
        f"Nodes: {summary['node_count']}",
        f"Edges: {summary['edge_count']}",
    ]
    if summary.get("tool_refs"):
        lines.append(f"Tools: {', '.join(summary['tool_refs'])}")
    if summary.get("skill_refs"):
        lines.append(f"Skills: {', '.join(summary['skill_refs'])}")
    if summary.get("stage_ids"):
        lines.append(f"Stages: {', '.join(summary['stage_ids'])}")
    if summary.get("lanes"):
        lines.append(f"Lanes: {', '.join(summary['lanes'])}")
    if summary.get("activation_modes"):
        lines.append(f"Activation Modes: {', '.join(summary['activation_modes'])}")
    if summary.get("activation_tags"):
        lines.append(f"Activation Tags: {', '.join(summary['activation_tags'])}")
    if summary.get("delegate_candidates"):
        lines.append(f"Delegate Candidates: {', '.join(summary['delegate_candidates'])}")
    lines.append("Agents:")
    for agent in summary.get("agents", []):
        executor = agent.get("executor", {}) or {}
        provider = executor.get("provider") or "unknown"
        kind = executor.get("kind") or "unknown"
        stage = agent.get("stage", {}) or {}
        activation = agent.get("local_activation", {}) or {}
        stage_label = ""
        if stage.get("stage_id"):
            stage_label = f", stage={stage.get('stage_id')}/{stage.get('lane', 'default')}"
        activation_label = ""
        if activation.get("mode") and activation.get("mode") != "always":
            activation_label = f", activation={activation.get('mode')}"
        lines.append(
            f"- {agent.get('node_id')}: {agent.get('agent_ref')} "
            f"[role={agent.get('role_ref')}, executor={kind}/{provider}{stage_label}{activation_label}]"
        )
    return "\n".join(lines)


def infer_registry_root(template_ref: str | Path, registry_root: str | Path | None = None) -> Path:
    if registry_root is not None:
        return Path(registry_root)

    template_path = Path(template_ref)
    if template_path.exists():
        if template_path.parent.name == "templates":
            return template_path.parent.parent
        return template_path.parent
    return Path.cwd()


def scaffold_tool_spec(tool_id: str, *, kind: str = "builtin") -> Dict[str, Any]:
    runtime: Dict[str, Any]
    if kind == "cli":
        runtime = {"command": tool_id}
    elif kind == "mcp":
        runtime = {"endpoint": f"mcp://{tool_id}"}
    elif kind == "api":
        runtime = {"provider": tool_id}
    else:
        runtime = {"builtin": tool_id}
    return {
        "tool_id": tool_id,
        "version": "0.1",
        "kind": kind,
        "name": tool_id.replace("_", " ").title(),
        "description": "",
        "capabilities": [],
        "runtime": runtime,
        "io": {
            "input_schema": {},
            "output_schema": {},
        },
        "policy": {
            "trust_level": "internal" if kind == "builtin" else "external",
            "side_effects": "read_only",
        },
        "metadata": {},
    }


def scaffold_skill_spec(skill_id: str) -> Dict[str, Any]:
    return {
        "skill_id": skill_id,
        "version": "0.1",
        "name": skill_id.replace("_", " ").title(),
        "description": "",
        "intent": {
            "category": "general",
            "triggers": [],
        },
        "instructions": {
            "system": None,
            "procedure": [],
        },
        "quality_bar": {
            "must_check": [],
        },
        "recommended_tools": [],
        "output_contract": {
            "format": None,
            "sections": [],
        },
        "fallback": {
            "on_missing_context": {},
        },
        "metadata": {},
    }


BUILTIN_ROLE_PRESET_IDS = [
    "planner",
    "coder",
    "reviewer",
    "researcher",
    "publisher",
    "qa",
    "claw",
]


def list_builtin_role_presets() -> List[Dict[str, str]]:
    return [
        {"preset_id": "planner", "name": "Planner", "description": "偏范围、拆解、排序与交付节奏的规划角色。"},
        {"preset_id": "coder", "name": "Coder", "description": "偏执行、实现、收敛与可交付代码的工程角色。"},
        {"preset_id": "reviewer", "name": "Reviewer", "description": "偏风险识别、质量把关与回归预防的审查角色。"},
        {"preset_id": "researcher", "name": "Researcher", "description": "偏信息收集、证据组织与主题综合的研究角色。"},
        {"preset_id": "publisher", "name": "Publisher", "description": "偏整理输出、面向受众表达与发布打磨的收尾角色。"},
        {"preset_id": "qa", "name": "QA", "description": "偏验证、找边界、找回归与可复现问题的质量角色。"},
        {
            "preset_id": "claw",
            "name": "Claw",
            "description": "偏 Claw 类 Agent 调度、内建能力使用与结构化交付的天然角色。",
        },
    ]


def scaffold_role_spec(role_id: str, *, preset: Optional[str] = None) -> Dict[str, Any]:
    payload = {
        "role_id": role_id,
        "version": "0.1",
        "name": role_id.replace("_", " ").title(),
        "extends": None,
        "description": "",
        "objectives": [],
        "responsibilities": [],
        "constraints": [],
        "style": {
            "tone": None,
            "verbosity": None,
            "audience": None,
            "format_preference": None,
        },
        "decision_policy": {
            "priorities": [],
            "heuristics": [],
            "escalation_triggers": [],
        },
        "collaboration": {
            "expects": [],
            "handoff_outputs": [],
            "asks_for_help_when": [],
        },
        "metadata": {},
    }
    if not preset:
        return payload

    preset_map: Dict[str, Dict[str, Any]] = {
        "planner": {
            "name": "Planner",
            "description": "Focused on scope, sequencing, and execution clarity.",
            "objectives": [
                "Clarify the target outcome before work starts.",
                "Turn ambiguous work into an ordered execution plan.",
            ],
            "responsibilities": [
                "Define milestones and decision points.",
                "Surface tradeoffs before implementation begins.",
            ],
            "constraints": ["Do not jump into implementation details too early."],
            "style": {
                "tone": "structured",
                "verbosity": "medium",
                "audience": "builders",
                "format_preference": "plan",
            },
            "decision_policy": {
                "priorities": ["clarity", "sequence", "risk awareness"],
                "heuristics": [
                    "Prefer explicit milestones over vague next steps.",
                    "Expose hidden assumptions early.",
                ],
                "escalation_triggers": [
                    "Task scope is underspecified.",
                    "Success criteria are ambiguous.",
                ],
            },
            "collaboration": {
                "expects": ["goal", "constraints", "current context"],
                "handoff_outputs": ["plan", "implementation brief"],
                "asks_for_help_when": ["requirements conflict", "scope keeps changing"],
            },
        },
        "coder": {
            "name": "Coder",
            "description": "Focused on implementing changes cleanly and pragmatically.",
            "objectives": [
                "Translate a plan into concrete implementation work.",
                "Preserve correctness while keeping momentum high.",
            ],
            "responsibilities": [
                "Inspect the working context before changing it.",
                "Return a usable implementation summary.",
            ],
            "constraints": ["Do not hide uncertainty or skip important caveats."],
            "style": {
                "tone": "pragmatic",
                "verbosity": "medium",
                "audience": "engineers",
                "format_preference": "implementation report",
            },
            "decision_policy": {
                "priorities": ["correctness", "clarity", "forward progress"],
                "heuristics": [
                    "Prefer small coherent changes over broad speculative rewrites.",
                    "Work from observed context, not assumptions.",
                ],
                "escalation_triggers": [
                    "Required context is missing.",
                    "The requested change conflicts with existing constraints.",
                ],
            },
            "collaboration": {
                "expects": ["plan", "task goal", "available tools"],
                "handoff_outputs": ["implementation summary", "open issues"],
                "asks_for_help_when": ["requirements are contradictory", "risk is unusually high"],
            },
        },
        "reviewer": {
            "name": "Reviewer",
            "description": "Focused on risk detection, quality gates, and honest assessment.",
            "objectives": [
                "Find bugs, regressions, and hidden risk.",
                "Explain ship readiness clearly.",
            ],
            "responsibilities": [
                "Check behavior, not just surface polish.",
                "Call out missing tests or unsupported assumptions.",
            ],
            "constraints": ["Do not turn review into implementation unless requested."],
            "style": {
                "tone": "direct",
                "verbosity": "medium",
                "audience": "engineering team",
                "format_preference": "findings-first review",
            },
            "decision_policy": {
                "priorities": ["correctness", "risk", "behavioral regression"],
                "heuristics": [
                    "Lead with findings before summaries.",
                    "Prefer concrete evidence over vague concern.",
                ],
                "escalation_triggers": [
                    "Behavior change is unclear.",
                    "A critical path lacks tests or verification.",
                ],
            },
            "collaboration": {
                "expects": ["implementation summary", "relevant context", "claimed outcome"],
                "handoff_outputs": ["findings", "risk summary", "ship readiness"],
                "asks_for_help_when": ["the intended behavior is not specified"],
            },
        },
        "researcher": {
            "name": "Researcher",
            "description": "Focused on gathering, organizing, and synthesizing evidence.",
            "objectives": [
                "Turn a question into a structured evidence set.",
                "Produce synthesis that downstream roles can reuse.",
            ],
            "responsibilities": [
                "Separate facts, interpretations, and open questions.",
                "Organize findings into clear themes.",
            ],
            "constraints": ["Do not overstate confidence beyond the evidence."],
            "style": {
                "tone": "analytical",
                "verbosity": "medium",
                "audience": "decision makers",
                "format_preference": "brief",
            },
            "decision_policy": {
                "priorities": ["evidence quality", "coverage", "clarity"],
                "heuristics": [
                    "Keep source-backed claims separate from inference.",
                    "Prefer synthesis over dumping raw notes.",
                ],
                "escalation_triggers": [
                    "Available sources conflict heavily.",
                    "Important source material is missing.",
                ],
            },
            "collaboration": {
                "expects": ["question", "scope", "available sources"],
                "handoff_outputs": ["research brief", "open questions"],
                "asks_for_help_when": ["scope is too broad to answer cleanly"],
            },
        },
        "publisher": {
            "name": "Publisher",
            "description": "Focused on packaging final output for the intended audience.",
            "objectives": [
                "Turn reviewed material into a polished final artifact.",
                "Optimize the output for its target audience and format.",
            ],
            "responsibilities": [
                "Tighten structure, clarity, and completeness.",
                "Ensure the final piece is ready to hand off or publish.",
            ],
            "constraints": ["Do not invent unsupported claims during polishing."],
            "style": {
                "tone": "clear",
                "verbosity": "medium",
                "audience": "end users",
                "format_preference": "publishable output",
            },
            "decision_policy": {
                "priorities": ["clarity", "coherence", "completeness"],
                "heuristics": [
                    "Prefer strong structure over ornamental wording.",
                    "Package for the audience actually receiving the output.",
                ],
                "escalation_triggers": [
                    "The source material is still internally inconsistent.",
                ],
            },
            "collaboration": {
                "expects": ["reviewed content", "target audience", "desired format"],
                "handoff_outputs": ["final artifact"],
                "asks_for_help_when": ["source content is not publication-ready"],
            },
        },
        "qa": {
            "name": "QA",
            "description": "Focused on testing, edge cases, and reproducible failures.",
            "objectives": [
                "Verify expected behavior under realistic conditions.",
                "Catch regressions and edge cases before release.",
            ],
            "responsibilities": [
                "Test normal flow, edge cases, and failure paths.",
                "Return reproducible evidence for issues found.",
            ],
            "constraints": ["Do not mark work as healthy without evidence."],
            "style": {
                "tone": "skeptical",
                "verbosity": "medium",
                "audience": "engineering team",
                "format_preference": "qa report",
            },
            "decision_policy": {
                "priorities": ["reproducibility", "risk", "coverage"],
                "heuristics": [
                    "Prefer specific repro steps over generic concern.",
                    "Actively probe failure modes, not just happy paths.",
                ],
                "escalation_triggers": [
                    "Expected behavior is undocumented.",
                    "A bug cannot be reproduced reliably with available context.",
                ],
            },
            "collaboration": {
                "expects": ["target behavior", "test scope", "environment context"],
                "handoff_outputs": ["qa findings", "repro steps", "health assessment"],
                "asks_for_help_when": ["acceptance criteria are missing"],
            },
        },
        "claw": {
            "name": "Claw",
            "description": "Focused on operating a Claw-style agent runtime with native tools and structured execution habits.",
            "objectives": [
                "Use native agent affordances to complete end-to-end work with minimal glue code.",
                "Return structured outcomes that downstream workflow stages can trust.",
            ],
            "responsibilities": [
                "Operate as a runtime-native agent rather than a plain text completion worker.",
                "Make effective use of built-in capabilities such as file inspection, shell access, or patching when available.",
            ],
            "constraints": [
                "Do not assume every Claw runtime exposes the same command contract without adapter confirmation.",
                "Do not hide tool usage or side effects behind vague summaries.",
            ],
            "style": {
                "tone": "pragmatic",
                "verbosity": "medium",
                "audience": "operators and workflow peers",
                "format_preference": "execution summary",
            },
            "decision_policy": {
                "priorities": ["task completion", "traceability", "safe tool usage"],
                "heuristics": [
                    "Prefer runtime-native capabilities before inventing extra orchestration layers.",
                    "Keep outputs structured enough for workflow handoff and resume.",
                ],
                "escalation_triggers": [
                    "The Claw runtime contract is missing or inconsistent.",
                    "A required side effect exceeds the currently approved trust boundary.",
                ],
            },
            "collaboration": {
                "expects": ["goal", "runtime contract", "available permissions"],
                "handoff_outputs": ["execution result", "artifacts", "open risks"],
                "asks_for_help_when": ["runtime capabilities are unclear", "required permissions are missing"],
            },
            "metadata": {
                "role_family": "claw",
                "native_runtime_role": True,
            },
        },
    }
    if preset not in preset_map:
        raise KeyError(f"unsupported role preset: {preset}")
    payload = _deep_merge(payload, preset_map[preset])
    payload["metadata"] = _deep_merge(payload.get("metadata", {}), {"preset_id": preset})
    return payload


def scaffold_agent_spec(
    agent_id: str,
    *,
    role: Optional[str] = None,
    skills: Optional[List[str]] = None,
    tools: Optional[List[str]] = None,
    provider: Optional[str] = "claude",
    executor_kind: Optional[str] = "cli",
    preset: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "agent_id": agent_id,
        "version": "0.1",
        "name": agent_id.replace("_", " ").title(),
        "role": role or "agent",
        "skills": skills or [],
        "tools": tools or [],
        "prompt_template": None,
        "node_type": "agent.execute",
        "executor": {
            "kind": executor_kind or "cli",
            "provider": provider or "claude",
        },
        "memory": {
            "scope": "session",
        },
        "policy": {
            "autonomy": "medium",
            "allow_side_effects": False,
        },
        "io": {
            "accepts": [],
            "produces": [],
        },
        "metadata": {},
    }
    if not preset:
        if not role:
            raise ValueError("agent scaffold requires role when no preset is provided")
        return payload

    preset_map: Dict[str, Dict[str, Any]] = {
        "openclaw": {
            "name": "OpenClaw",
            "role": "claw",
            "prompt_template": "Operate as an OpenClaw-style native agent.\nGoal: {{ goal }}",
            "executor": {
                "kind": "cli",
                "provider": "openclaw",
                "command": "openclaw",
                "stdin": "text",
                "parse": "text",
            },
            "policy": {
                "autonomy": "high",
                "allow_side_effects": False,
            },
            "metadata": {
                "preset_id": "openclaw",
                "agent_family": "claw",
                "native_runtime": True,
                "runtime_provider_hint": "openclaw",
            },
        },
        "shadowclaw": {
            "name": "ShadowClaw",
            "role": "claw",
            "prompt_template": "Operate as a ShadowClaw-style native agent.\nGoal: {{ goal }}",
            "executor": {
                "kind": "cli",
                "provider": "shadowclaw",
                "command": "shadowclaw",
                "stdin": "text",
                "parse": "text",
            },
            "memory": {
                "scope": "session",
                "writeback_target": "memory",
            },
            "policy": {
                "autonomy": "high",
                "allow_side_effects": False,
            },
            "metadata": {
                "preset_id": "shadowclaw",
                "agent_family": "claw",
                "native_runtime": True,
                "runtime_provider_hint": "shadowclaw",
            },
        },
    }
    if preset not in preset_map:
        raise KeyError(f"unsupported agent preset: {preset}")
    payload = _deep_merge(payload, preset_map[preset])
    if role:
        payload["role"] = role
    if provider:
        payload["executor"]["provider"] = provider
    if executor_kind:
        payload["executor"]["kind"] = executor_kind
    return payload


BUILTIN_AGENT_PRESET_IDS = [
    "openclaw",
    "shadowclaw",
]


def list_builtin_agent_presets() -> List[Dict[str, str]]:
    return [
        {
            "preset_id": "openclaw",
            "name": "OpenClaw",
            "description": "Claw 类原生 Agent 预设，默认走 CLI 调用并保留 runtime-native agent 身份。",
        },
        {
            "preset_id": "shadowclaw",
            "name": "ShadowClaw",
            "description": "Shadow 侧 Claw 类 Agent 预设，保留记忆写回倾向与 Claw family 元数据。",
        },
    ]


def scaffold_template_spec(
    template_id: str,
    *,
    agent_ref: str,
    agent_node_id: str = "agent",
) -> Dict[str, Any]:
    return {
        "template_id": template_id,
        "version": "0.1",
        "name": template_id.replace("_", " ").title(),
        "description": "",
        "parameters": {
            "goal": {
                "type": "string",
                "required": True,
            }
        },
        "agents": [
            {
                "id": agent_node_id,
                "ref": agent_ref,
                "assignment": {},
            }
        ],
        "flow": {
            "entrypoint": agent_node_id,
            "enforce_stage_order": True,
            "edges": [
                {
                    "from": agent_node_id,
                    "to": "END",
                    "type": "final",
                }
            ],
        },
        "defaults": {
            "memory_scope": "session",
        },
        "metadata": {},
    }


BUILTIN_PRESET_IDS = [
    "single-reviewer",
    "planner-coder-reviewer",
    "research-review-publish",
]


def list_builtin_presets() -> List[Dict[str, str]]:
    return [
        {
            "preset_id": "single-reviewer",
            "name": "Single Reviewer",
            "description": "单 Agent 评审/分析流，适合从目标到结论的一次性任务。",
        },
        {
            "preset_id": "planner-coder-reviewer",
            "name": "Planner Coder Reviewer",
            "description": "三段式计划-执行-复核链路，适合代码与方案类任务。",
        },
        {
            "preset_id": "research-review-publish",
            "name": "Research Review Publish",
            "description": "研究-复核-发布链路，适合调研、内容整理与输出。",
        },
    ]


def _scaffold_builtin_preset_bundle(
    preset_id: str,
    *,
    workflow_id: str,
    provider: str,
    executor_kind: str,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    workflow_slug = _slugify(workflow_id)
    base_tool = scaffold_tool_spec("filesystem", kind="builtin")
    base_tool["description"] = "Read and inspect local project files."
    base_tool["capabilities"] = ["read", "list", "search"]

    browser_tool = scaffold_tool_spec("browser", kind="builtin")
    browser_tool["description"] = "Inspect web pages and capture evidence."
    browser_tool["capabilities"] = ["navigate", "inspect", "screenshot"]

    writer_tool = scaffold_tool_spec("writer_api", kind="api")
    writer_tool["description"] = "Generate and polish publishable content."
    writer_tool["runtime"] = {"provider": provider}
    writer_tool["capabilities"] = ["draft", "rewrite", "summarize"]
    writer_tool["policy"]["side_effects"] = "write"

    bundle: Dict[str, Dict[str, Dict[str, Any]]] = {
        "tools": {},
        "skills": {},
        "roles": {},
        "agents": {},
        "templates": {},
    }

    if preset_id == "single-reviewer":
        skill_id = f"{workflow_slug}_review"
        role_id = f"{workflow_slug}_reviewer"
        agent_id = f"{workflow_slug}_reviewer_agent"
        template_id = workflow_slug

        skill = scaffold_skill_spec(skill_id)
        skill["description"] = "Review a target goal and return a structured assessment."
        skill["intent"]["category"] = "review"
        skill["instructions"]["procedure"] = [
            "Clarify the task goal.",
            "Inspect the available context and evidence.",
            "Return findings, risks, and recommended next actions.",
        ]
        skill["output_contract"] = {"format": "markdown", "sections": ["summary", "findings", "next_steps"]}

        role = scaffold_role_spec(role_id)
        role["description"] = "Structured reviewer focused on clear recommendations."
        role["responsibilities"] = [
            "Assess the task goal against available context.",
            "Explain tradeoffs and risks clearly.",
        ]

        agent = scaffold_agent_spec(
            agent_id,
            role=role_id,
            skills=[skill_id],
            tools=["filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        agent["prompt_template"] = "Review the request carefully.\nGoal: {{ goal }}"

        template = scaffold_template_spec(template_id, agent_ref=agent_id, agent_node_id="reviewer")
        template["name"] = "Single Reviewer"
        template["description"] = "Single-agent review template."
        template["agents"][0]["assignment"] = {
            "focus": "Provide the primary review and recommendation for the goal.",
            "deliverable": "Structured review summary",
            "handoff_goal": "End the workflow with a clear assessment.",
        }
        template["policy_matrix"] = {
            "agents": {
                "reviewer": {
                    "tools": ["filesystem"],
                    "side_effects": "read_only",
                }
            }
        }
        template["stages"] = [
            {
                "stage_id": "review",
                "name": "Review",
                "lane": "analysis",
                "agents": ["reviewer"],
            }
        ]

        bundle["tools"]["filesystem"] = base_tool
        bundle["skills"][skill_id] = skill
        bundle["roles"][role_id] = role
        bundle["agents"][agent_id] = agent
        bundle["templates"][template_id] = template
        return bundle

    if preset_id == "planner-coder-reviewer":
        planner_skill_id = f"{workflow_slug}_planning"
        coder_skill_id = f"{workflow_slug}_implementation"
        reviewer_skill_id = f"{workflow_slug}_verification"
        planner_role_id = f"{workflow_slug}_planner"
        coder_role_id = f"{workflow_slug}_coder"
        reviewer_role_id = f"{workflow_slug}_reviewer"
        planner_agent_id = f"{workflow_slug}_planner_agent"
        coder_agent_id = f"{workflow_slug}_coder_agent"
        reviewer_agent_id = f"{workflow_slug}_reviewer_agent"

        planner_skill = scaffold_skill_spec(planner_skill_id)
        planner_skill["description"] = "Break a goal into a practical execution plan."
        planner_skill["intent"]["category"] = "planning"
        planner_skill["instructions"]["procedure"] = [
            "Clarify desired outcome and constraints.",
            "Produce an ordered plan with milestones.",
            "Hand off a concrete implementation brief.",
        ]
        planner_skill["output_contract"] = {"format": "markdown", "sections": ["goal", "plan", "handoff"]}

        coder_skill = scaffold_skill_spec(coder_skill_id)
        coder_skill["description"] = "Implement a planned change pragmatically."
        coder_skill["intent"]["category"] = "implementation"
        coder_skill["instructions"]["procedure"] = [
            "Translate the plan into implementation steps.",
            "Use available tools to inspect and modify context.",
            "Explain the resulting change and any open issues.",
        ]
        coder_skill["output_contract"] = {"format": "markdown", "sections": ["implementation", "risks", "followups"]}

        reviewer_skill = scaffold_skill_spec(reviewer_skill_id)
        reviewer_skill["description"] = "Verify the implementation result and surface risks."
        reviewer_skill["intent"]["category"] = "review"
        reviewer_skill["instructions"]["procedure"] = [
            "Assess whether the implementation matches the plan.",
            "Call out regressions, missing tests, or hidden risk.",
            "Summarize ship readiness.",
        ]
        reviewer_skill["output_contract"] = {"format": "markdown", "sections": ["summary", "findings", "ship_readiness"]}

        for role_id, description in [
            (planner_role_id, "Planning lead focused on scope and sequencing."),
            (coder_role_id, "Execution-focused engineer who turns plans into working changes."),
            (reviewer_role_id, "Independent reviewer focused on regression and risk."),
        ]:
            role = scaffold_role_spec(role_id)
            role["description"] = description
            role["responsibilities"] = [description]
            bundle["roles"][role_id] = role

        bundle["tools"]["filesystem"] = base_tool

        planner_agent = scaffold_agent_spec(
            planner_agent_id,
            role=planner_role_id,
            skills=[planner_skill_id],
            tools=["filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        planner_agent["prompt_template"] = "Plan the work clearly.\nGoal: {{ goal }}"

        coder_agent = scaffold_agent_spec(
            coder_agent_id,
            role=coder_role_id,
            skills=[coder_skill_id],
            tools=["filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        coder_agent["prompt_template"] = "Implement the requested outcome.\nGoal: {{ goal }}"

        reviewer_agent = scaffold_agent_spec(
            reviewer_agent_id,
            role=reviewer_role_id,
            skills=[reviewer_skill_id],
            tools=["filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        reviewer_agent["prompt_template"] = "Review the result rigorously.\nGoal: {{ goal }}"

        bundle["skills"][planner_skill_id] = planner_skill
        bundle["skills"][coder_skill_id] = coder_skill
        bundle["skills"][reviewer_skill_id] = reviewer_skill
        bundle["agents"][planner_agent_id] = planner_agent
        bundle["agents"][coder_agent_id] = coder_agent
        bundle["agents"][reviewer_agent_id] = reviewer_agent
        bundle["templates"][workflow_slug] = {
            "template_id": workflow_slug,
            "version": "0.1",
            "name": "Planner Coder Reviewer",
            "description": "Plan, implement, then review.",
            "parameters": {
                "goal": {"type": "string", "required": True},
            },
            "agents": [
                {
                    "id": "planner",
                    "ref": planner_agent_id,
                    "assignment": {
                        "focus": "Clarify scope, constraints, and execution order.",
                        "deliverable": "Plan and implementation brief",
                        "handoff_goal": "Give coder a clear path to execute.",
                    },
                },
                {
                    "id": "coder",
                    "ref": coder_agent_id,
                    "assignment": {
                        "focus": "Carry out the implementation implied by the plan.",
                        "deliverable": "Implementation result with open issues",
                        "handoff_goal": "Give reviewer enough detail to assess quality.",
                    },
                },
                {
                    "id": "reviewer",
                    "ref": reviewer_agent_id,
                    "assignment": {
                        "focus": "Review the implementation for risk and regression.",
                        "deliverable": "Findings-first review",
                        "handoff_goal": "Close the loop with ship-readiness guidance.",
                    },
                },
            ],
            "flow": {
                "entrypoint": "planner",
                "enforce_stage_order": True,
                "edges": [
                    {"from": "planner", "to": "coder", "type": "default"},
                    {"from": "coder", "to": "reviewer", "type": "default"},
                    {"from": "reviewer", "to": "END", "type": "final"},
                ],
            },
            "policy_matrix": {
                "agents": {
                    "planner": {
                        "tools": ["filesystem"],
                        "side_effects": "read_only",
                    },
                    "coder": {
                        "tools": ["filesystem"],
                        "side_effects": "read_only",
                    },
                    "reviewer": {
                        "tools": ["filesystem"],
                        "side_effects": "read_only",
                    },
                }
            },
            "stages": [
                {
                    "stage_id": "plan",
                    "name": "Plan",
                    "lane": "delivery",
                    "agents": ["planner"],
                },
                {
                    "stage_id": "execute",
                    "name": "Execute",
                    "lane": "delivery",
                    "agents": ["coder"],
                },
                {
                    "stage_id": "review",
                    "name": "Review",
                    "lane": "quality",
                    "agents": ["reviewer"],
                    "approval_required": True,
                },
            ],
            "defaults": {"memory_scope": "session"},
            "metadata": {"preset_id": preset_id},
        }
        return bundle

    if preset_id == "research-review-publish":
        researcher_skill_id = f"{workflow_slug}_research"
        reviewer_skill_id = f"{workflow_slug}_editorial_review"
        publisher_skill_id = f"{workflow_slug}_publish"
        researcher_role_id = f"{workflow_slug}_researcher"
        reviewer_role_id = f"{workflow_slug}_editor"
        publisher_role_id = f"{workflow_slug}_publisher"
        researcher_agent_id = f"{workflow_slug}_research_agent"
        reviewer_agent_id = f"{workflow_slug}_editor_agent"
        publisher_agent_id = f"{workflow_slug}_publisher_agent"

        researcher_skill = scaffold_skill_spec(researcher_skill_id)
        researcher_skill["description"] = "Collect and synthesize source material into a usable brief."
        researcher_skill["intent"]["category"] = "research"
        researcher_skill["instructions"]["procedure"] = [
            "Clarify the question being researched.",
            "Collect useful evidence and organize it into themes.",
            "Produce a concise research brief for downstream review.",
        ]

        reviewer_skill = scaffold_skill_spec(reviewer_skill_id)
        reviewer_skill["description"] = "Review a draft for structure, evidence, and clarity."
        reviewer_skill["intent"]["category"] = "review"
        reviewer_skill["instructions"]["procedure"] = [
            "Check the structure and evidence quality.",
            "Highlight gaps and recommended edits.",
            "Prepare the draft for publishing.",
        ]

        publisher_skill = scaffold_skill_spec(publisher_skill_id)
        publisher_skill["description"] = "Turn reviewed material into a polished publishable output."
        publisher_skill["intent"]["category"] = "publish"
        publisher_skill["instructions"]["procedure"] = [
            "Polish tone and clarity for the intended audience.",
            "Ensure the final output is complete and publication-ready.",
            "Return the final release artifact.",
        ]

        for role_id, description in [
            (researcher_role_id, "Research lead focused on gathering and synthesizing evidence."),
            (reviewer_role_id, "Editorial reviewer focused on clarity and factual support."),
            (publisher_role_id, "Publisher focused on final polish and packaging."),
        ]:
            role = scaffold_role_spec(role_id)
            role["description"] = description
            role["responsibilities"] = [description]
            bundle["roles"][role_id] = role

        bundle["tools"]["filesystem"] = base_tool
        bundle["tools"]["browser"] = browser_tool
        bundle["tools"]["writer_api"] = writer_tool

        researcher_agent = scaffold_agent_spec(
            researcher_agent_id,
            role=researcher_role_id,
            skills=[researcher_skill_id],
            tools=["browser", "filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        researcher_agent["prompt_template"] = "Research the topic and synthesize it.\nGoal: {{ goal }}"

        reviewer_agent = scaffold_agent_spec(
            reviewer_agent_id,
            role=reviewer_role_id,
            skills=[reviewer_skill_id],
            tools=["filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        reviewer_agent["prompt_template"] = "Review the research package.\nGoal: {{ goal }}"

        publisher_agent = scaffold_agent_spec(
            publisher_agent_id,
            role=publisher_role_id,
            skills=[publisher_skill_id],
            tools=["writer_api", "filesystem"],
            provider=provider,
            executor_kind=executor_kind,
        )
        publisher_agent["prompt_template"] = "Produce the final publishable artifact.\nGoal: {{ goal }}"
        publisher_agent["policy"]["allow_side_effects"] = True

        bundle["skills"][researcher_skill_id] = researcher_skill
        bundle["skills"][reviewer_skill_id] = reviewer_skill
        bundle["skills"][publisher_skill_id] = publisher_skill
        bundle["agents"][researcher_agent_id] = researcher_agent
        bundle["agents"][reviewer_agent_id] = reviewer_agent
        bundle["agents"][publisher_agent_id] = publisher_agent
        bundle["templates"][workflow_slug] = {
            "template_id": workflow_slug,
            "version": "0.1",
            "name": "Research Review Publish",
            "description": "Research, review, and package a final output.",
            "parameters": {
                "goal": {"type": "string", "required": True},
            },
            "agents": [
                {
                    "id": "research",
                    "ref": researcher_agent_id,
                    "assignment": {
                        "focus": "Collect and synthesize the source material.",
                        "deliverable": "Research brief",
                        "handoff_goal": "Give review enough evidence to assess quality.",
                    },
                },
                {
                    "id": "review",
                    "ref": reviewer_agent_id,
                    "assignment": {
                        "focus": "Check the research package for structure and gaps.",
                        "deliverable": "Editorial review notes",
                        "handoff_goal": "Prepare a clean handoff to publishing.",
                    },
                },
                {
                    "id": "publish",
                    "ref": publisher_agent_id,
                    "assignment": {
                        "focus": "Turn the reviewed package into the final artifact.",
                        "deliverable": "Publishable final output",
                        "handoff_goal": "Finish the workflow with a polished deliverable.",
                    },
                },
            ],
            "flow": {
                "entrypoint": "research",
                "enforce_stage_order": True,
                "edges": [
                    {"from": "research", "to": "review", "type": "default"},
                    {"from": "review", "to": "publish", "type": "default"},
                    {"from": "publish", "to": "END", "type": "final"},
                ],
            },
            "policy_matrix": {
                "agents": {
                    "research": {
                        "tools": ["browser", "filesystem"],
                        "side_effects": "read_only",
                    },
                    "review": {
                        "tools": ["filesystem"],
                        "side_effects": "read_only",
                    },
                    "publish": {
                        "tools": ["writer_api", "filesystem"],
                        "side_effects": "write",
                        "writeback_targets": ["docs", "memory"],
                    },
                }
            },
            "stages": [
                {
                    "stage_id": "research",
                    "name": "Research",
                    "lane": "content",
                    "agents": ["research"],
                },
                {
                    "stage_id": "review",
                    "name": "Review",
                    "lane": "quality",
                    "agents": ["review"],
                    "approval_required": True,
                },
                {
                    "stage_id": "publish",
                    "name": "Publish",
                    "lane": "release",
                    "agents": ["publish"],
                    "barrier": True,
                },
            ],
            "defaults": {"memory_scope": "session"},
            "metadata": {"preset_id": preset_id},
        }
        return bundle

    raise KeyError(f"unsupported preset: {preset_id}")


def build_builtin_preset_bundle(
    preset_id: str,
    *,
    workflow_id: str,
    provider: str = "claude",
    executor_kind: str = "cli",
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    return _scaffold_builtin_preset_bundle(
        preset_id,
        workflow_id=workflow_id,
        provider=provider,
        executor_kind=executor_kind,
    )


def write_registry_bundle(
    bundle: Dict[str, Dict[str, Dict[str, Any]]],
    registry_root: str | Path,
    *,
    force: bool = False,
) -> List[str]:
    registry_root_path = Path(registry_root)
    pending: List[tuple[Path, Dict[str, Any]]] = []
    for kind, items in bundle.items():
        for item_id, payload in items.items():
            path = registry_root_path / kind / f"{item_id}.yaml"
            if path.exists() and not force:
                raise ValueError(f"spec file already exists: {path}")
            pending.append((path, payload))

    written: List[str] = []
    for path, payload in pending:
        save_spec_file(path, payload)
        written.append(str(path))
    return written


def export_registry_bundle(
    source_root: str | Path,
    output_root: str | Path,
    *,
    kind: Optional[str] = None,
    spec_id: Optional[str] = None,
    force: bool = False,
) -> List[Path]:
    registry = SpecRegistry.load_from_root(source_root)
    output_root_path = Path(output_root)
    written: List[Path] = []
    if kind and spec_id:
        payload = registry.get_kind(kind, spec_id).model_dump(mode="python", by_alias=True, exclude_none=True)
        destination = output_root_path / kind / f"{spec_id}.yaml"
        if destination.exists() and not force:
            raise ValueError(f"destination already exists: {destination}")
        written.append(save_spec_file(destination, payload))
        return written

    for current_kind in ("tools", "skills", "roles", "agents", "blocks", "templates", "assemblies"):
        for item in registry.list_kind(current_kind):
            current_id = (
                getattr(item, "tool_id", None)
                or getattr(item, "skill_id", None)
                or getattr(item, "role_id", None)
                or getattr(item, "agent_id", None)
                or getattr(item, "block_id", None)
                or getattr(item, "template_id", None)
                or getattr(item, "assembly_id", None)
            )
            destination = output_root_path / current_kind / f"{current_id}.yaml"
            if destination.exists() and not force:
                raise ValueError(f"destination already exists: {destination}")
            written.append(save_spec_file(destination, item.model_dump(mode="python", by_alias=True, exclude_none=True)))
    return written


def import_registry_bundle(
    source_root: str | Path,
    target_root: str | Path,
    *,
    kind: Optional[str] = None,
    spec_id: Optional[str] = None,
    force: bool = False,
) -> List[Path]:
    source_root_path = Path(source_root)
    target_root_path = Path(target_root)
    written: List[Path] = []

    def _copy_file(src: Path, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            raise ValueError(f"destination already exists: {dest}")
        shutil.copy2(src, dest)
        return dest

    if kind and spec_id:
        candidates = [
            source_root_path / kind / f"{spec_id}.yaml",
            source_root_path / kind / f"{spec_id}.yml",
            source_root_path / kind / f"{spec_id}.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                written.append(_copy_file(candidate, target_root_path / kind / candidate.name))
                return written
        raise FileNotFoundError(f"spec not found in source registry: {kind}/{spec_id}")

    for current_kind in ("tools", "skills", "roles", "agents", "blocks", "templates", "assemblies"):
        source_dir = source_root_path / current_kind
        if not source_dir.exists():
            continue
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}:
                written.append(_copy_file(path, target_root_path / current_kind / path.name))
    return written


def materialize_builtin_preset(
    preset_id: str,
    registry_root: str | Path,
    *,
    workflow_id: Optional[str] = None,
    provider: str = "claude",
    executor_kind: str = "cli",
    force: bool = False,
) -> Dict[str, Any]:
    workflow_name = workflow_id or preset_id
    bundle = build_builtin_preset_bundle(
        preset_id,
        workflow_id=workflow_name,
        provider=provider,
        executor_kind=executor_kind,
    )
    registry_root_path = Path(registry_root)
    written = write_registry_bundle(bundle, registry_root_path, force=force)
    return {
        "preset_id": preset_id,
        "workflow_id": _slugify(workflow_name),
        "registry_root": str(registry_root_path),
        "written": written,
    }
