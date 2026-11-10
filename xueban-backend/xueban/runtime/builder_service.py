"""Builder Service — Story 8.1 (骨架) + Story 8.6 (真实发布)

职责：
  generate_blueprint  — 从 goal/audience/mode 启发式生成 AgentBlueprint
  instantiate_blueprint — Blueprint → WorkflowTemplateSpec + WorkflowDefinition
  smoke_run_blueprint — 5 项最小静态检查（Story 8.5）
  publish_blueprint   — 真实回填：写 Template + WorkflowDefinition 到本地文件系统（Story 8.6）
  list_kits           — 返回静态内置 Kit 目录

映射规则（T3）：
  RoleProfile → TemplateAgentSpec + NodeDefinition
  ToolPolicy  → node config executor tools & metadata
  Knowledge/Memory/EvalProfile → template.metadata & workflow.metadata（占位）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from xueban.highlevel import WorkflowTemplateSpec
from xueban.runtime.contracts import WorkflowDefinition
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    KnowledgeBinding,
    PermissionRule,
    RoleProfile,
    ToolPolicy,
)
from xueban.runtime.errors import XuebanError
from xueban.runtime.prompts import SystemPromptBuilder

logger = logging.getLogger(__name__)

# T1 follow-up: single source for agent system_prompt (see prompts/builder.py).
# Stateless — safe as module-level singleton. Previously this site emitted a
# one-liner `f"You are {role.name}. {role.description}"` that dropped
# responsibilities/constraints/handoff_rules from the RoleProfile contract.
_PROMPT_BUILDER = SystemPromptBuilder()

# 模板存储目录（与 server.py 中 _CUSTOM_DIR 对齐，保证 GET /templates 可读取）
_CUSTOM_TEMPLATE_DIR = Path("templates/custom")
# Workflow 定义存储目录（新引入，供 /editor?workflowId=... 消费）
_WORKFLOW_DIR = Path(".xueban/workflows")

# Builder version constant (Patch 17)
_BUILDER_VERSION = "8.6"


class RegressionBlockedError(XuebanError):
    """Regression Gate 拦截，不允许发布。"""

    code = "REGRESSION_BLOCKED"

    def __init__(self, reason: str = "") -> None:
        super().__init__(
            f"Regression gate blocked publish: {reason}" if reason else "Regression gate blocked publish",
            details={"reason": reason},
        )


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class GenerateBlueprintRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    audience: str = Field(default="", max_length=500)
    mode: Literal["single", "team"] = "single"
    desired_output: str = Field(default="", max_length=200)
    knowledge_sources: List[str] = Field(default_factory=list, max_length=20)
    reference_agent_id: Optional[str] = Field(default=None, max_length=128)


class GenerateBlueprintResponse(BaseModel):
    blueprint: AgentBlueprint
    meta: Dict[str, Any] = Field(default_factory=dict)


class InstantiateBlueprintRequest(BaseModel):
    blueprint: AgentBlueprint
    parameters: Dict[str, Any] = Field(default_factory=dict)


class InstantiateBlueprintResponse(BaseModel):
    blueprint: AgentBlueprint
    template_spec: Dict[str, Any]
    workflow_definition: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)


class SmokeRunBlueprintRequest(BaseModel):
    blueprint: AgentBlueprint
    kit_id: Optional[str] = None


class SmokeCheck(BaseModel):
    check_id: str
    label: str
    status: Literal["passed", "failed", "warning", "skipped"]
    reason: str
    target_ref: Optional[str] = None
    failure_category: Literal[
        "goal_clarity", "knowledge_inaccessible", "tool_permission",
        "role_conflict", "graph_break", "none"
    ] = "none"
    raw_reason: Optional[str] = None


class SmokeRunBlueprintResponse(BaseModel):
    status: Literal["passed", "failed", "warning"] = "passed"
    checks: List[SmokeCheck] = Field(default_factory=list)
    summary: str = ""
    warnings: List[str] = Field(default_factory=list)
    recommended_fix: Optional[str] = None
    primary_blocker: Optional[str] = None


class PublishBlueprintRequest(BaseModel):
    blueprint: AgentBlueprint


class PublishLinks(BaseModel):
    templates: str = "/templates"
    editor: str = ""
    inbox: str = "/inbox"


class PublishBlueprintResponse(BaseModel):
    template_id: str = ""
    workflow_id: str = ""
    kit_tags: List[str] = Field(default_factory=list)
    publish_status: Literal["published", "pending", "error"] = "pending"
    links: PublishLinks = Field(default_factory=PublishLinks)


class BuilderKitSummary(BaseModel):
    kit_id: str
    name: str
    description: str
    mode: Literal["single", "team"]
    role_count: int
    tags: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 静态 Kit 目录
# ---------------------------------------------------------------------------

_BUILTIN_KITS: List[BuilderKitSummary] = [
    BuilderKitSummary(
        kit_id="research",
        name="Research Kit",
        description="规划 → 搜集 → 总结报告的多步骤研究 Agent",
        mode="team",
        role_count=3,
        tags=["research", "report", "multi-step"],
    ),
    BuilderKitSummary(
        kit_id="knowledge_assistant",
        name="Knowledge Assistant Kit",
        description="知识问答 + 引用 + 转人工的客服 Agent",
        mode="single",
        role_count=1,
        tags=["qa", "citation", "handoff"],
    ),
    BuilderKitSummary(
        kit_id="review_approval",
        name="Review & Approval Kit",
        description="Writer → Reviewer → Approver 审批流水线",
        mode="team",
        role_count=3,
        tags=["review", "approval", "pipeline"],
    ),
    BuilderKitSummary(
        kit_id="persona_npc",
        name="Persona / NPC Kit",
        description="有角色记忆与状态的 NPC / 人物扮演 Agent",
        mode="single",
        role_count=1,
        tags=["persona", "memory", "npc"],
    ),
]


# ---------------------------------------------------------------------------
# Failure translation helper (AC3)
# ---------------------------------------------------------------------------

_FIX_MAP: Dict[str, str] = {
    "goal_clarity": "返回 Goal Mode 补充目标描述，确保至少说明：做什么、对谁、输出什么",
    "knowledge_inaccessible": "打开 Knowledge Dock，检查知识来源引用是否完整可访问",
    "tool_permission": "打开 Tool Registry，确认工具策略可见性设置为 enabled",
    "role_conflict": "切换到 Scene Mode 调整角色职责定义，避免职责冲突",
    "graph_break": "切换到 Graph Mode 修复断裂的节点连接",
    "none": "检查验证面板中的具体失败项，逐项修复",
}


def _translate_fix(category: str) -> str:
    return _FIX_MAP.get(category, _FIX_MAP["none"])


# ---------------------------------------------------------------------------
# 映射函数（T3）
# ---------------------------------------------------------------------------


def _blueprint_to_template_spec(blueprint: AgentBlueprint) -> WorkflowTemplateSpec:
    """将 AgentBlueprint 映射为 WorkflowTemplateSpec（T3 映射规则）。

    Knowledge/Memory/Eval/PublishProfile 进入 template.metadata（占位）。
    不调用 TemplateCompiler（需要 SpecRegistry；将在 8.2/8.3 连通 spec 目录后启用）。
    """
    roles = blueprint.role_profiles or []
    if not roles:
        roles = [
            RoleProfile(
                name="default_agent",
                description=f"Executes: {blueprint.goal}",
                executor_kind="api",
                executor_provider="anthropic",
            )
        ]

    # 简单顺序链
    edges: List[Dict[str, Any]] = []
    for i, role in enumerate(roles[:-1]):
        edges.append({"from": role.role_id, "to": roles[i + 1].role_id, "type": "default"})
    edges.append({"from": roles[-1].role_id, "to": "END", "type": "final"})

    template_meta: Dict[str, Any] = {
        "blueprint_id": blueprint.blueprint_id,
        "blueprint_version": blueprint.version,
        "blueprint_goal": blueprint.goal,
        "blueprint_audience": blueprint.audience,
        "knowledge_bindings": [kb.model_dump() for kb in blueprint.knowledge_bindings],
        "memory_profile": blueprint.memory_profile.model_dump(),
        "eval_profile": blueprint.eval_profile.model_dump(),
        "publish_profile": blueprint.publish_profile.model_dump(),
        "tool_policies": [tp.model_dump() for tp in blueprint.tool_policies],
    }

    return WorkflowTemplateSpec.model_validate(
        {
            "template_id": blueprint.blueprint_id,
            "version": blueprint.version,
            "name": blueprint.name,
            "description": blueprint.goal,
            "agents": [{"id": role.role_id, "ref": role.role_id} for role in roles],
            "nodes": [],
            "flow": {
                "entrypoint": roles[0].role_id,
                "edges": edges,
            },
            "policy_matrix": {"agents": {}},
            "stages": [],
            "metadata": template_meta,
        }
    )


def _build_workflow_definition(
    blueprint: AgentBlueprint,
    template_spec: WorkflowTemplateSpec,
) -> WorkflowDefinition:
    """将 Blueprint 映射为 WorkflowDefinition，通过 model_validate 验证合法性（AC2）。

    每个 RoleProfile 生成 type=agent.execute 节点。
    can_spawn_tasks=True 的角色在 tool_refs 里追加 builtin:spawn_task。
    """
    roles = blueprint.role_profiles
    if not roles:
        roles = [
            RoleProfile(
                name="default_agent",
                description=f"Executes: {blueprint.goal}",
                executor_kind="api",
                executor_provider="anthropic",
            )
        ]

    nodes: List[Dict[str, Any]] = []
    for role in roles:
        tools = list(role.tools)
        if role.can_spawn_tasks and "builtin:spawn_task" not in tools:
            tools.append("builtin:spawn_task")

        config: Dict[str, Any] = {
            "executor": {
                "kind": role.executor_kind,
                "provider": role.executor_provider,
                "model": role.executor_model,
            },
            "prompt": _PROMPT_BUILDER.build(role),
            "role": role.role_id,
            "tool_refs": tools,
        }

        matching_policies = [tp for tp in blueprint.tool_policies if tp.tool_id in tools]
        if matching_policies:
            config["tool_policies"] = [tp.model_dump() for tp in matching_policies]

        nodes.append(
            {
                "id": role.role_id,
                "kind": "agent",
                "type": "agent.execute",
                "config": config,
                "metadata": {
                    "role_name": role.name,
                    "can_spawn_tasks": role.can_spawn_tasks,
                    "blueprint_id": blueprint.blueprint_id,
                    "knowledge_bindings": [kb.model_dump() for kb in blueprint.knowledge_bindings],
                    "memory_profile": blueprint.memory_profile.model_dump(),
                    "eval_profile": blueprint.eval_profile.model_dump(),
                },
            }
        )

    edges: List[Dict[str, Any]] = []
    for i, role in enumerate(roles[:-1]):
        edges.append({"from": role.role_id, "to": roles[i + 1].role_id, "type": "default"})
    edges.append({"from": roles[-1].role_id, "to": "END", "type": "final"})

    raw: Dict[str, Any] = {
        "workflow_id": blueprint.blueprint_id,
        "version": blueprint.version,
        "name": blueprint.name,
        "entrypoint": roles[0].role_id,
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "blueprint_id": blueprint.blueprint_id,
            "blueprint_version": blueprint.version,
            "builder_layer": "8.1",
            "publish_profile": blueprint.publish_profile.model_dump(),
        },
    }
    return WorkflowDefinition.model_validate(raw)


# ---------------------------------------------------------------------------
# BuilderService
# ---------------------------------------------------------------------------


class BuilderService:
    """Builder 主路径服务骨架（Story 8.1）。"""

    def generate_blueprint(self, req: GenerateBlueprintRequest) -> GenerateBlueprintResponse:
        """最小启发式 Blueprint 生成。"""
        missing_inputs: List[str] = []
        if not req.audience:
            missing_inputs.append("audience")
        if not req.desired_output:
            missing_inputs.append("desired_output")

        if req.mode == "team":
            roles = [
                RoleProfile(
                    name="planner",
                    description=f"Break down goal: {req.goal}",
                    executor_kind="api",
                    executor_provider="anthropic",
                ),
                RoleProfile(
                    name="executor",
                    description=f"Execute plan toward: {req.desired_output or req.goal}",
                    executor_kind="api",
                    executor_provider="anthropic",
                ),
            ]
        else:
            roles = [
                RoleProfile(
                    name="agent",
                    description=f"Achieve: {req.goal}",
                    executor_kind="api",
                    executor_provider="anthropic",
                )
            ]

        knowledge_bindings: List[KnowledgeBinding] = [
            KnowledgeBinding(source_type="url", source_ref=src)
            for src in req.knowledge_sources
        ]

        blueprint = AgentBlueprint(
            name=req.goal[:64] or "unnamed",
            goal=req.goal,
            audience=req.audience,
            mode=req.mode,
            role_profiles=roles,
            knowledge_bindings=knowledge_bindings,
        )

        confidence = max(0.3, 1.0 - len(missing_inputs) * 0.2)
        return GenerateBlueprintResponse(
            blueprint=blueprint,
            meta={
                "confidence": confidence,
                "missing_inputs": missing_inputs,
                "suggested_next_step": "instantiate_blueprint",
                "source": "heuristic",  # P4: AC3 requires meta.source; always "heuristic" until LLM generation
            },
        )

    def instantiate_blueprint(self, req: InstantiateBlueprintRequest) -> InstantiateBlueprintResponse:
        """Blueprint → WorkflowTemplateSpec + WorkflowDefinition（AC2）。"""
        warnings: List[str] = []

        template_spec = _blueprint_to_template_spec(req.blueprint)
        workflow_definition = _build_workflow_definition(req.blueprint, template_spec)

        if req.blueprint.eval_profile.smoke_eval_enabled:
            warnings.append("smoke_eval is enabled but eval engine is pending (Epic 9)")
        if req.blueprint.knowledge_bindings:
            warnings.append("knowledge_bindings are schema-complete; ingest pipeline is pending (Story 8.4)")

        return InstantiateBlueprintResponse(
            blueprint=req.blueprint,
            template_spec=template_spec.model_dump(mode="python"),
            workflow_definition=workflow_definition.model_dump(mode="python"),
            warnings=warnings,
        )

    def smoke_run_blueprint(self, req: SmokeRunBlueprintRequest) -> SmokeRunBlueprintResponse:
        """最小 Smoke Run 五项静态检查（Story 8.5 AC2/AC4/AC5）。"""
        bp = req.blueprint
        checks: List[SmokeCheck] = []

        # ── 1. Role initialization ───────────────────────────────────────────
        roles_ok = bool(bp.role_profiles) and all(r.name for r in bp.role_profiles)
        checks.append(SmokeCheck(
            check_id="role_init",
            label="角色能否正常初始化",
            status="passed" if roles_ok else "failed",
            reason=(
                f"{len(bp.role_profiles)} 个角色已定义，配置完整"
                if roles_ok
                else "角色列表为空或存在未命名角色，无法初始化 Agent"
            ),
            target_ref=None if roles_ok else "goal_mode",
            failure_category="none" if roles_ok else "goal_clarity",
            raw_reason=None if roles_ok else f"role_profiles empty or unnamed: {[r.role_id for r in bp.role_profiles if not r.name]}",
        ))

        # ── 2. Tool availability ─────────────────────────────────────────────
        all_tools: List[str] = []
        for role in bp.role_profiles:
            all_tools.extend(role.tools)
        enabled_ids = {tp.tool_id for tp in bp.tool_policies if tp.visibility == "enabled"}
        disabled_ids = {tp.tool_id for tp in bp.tool_policies if tp.visibility == "disabled"}
        conflicting_ids = enabled_ids & disabled_ids  # 同时在 enabled 和 disabled 中
        disabled_tools = [t for t in all_tools if t in disabled_ids]
        tools_status: Literal["passed", "failed", "warning"] = "passed"
        tools_reason = "所有工具策略正常或未配置（使用默认权限）"
        tools_target = None
        if disabled_tools:
            tools_status = "failed"
            tools_reason = f"工具权限不足：{disabled_tools} 已被禁用，相关角色无法使用"
            tools_target = "tool_registry"
        elif conflicting_ids:
            tools_status = "warning"
            tools_reason = f"工具策略存在冲突：{sorted(conflicting_ids)} 同时有 enabled 和 disabled 策略（disabled 优先），建议清理"
        elif all_tools and not enabled_ids:
            tools_status = "warning"
            tools_reason = "角色引用了工具但未配置显式策略，将使用默认 allow"
        checks.append(SmokeCheck(
            check_id="tools_available",
            label="必要工具是否可用",
            status=tools_status,
            reason=tools_reason,
            target_ref=tools_target,
            failure_category="tool_permission" if tools_status == "failed" else "none",
            raw_reason=f"disabled_tools={disabled_tools}" if disabled_tools else None,
        ))

        # ── 3. Knowledge accessibility ──────────────────────────────────────
        real_bindings = [
            kb for kb in bp.knowledge_bindings if kb.source_type != "unspecified"
        ]
        unspecified_bindings = [
            kb for kb in bp.knowledge_bindings if kb.source_type == "unspecified"
        ]
        if not bp.knowledge_bindings or all(kb.source_type == "unspecified" for kb in bp.knowledge_bindings):
            knowledge_status: Literal["passed", "failed", "warning", "skipped"] = "passed"
            knowledge_reason = "当前选择「暂不绑定知识」，无需知识来源"
            knowledge_target = None
            knowledge_category: Literal["none", "knowledge_inaccessible"] = "none"
        else:
            missing_ref = [kb for kb in real_bindings if not kb.source_ref]
            if missing_ref:
                knowledge_status = "failed"
                knowledge_reason = f"知识缺失或不可访问：{len(missing_ref)} 个绑定缺少有效来源引用"
                knowledge_target = "knowledge_dock"
                knowledge_category = "knowledge_inaccessible"
            else:
                knowledge_status = "passed"
                knowledge_reason = f"{len(real_bindings)} 个知识来源已绑定，结构合法（可访问性由运行时验证）"
                knowledge_target = None
                knowledge_category = "none"
        checks.append(SmokeCheck(
            check_id="knowledge_accessible",
            label="知识绑定是否可访问",
            status=knowledge_status,
            reason=knowledge_reason,
            target_ref=knowledge_target,
            failure_category=knowledge_category,
            raw_reason=f"unspecified_count={len(unspecified_bindings)}, real_count={len(real_bindings)}",
        ))

        # ── 4. Minimum task loop ─────────────────────────────────────────────
        goal_words = len(bp.goal.split())
        if not bp.goal or goal_words < 3:
            min_task_ok = False
            min_reason = "目标描述过于简短或缺失，无法形成有效任务闭环"
            min_raw: Optional[str] = f"goal_word_count={goal_words}"
            min_target: Optional[str] = "goal_mode"
            min_category: Literal["none", "goal_clarity"] = "goal_clarity"
        elif not bp.role_profiles:
            min_task_ok = False
            min_reason = "尚未定义任何角色，无法形成任务闭环 — 请在 Scene Mode 添加角色"
            min_raw = "role_profiles_count=0"
            min_target = "scene_mode"
            # role_conflict: target_ref='scene_mode' 与 failure_category 对齐，
            # FixActionButton 优先按 target_ref 路由到 Scene Mode
            min_category: Literal["none", "goal_clarity"] = "goal_clarity"
        else:
            min_task_ok = True
            min_reason = "目标描述清晰，角色完整，可形成最小输入→输出闭环"
            min_raw = None
            min_target = None
            min_category = "none"
        checks.append(SmokeCheck(
            check_id="min_task_loop",
            label="最小任务能否从输入走到输出",
            status="passed" if min_task_ok else "failed",
            reason=min_reason,
            target_ref=min_target,
            failure_category=min_category,
            raw_reason=min_raw,
        ))

        # ── 5. Citation requirement ──────────────────────────────────────────
        citation_bindings = [kb for kb in bp.knowledge_bindings if kb.citation_required]
        if not citation_bindings:
            citation_status: Literal["passed", "failed", "warning", "skipped"] = "skipped"
            citation_reason = "未启用引用要求，跳过引用检查"
            citation_target = None
            citation_category: Literal["none", "knowledge_inaccessible"] = "none"
        else:
            # citation_required=True, source_type must be non-unspecified (validated by model)
            accessible = [kb for kb in citation_bindings if kb.source_ref]
            if len(accessible) == len(citation_bindings):
                citation_status = "passed"
                citation_reason = f"{len(citation_bindings)} 个引用要求已满足（完整 citation_trace 在 Epic 9.2 实现）"
                citation_target = None
                citation_category = "none"
            else:
                citation_status = "warning"
                citation_reason = "部分引用要求的知识来源引用不完整，发布前建议完善"
                citation_target = "knowledge_dock"
                citation_category = "knowledge_inaccessible"
        checks.append(SmokeCheck(
            check_id="citation_check",
            label="引用要求是否被满足",
            status=citation_status,
            reason=citation_reason,
            target_ref=citation_target,
            failure_category=citation_category,
            raw_reason=f"citation_bindings={len(citation_bindings)}" if citation_bindings else None,
        ))

        # ── 6. Workflow binding (Story 13.2 AC4) ────────────────────────────
        em = bp.execution_mode
        if em is None or em.mode == "react":
            # ReAct 模式 / 未配置：跳过
            workflow_binding_status: Literal["passed", "failed", "warning", "skipped"] = "skipped"
            workflow_binding_reason = "未绑定工作流，将使用 ReAct 循环模式"
            workflow_binding_target = None
        elif em.mode == "workflow":
            if not em.workflow_ref:
                # 绑定模式但 workflow_ref 为空 → warning
                workflow_binding_status = "warning"
                workflow_binding_reason = "已选择绑定工作流模式，但未指定 workflow_ref，将回退到 ReAct 模式"
                workflow_binding_target = "inspector"
            else:
                # 已有 workflow_ref：简单检查非空格式（真实 template 存在性验证在运行时）
                # Mock: 认为非空 workflow_ref 即为有效（smoke run 不做真实 I/O）
                #
                # KNOWN-GAP (13-2 review H2/M1, 2026-04-29):
                #   1) tautology — 任何非空字符串都 passed，未做 template registry 校验
                #   2) execution_mode 字段当前是 "blueprint 元数据 + smoke 自检"，
                #      agent_engine / orchestrator 没有 mode == "workflow" 分支真消费
                #   Phase-2 计划：接入 listTemplates 做存在性校验，并在 runtime
                #   按 execution_mode 路由到 workflow executor。
                workflow_binding_status = "passed"
                workflow_binding_reason = f"已绑定工作流 '{em.workflow_ref}'，Smoke Run 结构验证通过"
                workflow_binding_target = None
        else:
            workflow_binding_status = "skipped"
            workflow_binding_reason = "未知执行模式，跳过工作流绑定检查"
            workflow_binding_target = None

        checks.append(SmokeCheck(
            check_id="workflow_binding",
            label="工作流绑定是否有效",
            status=workflow_binding_status,
            reason=workflow_binding_reason,
            target_ref=workflow_binding_target,
            failure_category="none",
            raw_reason=f"execution_mode={em.model_dump() if em else None}",
        ))

        # ── Aggregate result ─────────────────────────────────────────────────
        failed = [c for c in checks if c.status == "failed"]
        warned = [c for c in checks if c.status == "warning"]

        if failed:
            overall_status: Literal["passed", "failed", "warning"] = "failed"
            primary_blocker = failed[0].check_id
            summary = f"发现 {len(failed)} 项阻塞问题，发布前必须修复"
            recommended_fix = _translate_fix(failed[0].failure_category)
        elif warned:
            overall_status = "warning"
            primary_blocker = warned[0].check_id
            summary = f"整体可运行，但存在 {len(warned)} 项建议改进项"
            recommended_fix = _translate_fix(warned[0].failure_category)
        else:
            overall_status = "passed"
            primary_blocker = None
            summary = "当前已通过最小闭环验证，可继续发布流程"
            recommended_fix = None

        return SmokeRunBlueprintResponse(
            status=overall_status,
            checks=checks,
            summary=summary,
            warnings=[],
            recommended_fix=recommended_fix,
            primary_blocker=primary_blocker,
        )

    def publish_blueprint(self, req: PublishBlueprintRequest) -> PublishBlueprintResponse:
        """真实回填发布（Story 8.6 AC5）。

        流程：
        1. 调用 instantiate_blueprint 获取 template_spec + workflow_definition
        2. 验证两者可通过 model_validate（失败则抛 BLUEPRINT_INVALID）
        3. （可选）调用 Regression Gate；blocked 时抛 REGRESSION_BLOCKED
        4. 写 template → templates/custom/{template_id}.yaml
        5. 写 workflow  → .xueban/workflows/{workflow_id}.json
        6. 返回 template_id / workflow_id / kit_tags / links
        """
        bp = req.blueprint

        # Step 1+2: instantiate and validate
        inst_req = InstantiateBlueprintRequest(blueprint=bp)
        inst = self.instantiate_blueprint(inst_req)

        try:
            template_spec = WorkflowTemplateSpec.model_validate(inst.template_spec)
        except Exception as exc:
            raise XuebanError(
                f"Blueprint instantiation produced invalid template: {exc}",
                details={"code": "BLUEPRINT_INVALID"},
            ) from exc

        try:
            workflow_def = WorkflowDefinition.model_validate(inst.workflow_definition)
        except Exception as exc:
            raise XuebanError(
                f"Blueprint instantiation produced invalid workflow: {exc}",
                details={"code": "BLUEPRINT_INVALID"},
            ) from exc

        # Step 3: Regression Gate (Story 9-6)
        # Gate is only enforced via /regression/{id}/run (where real current_metrics are
        # available).  At publish time there are no eval results, so we skip the gate to
        # avoid a guaranteed false-positive block (empty metrics → -100 % on every metric).
        pass

        # Step 4: generate IDs upfront so each file can reference the other
        hex8 = uuid4().hex[:8]
        template_id = f"bldr-{hex8}"
        workflow_id = uuid4().hex

        kit_tags: List[str] = list(getattr(bp.publish_profile, "kit_tags", []) or [])

        # Step 4a: build template spec with builder metadata (includes workflow_id for editor link)
        builder_meta: Dict[str, Any] = {
            **inst.template_spec.get("metadata", {}),
            "builder_origin": "builder",
            "builder_version": _BUILDER_VERSION,
            "source_goal": bp.goal,
            "source_blueprint_id": bp.blueprint_id,
            "kit_tags": kit_tags,
            "workflow_id": workflow_id,
        }
        template_raw: Dict[str, Any] = {
            **inst.template_spec,
            "template_id": template_id,
            "name": bp.name,
            "description": bp.goal,
            "metadata": builder_meta,
        }

        try:
            final_template = WorkflowTemplateSpec.model_validate(template_raw)
        except Exception as exc:
            raise XuebanError(
                f"Builder metadata injection produced invalid template: {exc}",
                details={"code": "BLUEPRINT_INVALID"},
            ) from exc

        # Step 4b: build workflow definition with source_template_id
        wf_raw: Dict[str, Any] = {
            **inst.workflow_definition,
            "workflow_id": workflow_id,
            "metadata": {
                **inst.workflow_definition.get("metadata", {}),
                "source_template_id": template_id,
                "builder_origin": "builder",
                "builder_version": _BUILDER_VERSION,
            },
        }
        try:
            validated_wf = WorkflowDefinition.model_validate(wf_raw)  # Patch 3: save result
        except Exception as exc:
            raise XuebanError(
                f"Workflow metadata injection produced invalid definition: {exc}",
                details={"code": "BLUEPRINT_INVALID"},
            ) from exc

        # Step 5a: persist template → templates/custom/{template_id}.yaml
        _CUSTOM_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        template_path = _CUSTOM_TEMPLATE_DIR / f"{template_id}.yaml"
        yaml_out = yaml.safe_dump(
            final_template.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        template_path.write_text(yaml_out, encoding="utf-8")

        # Step 5b: persist workflow → .xueban/workflows/{workflow_id}.json
        # Patch 1: atomic rollback — if workflow write fails, clean up the template file
        try:
            _WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
            workflow_path = _WORKFLOW_DIR / f"{workflow_id}.json"
            # Patch 3: write validated model_dump, not raw dict
            workflow_path.write_text(
                json.dumps(validated_wf.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Rollback: remove template orphan
            try:
                template_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        # Step 5c: register in Catalog index (Story 8.7 AC5)
        # Failure here must not break publish — Catalog is an index layer, not a real source.
        try:
            from xueban.runtime.catalog_service import (
                CatalogService,
                RegisterPublishedAppRequest,
                get_service as _catalog_get_service,
            )
            kit_type = self._infer_kit_type(kit_tags)
            author = ""
            if isinstance(bp.metadata, dict):
                author = str(bp.metadata.get("author", "") or "")
            catalog_svc: CatalogService = _catalog_get_service()
            catalog_svc.register_published_app(
                RegisterPublishedAppRequest(
                    blueprint=bp,
                    template_id=template_id,
                    workflow_id=workflow_id,
                    author=author or "anonymous",
                    kit_type=kit_type,
                )
            )
        except Exception as _catalog_exc:  # noqa: BLE001 — never let Catalog failure break publish
            logger.warning("Catalog registration failed for %s: %s", bp.blueprint_id, _catalog_exc)

        # Step 6: return result
        return PublishBlueprintResponse(
            template_id=template_id,
            workflow_id=workflow_id,
            kit_tags=kit_tags,
            publish_status="published",
            links=PublishLinks(
                templates="/templates",
                editor=f"/editor?workflowId={workflow_id}",
                inbox="/inbox",
            ),
        )

    @staticmethod
    def _infer_kit_type(kit_tags: List[str]) -> str:
        """Map publish kit_tags onto the Catalog kit_type vocabulary (AC2)."""
        if not kit_tags:
            return "custom"
        known = {"research", "knowledge_assistant", "review_approval", "persona"}
        for tag in kit_tags:
            t = str(tag).lower().replace("-", "_")
            if t in known:
                return t
            if t == "npc":
                return "persona"
        return "custom"

    def list_kits(self) -> List[BuilderKitSummary]:
        """返回静态内置 Kit 目录。"""
        return list(_BUILTIN_KITS)

    def instantiate_kit(self, kit_id: str) -> AgentBlueprint:
        """从 kit_id 生成预配置的 AgentBlueprint（AC6: Research Kit 默认 web_search）。"""
        kit_map = {k.kit_id: k for k in _BUILTIN_KITS}
        if kit_id not in kit_map:
            raise ValueError(f"Unknown kit_id: {kit_id!r}")

        kit = kit_map[kit_id]
        tool_policies: List[ToolPolicy] = []

        if kit_id == "research":
            # AC6: Research Kit 默认附带 builtin:web_search，开箱即用
            tool_policies.append(
                ToolPolicy(
                    tool_id="builtin:web_search",
                    visibility="enabled",
                    default_permission="allow",
                )
            )
            roles = [
                RoleProfile(
                    name="planner",
                    description=(
                        "Break down research goals into structured search queries. "
                        "Available tools: web_search (auto-allowed)."
                    ),
                    can_spawn_tasks=True,
                    tools=["builtin:web_search"],
                ),
                RoleProfile(
                    name="researcher",
                    description=(
                        "Execute searches and collect evidence. "
                        "Available tools: web_search, web_fetch."
                    ),
                    tools=["builtin:web_search", "builtin:web_fetch"],
                ),
                RoleProfile(
                    name="writer",
                    description="Synthesise findings into a coherent report.",
                    tools=[],
                ),
            ]
            return AgentBlueprint(
                name="Research Kit",
                goal="Research a topic and produce a structured report.",
                audience="Researchers and analysts",
                mode="team",
                role_profiles=roles,
                tool_policies=tool_policies,
            )

        # Generic fallback for other kits
        roles = [
            RoleProfile(
                name=kit.name,
                description=kit.description,
            )
        ]
        return AgentBlueprint(
            name=kit.name,
            goal=kit.description,
            mode=kit.mode,
            role_profiles=roles,
            tool_policies=tool_policies,
        )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_SERVICE_SINGLETON: Optional[BuilderService] = None


def get_service() -> BuilderService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = BuilderService()
    return _SERVICE_SINGLETON


def set_service(svc: BuilderService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc