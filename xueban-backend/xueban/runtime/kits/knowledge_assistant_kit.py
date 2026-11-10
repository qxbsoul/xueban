"""Knowledge Assistant Kit — Story 10.2

知识问答 + 引用 + 转人工的可信问答助手 Kit。

核心差异化：
  - no_source_response: hit_count=0 时 Policy 层强制拒答，不允许 Answerer 编造
  - human_handoff_event: confidence < threshold 时触发 Escalation 角色
  - citation_required: 强制引用，escalation_keywords 命中时也强制引用
  - Escalation 角色复用 Epic 1 Approval Gate 机制（can_receive_approvals via metadata）

架构约束：
  - RoleProfile / AgentBlueprint / EvalProfile 从 contracts_builder 导入
  - KitDefinition / PolicyProfile / SceneDefinition 从 registry 导入（Story 10.5）
  - REGISTRY.register() 在模块级触发自动注册
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    HandoffRule,
    KnowledgeBinding,
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
# KnowledgeAssistantGoalInputs — 向导 5 字段（Pydantic v2）
# ---------------------------------------------------------------------------


class KnowledgeAssistantGoalInputs(BaseModel):
    """Goal Mode 向导输入 — 5 个字段。

    Fields:
        knowledge_source: 知识来源类型
            "upload"         — 上传文档
            "url"            — 填写 URL
            "existing_pack"  — 绑定已有 KnowledgePack（通过 pack_id 字段提供）
            "none"           — 暂不绑定（拒答策略兜底）
        citation_required: 是否强制引用（默认 True）
        low_confidence_strategy: 低置信度处理策略
            "escalate_human"     — 转人工审核
            "escalate_review"    — 转 Review 队列
            "reject_with_message" — 返回标准拒答
        escalation_keywords: 高风险关键词列表（命中时强制引用），可为空
        assistant_name: 助手名称（用于 AgentDM 显示名）
        pack_id: KnowledgePack ID（当 knowledge_source="existing_pack" 时必填）
        confidence_threshold: 置信度升级阈值（默认 0.5）
    """

    knowledge_source: str = Field(
        default="none",
        description="upload / url / existing_pack / none",
    )
    citation_required: bool = True
    low_confidence_strategy: str = Field(
        default="escalate_human",
        description="escalate_human / escalate_review / reject_with_message",
    )
    escalation_keywords: List[str] = Field(default_factory=list)
    assistant_name: str = Field(
        default="Knowledge Assistant",
        min_length=1,
        max_length=200,
    )
    pack_id: Optional[str] = Field(
        default=None,
        description="KnowledgePack ID，knowledge_source='existing_pack' 时使用",
    )
    confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="置信度升级阈值",
    )

    @field_validator("knowledge_source")
    @classmethod
    def validate_knowledge_source(cls, v: str) -> str:
        valid = {"upload", "url", "existing_pack", "none"}
        if v not in valid:
            raise ValueError(
                f"knowledge_source must be one of {sorted(valid)}, got {v!r}"
            )
        return v

    @field_validator("low_confidence_strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        valid = {"escalate_human", "escalate_review", "reject_with_message"}
        if v not in valid:
            raise ValueError(
                f"low_confidence_strategy must be one of {sorted(valid)}, got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# create_knowledge_assistant_blueprint — 3 角色 Blueprint 生成
# ---------------------------------------------------------------------------


def create_knowledge_assistant_blueprint(
    goal_inputs: KnowledgeAssistantGoalInputs,
) -> AgentBlueprint:
    """从 Goal 向导输入生成 Knowledge Assistant AgentBlueprint。

    生成 3 个 RoleProfile：
      - Retriever: 执行知识库检索，输出命中片段 + confidence score
      - Answerer: 基于命中片段生成回答，带 citation_trace[]
      - Escalation: 当触发升级条件时接管，发出 human_handoff 事件

    PolicyProfile 规则（在 metadata 中声明）：
      - hit_count=0 → Answerer 禁止发言，返回 no_source_response 模板
      - confidence < threshold → 转 Escalation，触发 human_handoff_event
      - escalation_keywords 命中 → 强制引用，不允许引用缺失
      - Retriever 故障 → 回退到拒答（不崩溃整个助手）

    EvalProfile 检查项：
      - doc_hit_rate: 知识包命中率
      - citation_attached_rate: 是否附引用
      - escalation_triggered: 拒答/升级是否按规则触发
    """
    # 1. Retriever 角色
    retriever = RoleProfile(
        role_id="retriever",
        name="Retriever",
        description="执行知识库检索，输出命中片段与 confidence score",
        persona="专注知识检索的信息检索专家",
        responsibilities=[
            "从绑定的 KnowledgePack 中检索相关片段",
            "输出 hit_count 和最高 confidence score",
            "Retriever 故障时回退到拒答，不崩溃整个助手",
        ],
        constraints=[
            "只能访问已绑定的 KnowledgePack",
            "不生成任何答案，只输出检索结果",
        ],
        tools=["knowledge_retrieval"],
        handoff_rules=[
            HandoffRule(trigger="retrieval_done", target_role="answerer"),
            HandoffRule(trigger="retrieval_failed", target_role="escalation"),
        ],
        metadata={
            "role_type": "retriever",
            "output_fields": ["hit_count", "confidence", "chunks"],
        },
    )

    # 2. Answerer 角色
    answerer = RoleProfile(
        role_id="answerer",
        name="Answerer",
        description="基于检索片段生成带 citation_trace 的回答",
        persona="严格遵守知识边界、只基于检索结果作答的问答专家",
        responsibilities=[
            "仅基于 Retriever 提供的命中片段生成回答",
            "每个答案必须附带 citation_trace[]",
            "hit_count=0 时返回 no_source_response 标准拒答模板",
        ],
        constraints=[
            "禁止编造未在知识库中出现的内容",
            f"citation_required={goal_inputs.citation_required} — 强制引用规则",
            "答案置信度 < threshold 时必须转 Escalation，不得独立作答",
        ],
        tools=["citation_service"],
        handoff_rules=[
            HandoffRule(trigger="low_confidence", target_role="escalation"),
            HandoffRule(trigger="no_source", target_role="escalation"),
        ],
        metadata={
            "role_type": "answerer",
            "citation_required": goal_inputs.citation_required,
            "no_source_response": "很抱歉，我没有找到与您问题相关的知识库内容，无法提供答案。请联系人工客服获取帮助。",
            "confidence_threshold": goal_inputs.confidence_threshold,
        },
    )

    # 3. Escalation 角色（复用 Approval Gate 机制）
    escalation = RoleProfile(
        role_id="escalation",
        name="Escalation",
        description="当触发升级条件时接管，发出 human_handoff_event（复用 Approval Gate）",
        persona="负责将低置信度或无知识支撑的问题转交人工的升级处理员",
        responsibilities=[
            "接收来自 Answerer 或 Retriever 的升级请求",
            "发出 human_handoff_event，触发人工介入",
            "记录升级原因和触发条件",
        ],
        constraints=[
            "不独立生成答案",
            "必须通过 ApprovalGate 机制通知人工",
        ],
        tools=["approval_gate"],
        handoff_rules=[],
        metadata={
            "role_type": "escalation",
            "can_receive_approvals": True,
            "approval_gate_event": "human_handoff_event",
            "escalation_strategy": goal_inputs.low_confidence_strategy,
            "escalation_keywords": goal_inputs.escalation_keywords,
        },
    )

    # 4. KnowledgeBinding（若有知识源）
    knowledge_bindings: List[KnowledgeBinding] = []
    if goal_inputs.knowledge_source == "existing_pack" and goal_inputs.pack_id:
        knowledge_bindings.append(
            KnowledgeBinding(
                source_type="pack",
                source_ref=goal_inputs.pack_id,
                citation_required=goal_inputs.citation_required,
                retrieval_mode="auto",
                freshness_hint="on_demand",
                scope="shared",
                metadata={"pack_id": goal_inputs.pack_id},
            )
        )
    elif goal_inputs.knowledge_source in ("upload", "url"):
        # 占位绑定，Epic 9 ingest 流程将填充实际 source_ref
        knowledge_bindings.append(
            KnowledgeBinding(
                source_type=goal_inputs.knowledge_source if goal_inputs.knowledge_source != "upload" else "file",
                source_ref="__pending__",
                citation_required=goal_inputs.citation_required,
                retrieval_mode="auto",
                freshness_hint="on_demand",
                scope="shared",
                metadata={"ingest_pending": True},
            )
        )
    # knowledge_source="none" 时不添加 KnowledgeBinding，Policy 层兜底拒答

    # 5. EvalProfile — 3 个检查项
    eval_profile = EvalProfile(
        smoke_eval_enabled=True,
        eval_criteria=[
            "doc_hit_rate: 知识包检索命中率，目标 > 0.8",
            "citation_attached_rate: 命中路径回答附带 citation_trace 的比率，目标 = 1.0",
            "escalation_triggered: 低置信度和无知识时升级规则按 Policy 触发",
        ],
        regression_gate=False,
        metadata={
            "confidence_threshold": goal_inputs.confidence_threshold,
            "citation_required": goal_inputs.citation_required,
        },
    )

    # 6. Blueprint 元数据中声明 PolicyProfile 规则
    policy_metadata: Dict[str, Any] = {
        "policy_rules": {
            "no_source_rule": {
                "condition": "hit_count == 0",
                "action": "reject",
                "response_template": "no_source_response",
                "target_role": "answerer",
                "description": "无命中文档时 Answerer 禁止发言，返回标准拒答模板",
            },
            "low_confidence_rule": {
                "condition": f"confidence < {goal_inputs.confidence_threshold}",
                "action": "escalate",
                "event": "human_handoff_event",
                "target_role": "escalation",
                "description": f"命中置信度 < {goal_inputs.confidence_threshold} 时转 Escalation 角色",
            },
            "escalation_keywords_rule": {
                "condition": "escalation_keywords_match",
                "keywords": goal_inputs.escalation_keywords,
                "action": "force_citation",
                "description": "escalation_keywords 命中时强制引用，不允许引用缺失",
            },
            "retriever_failure_rule": {
                "condition": "retriever_failed",
                "action": "reject",
                "response_template": "no_source_response",
                "description": "Retriever 故障时回退到拒答，不崩溃整个助手",
            },
        },
        "citation_required": goal_inputs.citation_required,
        "low_confidence_strategy": goal_inputs.low_confidence_strategy,
        "confidence_threshold": goal_inputs.confidence_threshold,
        "escalation_keywords": goal_inputs.escalation_keywords,
        "kit_id": "knowledge_assistant_kit",
    }

    blueprint = AgentBlueprint(
        name=goal_inputs.assistant_name,
        goal=(
            "作为可信知识问答助手，基于绑定的知识库回答用户问题，"
            "严格引用来源，低置信度时转人工，禁止编造答案。"
        ),
        audience="企业用户 / 知识管理员 / 文档助手使用者",
        mode="team",
        role_profiles=[retriever, answerer, escalation],
        knowledge_bindings=knowledge_bindings,
        eval_profile=eval_profile,
        metadata=policy_metadata,
    )

    return blueprint


# ---------------------------------------------------------------------------
# T2: Smoke Run 三路径 case
# ---------------------------------------------------------------------------

KNOWLEDGE_ASSISTANT_SMOKE_CASES: List[Dict[str, Any]] = [
    {
        "name": "hit_path",
        "description": "命中路径 — 输入有对应知识的问题，返回带 citation_trace 的回答",
        "question": "公司的年假政策是什么？",
        "mock_context": {
            "hit_count": 3,
            "confidence": 0.85,
            "chunks": ["员工每年享有10天带薪年假，工龄超过5年后增加至15天。"],
        },
        "expected": "citation_trace attached",
        "expected_fields": ["citation_trace"],
        "pass_condition": "citation_trace is non-empty and hit_count > 0",
    },
    {
        "name": "reject_path",
        "description": "拒答路径 — 输入完全不在知识库中的问题，返回标准拒答模板，不编造",
        "question": "今天股市行情怎么样？",
        "mock_context": {
            "hit_count": 0,
            "confidence": 0.0,
            "chunks": [],
        },
        "expected": "no_source_response",
        "expected_fields": ["response_template"],
        "pass_condition": "response_template == 'no_source_response' and no fabrication",
    },
    {
        "name": "escalate_path",
        "description": "升级路径 — 命中但 confidence < threshold，触发 Escalation，产出 human_handoff_event",
        "question": "这份合同的法律风险是什么？",
        "mock_context": {
            "hit_count": 2,
            "confidence": 0.35,
            "chunks": ["合同中包含不可抗力条款..."],
        },
        "expected": "human_handoff_event",
        "expected_fields": ["event_type"],
        "pass_condition": "event_type == 'human_handoff_event' and confidence < threshold",
    },
]


# ---------------------------------------------------------------------------
# KNOWLEDGE_ASSISTANT_KIT_DEFINITION — 完整注册合同
# ---------------------------------------------------------------------------

_DEFAULT_BLUEPRINT = create_knowledge_assistant_blueprint(
    KnowledgeAssistantGoalInputs(
        knowledge_source="none",
        citation_required=True,
        low_confidence_strategy="escalate_human",
        escalation_keywords=[],
        assistant_name="Knowledge Assistant",
    )
)

_DEFAULT_POLICY_PROFILE = PolicyProfile(
    profile_id="knowledge_assistant_policy",
    display_name="Knowledge Assistant Policy — 可信问答规则",
    default_tool_permission="ask",
    allow_tool_ids=["knowledge_retrieval", "citation_service"],
    deny_tool_ids=[],
    require_approval_for=["human_handoff_event"],
    metadata={
        "policy_rules": ["no_source_rule", "low_confidence_rule", "escalation_keywords_rule"],
        "citation_required": True,
        "confidence_threshold": 0.5,
    },
)

_DEFAULT_EVAL_PROFILE = EvalProfile(
    smoke_eval_enabled=True,
    eval_criteria=[
        "doc_hit_rate: 知识包检索命中率，目标 > 0.8",
        "citation_attached_rate: 命中路径回答附带 citation_trace 的比率，目标 = 1.0",
        "escalation_triggered: 低置信度和无知识时升级规则按 Policy 触发",
    ],
    regression_gate=False,
    metadata={
        "smoke_cases": [c["name"] for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES],
        "confidence_threshold": 0.5,
    },
)

_DEFAULT_SCENE = SceneDefinition(
    scene_id="knowledge_assistant_default",
    display_name="Knowledge Assistant — 可信问答场景",
    root_roles=[
        SceneRoleNode(
            role_id="retriever",
            role_name="Retriever",
            role_type="worker",
            description="执行知识库检索，输出命中片段 + confidence score",
        ),
        SceneRoleNode(
            role_id="answerer",
            role_name="Answerer",
            role_type="worker",
            description="基于命中片段生成回答，带 citation_trace[]",
        ),
        SceneRoleNode(
            role_id="escalation",
            role_name="Escalation",
            role_type="solo",
            description="低置信度/无知识时接管，发出 human_handoff_event",
            metadata={"human": True, "badge": "human_handoff"},
        ),
    ],
)

KNOWLEDGE_ASSISTANT_KIT_DEFINITION = KitDefinition(
    kit_id="knowledge_assistant_kit",
    display_name="Knowledge Assistant Kit（知识问答助手）",
    description=(
        "面向文档、FAQ、制度、产品资料等知识库场景的可信问答助手。"
        "支持强制引用来源、低置信度转人工，禁止编造答案。"
        "适用于企业内知识管理、客服知识库、产品文档助手等场景。"
    ),
    category="knowledge",
    supported_modes=["goal", "scene"],
    default_blueprint=_DEFAULT_BLUEPRINT,
    default_scene=_DEFAULT_SCENE,
    default_policy_profile=_DEFAULT_POLICY_PROFILE,
    default_eval_profile=_DEFAULT_EVAL_PROFILE,
    default_result_view="agent_dm_with_state",
    recommended_inputs=[
        "knowledge_source",
        "citation_required",
        "low_confidence_strategy",
        "escalation_keywords",
        "assistant_name",
    ],
    icon="📚",
)


# ---------------------------------------------------------------------------
# 模块级自动注册
# ---------------------------------------------------------------------------

REGISTRY.register(KNOWLEDGE_ASSISTANT_KIT_DEFINITION)


__all__ = [
    "KnowledgeAssistantGoalInputs",
    "KNOWLEDGE_ASSISTANT_KIT_DEFINITION",
    "KNOWLEDGE_ASSISTANT_SMOKE_CASES",
    "create_knowledge_assistant_blueprint",
]
