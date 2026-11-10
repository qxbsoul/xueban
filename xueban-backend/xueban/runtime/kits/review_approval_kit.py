"""Review & Approval Kit — Story 10.3

Writer / Reviewer / Approver 生成-审核-审批闭环 Kit。

设计原则：
  - Kit = 预配置的 AgentBlueprint 工厂 + KitDefinition 注册条目
  - approval_levels="single_review"  → 2 角色（Writer + Reviewer）
  - approval_levels="review_then_approve" → 3 角色（Writer + Reviewer + Approver）
  - PolicyMatrix：Writer 禁止直接 Deliver（allow_send 不含 Writer→END）
  - Reviewer / Approver 可以 reject 回 Writer（allow_reject）
  - max_reject_rounds → RetryPolicy.max_rounds
  - Blueprint metadata 注入 ApprovalGate 节点配置（由运行时读取）

KitDefinition 使用 Story 10.5 的正式 registry.KitDefinition，
discover_and_register_kits() 会自动导入本模块并触发 REGISTRY.register()。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    MemoryProfile,
    RoleProfile,
)
from xueban.runtime.kits.registry import (
    KitDefinition,
    PolicyProfile,
    REGISTRY,
    SceneDefinition,
    SceneRoleNode,
)


# ---------------------------------------------------------------------------
# 向导输入 Pydantic 模型
# ---------------------------------------------------------------------------


class ReviewApprovalGoalInputs(BaseModel):
    """Review & Approval Kit Goal Mode 向导的 6 个输入字段。

    Pydantic v2，field_validator 进行枚举和范围校验。
    """

    content_type: str = Field(
        default="document",
        description="内容类型：document / code / proposal / custom",
    )
    approval_levels: str = Field(
        default="single_review",
        description="审批层级：single_review / review_then_approve（multi_level Phase 2）",
    )
    max_reject_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description="最大驳回轮次（1–10，默认 3）",
    )
    output_format: str = Field(
        default="markdown",
        description="输出格式：markdown / json / plain_text",
    )
    reviewer_name: str = Field(
        default="Reviewer",
        max_length=64,
        description="审核角色姓名（可选）",
    )
    approver_name: str = Field(
        default="Approver",
        max_length=64,
        description="审批角色姓名（可选）",
    )

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        allowed = {"document", "code", "proposal", "custom"}
        if v not in allowed:
            raise ValueError(f"content_type 必须是 {allowed} 之一，当前值：{v!r}")
        return v

    @field_validator("approval_levels")
    @classmethod
    def validate_approval_levels(cls, v: str) -> str:
        allowed = {"single_review", "review_then_approve"}
        if v not in allowed:
            raise ValueError(
                f"approval_levels MVP 仅支持 {allowed}，multi_level 为 Phase 2。"
                f"当前值：{v!r}"
            )
        return v

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, v: str) -> str:
        allowed = {"markdown", "json", "plain_text"}
        if v not in allowed:
            raise ValueError(f"output_format 必须是 {allowed} 之一，当前值：{v!r}")
        return v


# ---------------------------------------------------------------------------
# Blueprint 工厂函数
# ---------------------------------------------------------------------------


def create_review_approval_blueprint(
    goal_inputs: ReviewApprovalGoalInputs,
) -> AgentBlueprint:
    """从向导输入生成 Review & Approval Kit 默认 AgentBlueprint。

    approval_levels="single_review"      → 2 角色：Writer + Reviewer
    approval_levels="review_then_approve" → 3 角色：Writer + Reviewer + Approver

    PolicyMatrix 规则（WorkflowPolicyMatrixSpec 字段映射）：
      - allow_send：Writer 只能发给 Reviewer（不可直接 deliver）
                    Reviewer 可以发给 Approver 或 END
                    Approver 可以发给 END
      - allow_reject：Reviewer 可以 reject 回 Writer
                      Approver 可以 reject 回 Writer

    max_reject_rounds → 注入 RetryPolicy.max_rounds（metadata 层传递，
    由运行时 service.py 消费）。

    Blueprint metadata 中保存 approval_gate_config，供运行时注入节点。
    """
    content_type = goal_inputs.content_type
    approval_levels = goal_inputs.approval_levels
    max_reject_rounds = goal_inputs.max_reject_rounds
    output_format = goal_inputs.output_format
    reviewer_name = goal_inputs.reviewer_name
    approver_name = goal_inputs.approver_name

    use_approver = (approval_levels == "review_then_approve")

    # ── 角色 1：Writer（生成初稿）──────────────────────────────────────────
    writer_role_id = "writer"
    writer_constraints = [
        f"输出格式必须是 {output_format}",
        "不得绕过审核直接交付最终产物",
        "每轮重写必须根据 Reviewer 的反馈意见修改",
    ]
    writer = RoleProfile(
        role_id=writer_role_id,
        name="Writer",
        description=(
            f"负责生成{_content_type_desc(content_type)}初稿，"
            f"输出格式：{output_format}。"
            "收到驳回意见后根据反馈修改并重新提交。"
        ),
        persona="高效的内容创作者，善于根据反馈迭代改进",
        responsibilities=[
            f"根据需求生成高质量{_content_type_desc(content_type)}",
            f"以 {output_format} 格式输出内容",
            "接受审核意见后完成修改，进入下一轮审核",
        ],
        constraints=writer_constraints,
        tools=[],
        metadata={
            "kit_id": "review_approval_kit",
            "role_type": "writer",
            "output_format": output_format,
            "max_reject_rounds": max_reject_rounds,
            "retry_policy": {"max_rounds": max_reject_rounds},
        },
    )

    # ── 角色 2：Reviewer（复核）───────────────────────────────────────────
    reviewer_role_id = "reviewer"
    reviewer = RoleProfile(
        role_id=reviewer_role_id,
        name=reviewer_name,
        description=(
            f"复核 Writer 提交的{_content_type_desc(content_type)}，"
            "产出审核意见（approve 或 reject）。"
            "reject 时必须提供具体修改建议。"
        ),
        persona="严谨的内容审核专家，关注质量和合规性",
        responsibilities=[
            f"评估{_content_type_desc(content_type)}是否符合质量标准",
            "approve：内容通过，转入下一环节",
            "reject：列出具体问题，触发 Writer 重新修改",
        ],
        constraints=[
            "必须提供明确的 approve 或 reject 决定",
            "reject 时必须提供详细的修改建议",
            f"超过 {max_reject_rounds} 轮驳回后升级为 stuck 状态",
        ],
        tools=[],
        metadata={
            "kit_id": "review_approval_kit",
            "role_type": "reviewer",
            "approval_gate": True,
            "approval_gate_config": {
                "approver": reviewer_role_id,
                "on_reject": "retry",
                "timeout_seconds": 300,
            },
        },
    )

    # ── 角色 3：Approver（最终审批，仅 review_then_approve）───────────────
    approver_role_id = "approver"
    approver: Optional[RoleProfile] = None
    if use_approver:
        approver = RoleProfile(
            role_id=approver_role_id,
            name=approver_name,
            description=(
                f"对已通过 Reviewer 复核的{_content_type_desc(content_type)}进行最终审批。"
                "approve 则发布最终产物；reject 则退回 Writer 重写。"
            ),
            persona="决策层审批官，负责最终合规与质量把关",
            responsibilities=[
                "阅读 Reviewer 的审核报告和 Writer 的初稿",
                "做出最终 approve 或 reject 决策",
                "reject 时提供明确的退回理由",
            ],
            constraints=[
                "最终审批决策不可撤销",
                "reject 时必须说明退回原因",
            ],
            tools=[],
            metadata={
                "kit_id": "review_approval_kit",
                "role_type": "approver",
                "approval_gate": True,
                "approval_gate_config": {
                    "approver": approver_role_id,
                    "on_reject": "retry",
                    "timeout_seconds": 300,
                },
            },
        )

    # ── 角色列表 ─────────────────────────────────────────────────────────
    roles: List[RoleProfile] = [writer, reviewer]
    if approver is not None:
        roles.append(approver)

    # ── Policy Matrix（WorkflowPolicyMatrixSpec 字段映射）─────────────────
    # H1 fix (Round 2): 强制顺序 writer → reviewer → approver → END，
    # 杜绝 Approver bypass。allow_send 严格限制每个角色只能流向下一阶段，
    # 由 ApprovalGate（approval_gate_nodes）+ require_approval_for 在运行时
    # 守门：Reviewer 必须 approve 之后才能流向 Approver；Approver 必须
    # approve 之后才能流向 END。reject 路径走 allow_reject 回到 Writer。
    #
    # allow_send: Writer 不可直接 deliver（不含 writer -> END）；
    #             Reviewer 不可直接 END（必须经 Approver，三阶段模式）；
    #             Approver 是唯一允许 END 的角色（三阶段模式）。
    # allow_reject: Reviewer / Approver 可驳回 Writer（触发 retry/rework）。
    # require_approval_after: 标识哪些角色的 send 必须先通过 ApprovalGate
    #                         ——这是 H1 顺序约束的策略层声明。
    allow_send: Dict[str, List[str]] = {
        writer_role_id: [reviewer_role_id],     # Writer 只能发给 Reviewer
        reviewer_role_id: (
            [approver_role_id] if use_approver else ["END"]
        ),                                       # Reviewer → Approver（三阶段）或 END（两阶段）
    }
    if use_approver:
        allow_send[approver_role_id] = ["END"]  # Approver → 最终 END（唯一 END 出口）

    allow_reject: Dict[str, List[str]] = {
        reviewer_role_id: [writer_role_id],     # Reviewer 可驳回 Writer
    }
    if use_approver:
        allow_reject[approver_role_id] = [writer_role_id]  # Approver 也可驳回 Writer

    # 顺序门控：每个角色 send 之前必须通过的 ApprovalGate
    require_approval_after: Dict[str, str] = {
        reviewer_role_id: f"gate_{reviewer_role_id}",
    }
    if use_approver:
        require_approval_after[approver_role_id] = f"gate_{approver_role_id}"

    policy_matrix_config: Dict[str, Any] = {
        "allow_send": allow_send,
        "allow_reject": allow_reject,
        "require_approval_after": require_approval_after,
        "description": (
            f"Review & Approval Kit PolicyMatrix — {approval_levels}，"
            f"max_reject_rounds={max_reject_rounds}。"
            "顺序约束：writer→reviewer→"
            + ("approver→END" if use_approver else "END")
            + "；Approver bypass 由 allow_send + require_approval_after 双重阻断。"
        ),
        "version": "1.1",
    }

    # ── Blueprint metadata ────────────────────────────────────────────────
    bp_metadata: Dict[str, Any] = {
        "kit_id": "review_approval_kit",
        "kit_version": "1.0",
        "goal_inputs": goal_inputs.model_dump(),
        "approval_levels": approval_levels,
        "max_reject_rounds": max_reject_rounds,
        "output_format": output_format,
        "content_type": content_type,
        # PolicyMatrix 预配置（让运行时 service.py 能读取并注入 WorkflowDefinition）
        "policy_matrix": policy_matrix_config,
        # ApprovalGate 节点配置列表（每个有 approval_gate=True 的角色一条）
        "approval_gate_nodes": _build_approval_gate_nodes(
            reviewer_role_id=reviewer_role_id,
            approver_role_id=approver_role_id if use_approver else None,
        ),
        # RetryPolicy（全局默认，Writer 角色的 max_rounds）
        "retry_policy": {
            "max_rounds": max_reject_rounds,
            "on_exceed": "escalated",
        },
    }

    goal_desc = (
        f"审核并审批{_content_type_desc(content_type)}。"
        f"审批层级：{approval_levels}。"
        f"输出格式：{output_format}。"
        f"最大驳回轮次：{max_reject_rounds}。"
    )

    return AgentBlueprint(
        name=f"Review & Approval: {content_type}",
        goal=goal_desc,
        audience="需要内容审核或方案审批的用户",
        mode="team",
        role_profiles=roles,
        tool_policies=[],
        knowledge_bindings=[],
        memory_profile=MemoryProfile(scope="session", enabled=True),
        eval_profile=EvalProfile(
            smoke_eval_enabled=True,
            eval_criteria=[
                "Writer 角色产出初稿（draft 字段非空）",
                "Reviewer 角色产出 approve 或 reject 决策",
                "reject 路径正确触发 checkpoint（checkpoint_created=True）",
                "approve 路径产出 final_artifact 字段非空",
                "ApprovalGateEvent 在 events stream 中出现",
            ],
        ),
        metadata=bp_metadata,
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _content_type_desc(content_type: str) -> str:
    """将 content_type 映射为可读中文描述。"""
    mapping = {
        "document": "文档",
        "code": "代码",
        "proposal": "方案",
        "custom": "内容",
    }
    return mapping.get(content_type, "内容")


def _build_approval_gate_nodes(
    reviewer_role_id: str,
    approver_role_id: Optional[str],
) -> List[Dict[str, Any]]:
    """构建 ApprovalGate 节点配置列表（注入 Blueprint metadata）。

    由运行时 service.py 读取此列表，在 WorkflowDefinition 中插入 approval_gate 节点。
    此处只生成描述性配置，不直接构造 WorkflowDefinition（避免循环依赖）。
    """
    nodes = [
        {
            "node_id": f"gate_{reviewer_role_id}",
            "type": "approval_gate",
            "approver": reviewer_role_id,
            "on_reject": "retry",
            "on_approve": None,
            "timeout_seconds": 300,
            "bound_to_role": reviewer_role_id,
        }
    ]
    if approver_role_id is not None:
        nodes.append(
            {
                "node_id": f"gate_{approver_role_id}",
                "type": "approval_gate",
                "approver": approver_role_id,
                "on_reject": "retry",
                "on_approve": None,
                "timeout_seconds": 300,
                "bound_to_role": approver_role_id,
            }
        )
    return nodes


# ---------------------------------------------------------------------------
# Smoke Run 3 路径 case
# ---------------------------------------------------------------------------

REVIEW_APPROVAL_SMOKE_CASES: List[Dict[str, Any]] = [
    {
        "name": "happy_path",
        "description": "Writer → Reviewer approve → 输出最终产物（review_then_approve 时再经 Approver approve）",
        "steps": [
            {"role": "writer", "action": "draft", "output": "draft content"},
            {"role": "reviewer", "action": "approve", "output": "approved"},
        ],
        "expected": {
            "final_artifact": "draft content",
            "checkpoint_created": False,
            "approval_gate_event_in_stream": True,
            "events": ["approval.pending", "approval.approved"],
        },
    },
    {
        "name": "reject_rework_path",
        "description": "Reviewer reject → Writer rerun → Reviewer approve（2 轮内完成）",
        "steps": [
            {"role": "writer", "action": "draft", "output": "first draft"},
            {"role": "reviewer", "action": "reject", "reason": "needs more detail"},
            {"role": "writer", "action": "rework", "output": "revised draft"},
            {"role": "reviewer", "action": "approve", "output": "approved on second attempt"},
        ],
        "expected": {
            "final_artifact": "revised draft",
            "checkpoint_created": True,
            "approval_gate_event_in_stream": True,
            "events": [
                "approval.pending",
                "approval.rejected",
                "node.retried",
                "approval.pending",
                "approval.approved",
            ],
            "reject_rounds": 1,
        },
    },
    {
        "name": "approvalgate_visibility",
        "description": "验证 ApprovalGateEvent 在 BriefBoard / Inbox 的 events stream 中产生",
        "steps": [
            {"role": "writer", "action": "draft", "output": "sample content"},
            {"role": "reviewer", "action": "pending", "output": "waiting for decision"},
        ],
        "expected": {
            "final_artifact": None,
            "checkpoint_created": False,
            "approval_gate_event_in_stream": True,
            "events": ["approval.pending"],
            "run_status": "awaiting_approval",
        },
    },
]


# ---------------------------------------------------------------------------
# Kit 注册条目（使用 Story 10.5 的正式 KitDefinition）
# ---------------------------------------------------------------------------

# 构建默认 Blueprint（使用默认输入）
_default_inputs = ReviewApprovalGoalInputs()
_default_blueprint = create_review_approval_blueprint(_default_inputs)

# 默认 Scene（3 角色层级）
_default_scene = SceneDefinition(
    scene_id="review_approval_scene",
    display_name="Review & Approval Scene",
    root_roles=[
        SceneRoleNode(
            role_id="writer",
            role_name="Writer",
            role_type="worker",
            description="生成初稿内容",
        ),
        SceneRoleNode(
            role_id="reviewer",
            role_name="Reviewer",
            role_type="boss",
            description="复核内容，可 approve 或 reject",
        ),
        SceneRoleNode(
            role_id="approver",
            role_name="Approver",
            role_type="boss",
            description="最终审批（review_then_approve 模式下激活）",
        ),
    ],
)

# 默认 PolicyProfile
_default_policy_profile = PolicyProfile(
    profile_id="review_approval_policy",
    display_name="Review & Approval Policy",
    default_tool_permission="ask",
    allow_tool_ids=[],
    deny_tool_ids=[],
    # H2 fix (Round 2): Writer 是 producer 不是 approver 角色。
    # require_approval_for 应当列出"需要他们 approve 才能往下走"的 gate 角色，
    # 即 Reviewer + Approver；Writer 的输出由 Reviewer 把关，不是 Writer 自己 approve。
    require_approval_for=["reviewer", "approver"],
    metadata={
        "kit_id": "review_approval_kit",
        "description": (
            "Writer 不可直接 deliver；Reviewer / Approver 是 ApprovalGate 守门人，"
            "顺序约束 writer→reviewer→approver→END，杜绝 Approver bypass。"
        ),
        "send_rules": {
            "writer": ["reviewer"],
            "reviewer": ["approver"],     # 三阶段：reviewer 不能直达 END
            "approver": ["END"],          # 唯一 END 出口
        },
        "reject_rules": {
            "reviewer": ["writer"],
            "approver": ["writer"],
        },
    },
)

# 默认 EvalProfile
_default_eval_profile = EvalProfile(
    smoke_eval_enabled=True,
    eval_criteria=[
        "Writer 角色产出初稿（draft 字段非空）",
        "Reviewer 角色产出 approve 或 reject 决策",
        "reject 路径正确触发 checkpoint（checkpoint_created=True）",
        "approve 路径产出 final_artifact 字段非空",
        "ApprovalGateEvent 在 events stream 中出现",
    ],
    regression_gate=False,
)

REVIEW_APPROVAL_KIT_DEFINITION = KitDefinition(
    kit_id="review_approval_kit",
    display_name="Review & Approval Kit（生成-审核-审批）",
    description=(
        "从内容生成到多轮审核审批的完整闭环 Kit。"
        "支持 Writer→Reviewer 两阶段和 Writer→Reviewer→Approver 三阶段审批流程。"
        "自动预配置 PolicyMatrix 阻止 Writer 绕过审核，"
        "内置驳回上限与 checkpoint resume 机制。"
        "适用于文档审核、代码 review、方案审批等合规场景。"
    ),
    category="review",
    supported_modes=["goal", "scene", "graph"],
    default_blueprint=_default_blueprint,
    default_scene=_default_scene,
    default_policy_profile=_default_policy_profile,
    default_eval_profile=_default_eval_profile,
    default_result_view="approval_inbox",
    recommended_inputs=[
        "content_type",
        "approval_levels",
        "max_reject_rounds",
        "output_format",
        "reviewer_name",
        "approver_name",
    ],
    icon="checkmark-shield",
)

# 在模块导入时自动注册到 REGISTRY
REGISTRY.register(REVIEW_APPROVAL_KIT_DEFINITION)
