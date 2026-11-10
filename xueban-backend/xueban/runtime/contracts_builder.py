"""Builder 领域合同 — Story 8.1 (AC1)

AgentBlueprint 及其子对象是 Goal / Scene / Graph 三层编辑共同操作的中间产物。
本文件与 contracts.py 解耦：Builder 合同不持有运行时字段；
映射（blueprint → WorkflowTemplateSpec → WorkflowDefinition）发生在 builder_service.py。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 子对象
# ---------------------------------------------------------------------------


class ExecutionMode(BaseModel):
    """Story 13.2: Agent 执行方式配置（缺省 ReAct 循环，向后兼容）。"""

    mode: Literal["react", "workflow"] = "react"
    workflow_ref: Optional[str] = None
    workflow_name: Optional[str] = None


class CollaborationContract(BaseModel):
    """Story 13.5: Agent 协作合同——表达 scope 和协作边界。

    缺省时（RoleProfile.collaboration_contract = None）视为 standalone。
    向后兼容：旧 Blueprint 无此字段时不影响发布和运行。
    """

    scope: Literal["standalone", "team_member_candidate"] = "standalone"
    accepts_from: List[str] = Field(default_factory=list)
    delivers_to: List[str] = Field(default_factory=list)
    collaboration_style: Literal["push", "pull"] = "push"


class HandoffRule(BaseModel):
    trigger: str
    target_role: str  # role_id of target role


class StateField(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "json"] = "string"
    default: Any = None


class RoleProfile(BaseModel):
    role_id: str = Field(default_factory=lambda: f"role-{uuid4().hex[:8]}")
    name: str
    description: str = ""
    persona: str = ""
    responsibilities: List[str] = Field(default_factory=list)
    # RACI 分工(2026-06-01):职责桶 → R/A/C/I。与上面的 responsibilities(自由文本
    # 职责描述)正交、不同概念,故独立字段。组建时由 deriveRaci 派生写入;手动招人留空。
    # v1 职责桶:plan|draft|review|approve|gate|tool(对齐 PolicyMatrixMini)。
    raci: Dict[str, str] = Field(default_factory=dict)
    constraints: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    executor_kind: Literal["api", "cli"] = "api"
    executor_provider: str = "anthropic"
    executor_model: str = "claude-sonnet-4-6"
    # 新增：深度配置字段（Story 8.3b）
    capabilities: List[str] = Field(default_factory=list)
    handoff_rules: List[HandoffRule] = Field(default_factory=list)
    persona_traits: Dict[str, str] = Field(default_factory=dict)
    state_fields: List[StateField] = Field(default_factory=list)
    # 主管-员工层级结构
    can_spawn_tasks: bool = False
    sub_agents: List["RoleProfile"] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Story 13.5: 协作合同（可选，缺省时视为 standalone，向后兼容）
    collaboration_contract: Optional[CollaborationContract] = None

    @model_validator(mode="after")
    def infer_can_spawn(self) -> "RoleProfile":
        if self.sub_agents:
            self.can_spawn_tasks = True
        return self


# 允许自引用（递归嵌套）
RoleProfile.model_rebuild()


class PermissionRule(BaseModel):
    """Single deny/ask/allow rule with optional arg-pattern (AC4)."""

    permission: Literal["allow", "ask", "deny"]
    arg_pattern: str = ""  # e.g. "query:*小红书*"; empty = matches all args


class ToolPolicy(BaseModel):
    tool_id: str
    # MCP-specific (AC3)
    provider_id: Optional[str] = None
    credentials_ref: Optional[str] = None  # equals provider_id; no inline secrets
    visibility: Literal["enabled", "disabled"] = "enabled"
    # deny > ask > allow rules (AC4)
    permission_rules: List[PermissionRule] = Field(default_factory=list)
    default_permission: Literal["allow", "ask", "deny"] = "allow"
    # Legacy fields kept for backward compatibility
    trust_level: Literal["internal", "external"] = "external"
    side_effects: Literal["read_only", "write", "mixed"] = "read_only"
    requires_confirmation: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeBinding(BaseModel):
    binding_id: str = Field(default_factory=lambda: f"kb-{uuid4().hex[:8]}")
    source_type: Literal["file", "url", "cid", "inline", "unspecified", "pack"] = "unspecified"
    source_ref: str = ""
    citation_required: bool = False
    retrieval_mode: str = "auto"
    freshness_hint: str = "static"
    scope: Literal["shared", "agent"] = "shared"
    target_ref: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_ref(self) -> "KnowledgeBinding":
        if self.source_type != "unspecified" and not self.source_ref:
            raise ValueError(
                f"source_ref is required when source_type='{self.source_type}'"
            )
        return self


class MemoryProfile(BaseModel):
    scope: Literal["session", "user", "global"] = "session"
    writeback_target: Optional[Literal["host", "docs", "memory", "graph"]] = None
    enabled: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EvalProfile(BaseModel):
    smoke_eval_enabled: bool = False
    eval_criteria: List[str] = Field(default_factory=list)
    regression_gate: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PublishProfile(BaseModel):
    target: Literal["template", "workflow", "agent_app", "none"] = "none"
    visibility: Literal["private", "team", "public"] = "private"
    publish_ref: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_visibility_target(self) -> "PublishProfile":
        # visibility='public' + target='none' 是矛盾组合：公开发布必须有目标
        if self.visibility == "public" and self.target == "none":
            raise ValueError(
                "visibility='public' requires a non-none target; "
                "set target to 'template', 'workflow', or 'agent_app'"
            )
        return self


# ---------------------------------------------------------------------------
# 顶层 AgentBlueprint
# ---------------------------------------------------------------------------


class AgentBlueprint(BaseModel):
    blueprint_id: str = Field(default_factory=lambda: f"bp-{uuid4().hex[:12]}")
    version: str = "1.0"
    name: str
    goal: str
    audience: str = ""
    mode: Literal["single", "team"] = "single"
    role_profiles: List[RoleProfile] = Field(default_factory=list)
    tool_policies: List[ToolPolicy] = Field(default_factory=list)
    knowledge_bindings: List[KnowledgeBinding] = Field(default_factory=list)
    memory_profile: MemoryProfile = Field(default_factory=MemoryProfile)
    eval_profile: EvalProfile = Field(default_factory=EvalProfile)
    publish_profile: PublishProfile = Field(default_factory=PublishProfile)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Story 13.2: 执行方式（缺省时按 ReAct 模式运行，向后兼容）
    execution_mode: Optional[ExecutionMode] = None

    @model_validator(mode="after")
    def validate_blueprint(self) -> "AgentBlueprint":
        if self.mode == "single" and len(self.role_profiles) < 1:
            raise ValueError("mode='single' requires at least one role_profile")

        # citation_required 的 knowledge binding 必须有 source_type != 'unspecified'
        for kb in self.knowledge_bindings:
            if kb.citation_required and kb.source_type == "unspecified":
                raise ValueError(
                    f"knowledge_binding '{kb.binding_id}' has citation_required=True "
                    "but source_type is 'unspecified'"
                )

        return self


# ---------------------------------------------------------------------------
# Permission rule evaluation (AC4)
# ---------------------------------------------------------------------------


def _matches_pattern(pattern: str, value: str) -> bool:
    """Minimal glob match: * is wildcard, everything else literal."""
    import fnmatch
    return fnmatch.fnmatch(value, pattern)


def evaluate_permission(
    policy: "ToolPolicy",
    args: Dict[str, Any],
) -> Literal["allow", "ask", "deny"]:
    """Evaluate deny > ask > allow rules against call args. Returns first match.

    arg_pattern format: "key:glob"  e.g. "query:*小红书*"
    Empty arg_pattern matches any call unconditionally.
    """
    def _rule_matches(rule: "PermissionRule") -> bool:
        pat = rule.arg_pattern.strip()
        if not pat:
            return True
        if ":" not in pat:
            return True
        key, glob = pat.split(":", 1)
        value = str(args.get(key, ""))
        return _matches_pattern(glob, value)

    for perm_level in ("deny", "ask", "allow"):
        for rule in policy.permission_rules:
            if rule.permission == perm_level and _rule_matches(rule):
                return perm_level  # type: ignore[return-value]

    return policy.default_permission
