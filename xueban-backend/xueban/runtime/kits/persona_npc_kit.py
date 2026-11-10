"""Persona / NPC Kit — Story 10.4

角色、记忆、状态、关系。使 Agent 从一次性执行器演化为有连贯人格、
记忆和状态的持续角色（陪伴型/NPC/长期角色扮演）。

核心设计：
  - Kit = 配置预设，运行时能力来自 Epic 7/9（AgentDM/MemoryProfile/AgentState）
  - memory_retention 三档（minimal/balanced/rich）是产品化封装，不暴露技术字段
  - RelationshipHooks 是角色关系演化的种子结构（chapter15 赛博小镇 Phase 2 扩展）
  - State Fields（mood/relationship_level/interaction_count/last_seen）随对话演化
  - writeback_policy 触发写回（依赖 Story 9.3 context_builder）

架构约束：
  - RoleProfile / AgentBlueprint / EvalProfile / MemoryProfile 从 contracts_builder 导入
  - KitDefinition / PolicyProfile / SceneDefinition 从 registry 导入（Story 10.5）
  - REGISTRY.register() 在模块级触发自动注册
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    MemoryProfile,
    RoleProfile,
    StateField,
)
from xueban.runtime.kits.registry import (
    KitDefinition,
    PolicyProfile,
    REGISTRY,
    SceneDefinition,
    SceneRoleNode,
)


# ---------------------------------------------------------------------------
# RelationshipHook — 关系触发钩子数据结构
# ---------------------------------------------------------------------------


class RelationshipHook(BaseModel):
    """角色关系演化钩子。

    Fields:
        hook_id:      钩子唯一标识
        trigger_type: 触发类型
            "interaction_count_reaches" — 互动次数达到阈值
            "mentioned_name"            — Agent 被点名/提及
            "mood_change"               — 情绪字段变化
        threshold:    触发阈值（整数 or 字符串 pattern）
        effect:       触发后的效果描述（用于运行时解释）
        metadata:     扩展字段（Phase 2 赛博小镇用）
    """

    hook_id: str
    trigger_type: str
    threshold: Any  # int for count, str for name/pattern
    effect: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# MEMORY_RETENTION_PRESETS — 三档预设（产品化封装）
# ---------------------------------------------------------------------------

MEMORY_RETENTION_PRESETS: Dict[str, Dict[str, Any]] = {
    "minimal": {
        "working_memory_limit": 4,
        "episodic_retention_days": 7,
        "semantic_retrieval_top_k": 3,
        "writeback_policy": "key_events_only",
        "display_label": "轻量模式",
        "display_desc": "记住关键事件，适合简短互动",
    },
    "balanced": {
        "working_memory_limit": 8,
        "episodic_retention_days": 30,
        "semantic_retrieval_top_k": 5,
        "writeback_policy": "auto",
        "display_label": "平衡模式",
        "display_desc": "自动管理记忆，适合日常陪伴",
    },
    "rich": {
        "working_memory_limit": 16,
        "episodic_retention_days": 365,
        "semantic_retrieval_top_k": 10,
        "writeback_policy": "all",
        "display_label": "丰富模式",
        "display_desc": "记住所有细节，适合长期深度角色",
    },
}

_VALID_RETENTION = frozenset(MEMORY_RETENTION_PRESETS.keys())


# ---------------------------------------------------------------------------
# PersonaNPCGoalInputs — 向导 5 字段（Pydantic v2）
# ---------------------------------------------------------------------------


class PersonaNPCGoalInputs(BaseModel):
    """Persona / NPC Kit Goal Mode 向导输入 — 5 个字段。

    Fields:
        persona_name:        角色名称（必填，1–200 字符）
        personality:         性格描述（必填，如"沉稳、理性、略带幽默"）
        backstory:           背景故事（可选，作为 semantic memory 种子内容）
        memory_retention:    记忆保留策略 — minimal / balanced / rich
        enable_relationships: 是否启用关系追踪（默认 True）
    """

    persona_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="角色名称（必填）",
    )
    personality: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="性格描述，如「沉稳、理性、略带幽默」（必填）",
    )
    backstory: str = Field(
        default="",
        max_length=5000,
        description="背景故事（可选，用于 MemoryProfile semantic memory 种子）",
    )
    memory_retention: str = Field(
        default="balanced",
        description="记忆保留策略：minimal / balanced / rich",
    )
    enable_relationships: bool = Field(
        default=True,
        description="是否启用关系追踪（RelationshipHooks）",
    )

    @field_validator("memory_retention")
    @classmethod
    def validate_memory_retention(cls, v: str) -> str:
        if v not in _VALID_RETENTION:
            raise ValueError(
                f"memory_retention must be one of {sorted(_VALID_RETENTION)}, got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# _extract_persona_traits — 从 personality 文本提取特征关键词
# ---------------------------------------------------------------------------


def _extract_persona_traits(personality: str) -> Dict[str, str]:
    """从 personality 字符串提取特征标签。

    简单策略：
      - 按中文顿号（、）、英文逗号（,）、空格分割
      - 去重、去空字符串
      - 前 10 个词作为 trait 标签（key=tag_N, value=原词）
    """
    import re
    tokens = re.split(r"[、，,\s]+", personality.strip())
    traits: Dict[str, str] = {}
    for i, tok in enumerate(tokens):
        tok = tok.strip()
        if tok:
            traits[f"trait_{i+1}"] = tok
        if i >= 9:
            break
    return traits


# ---------------------------------------------------------------------------
# create_persona_npc_blueprint — 核心工厂函数
# ---------------------------------------------------------------------------


def create_persona_npc_blueprint(
    goal_inputs: PersonaNPCGoalInputs,
) -> AgentBlueprint:
    """从 Goal 向导输入生成 Persona / NPC AgentBlueprint。

    产出内容：
      - 1 个 RoleProfile（角色本体），包含：
          * persona_traits（从 personality 提取）
          * state_fields（mood / relationship_level / interaction_count / last_seen）
          * system_prompt（personality + backstory 构成角色扮演基础提示词）
      - MemoryProfile（根据 memory_retention 映射预设值）
      - EvalProfile（3 个检查项）
      - Blueprint metadata 包含 RelationshipHooks（若 enable_relationships=True）
        和 State Fields 初始值
    """
    preset = MEMORY_RETENTION_PRESETS[goal_inputs.memory_retention]
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. 构建 system_prompt
    system_prompt_parts = [
        f"你的名字是「{goal_inputs.persona_name}」。",
        f"你的性格特征：{goal_inputs.personality}。",
    ]
    if goal_inputs.backstory.strip():
        system_prompt_parts.append(f"你的背景故事：{goal_inputs.backstory.strip()}")
    system_prompt_parts += [
        "在对话中始终保持角色一致，保持性格稳定。",
        "记住用户提到的重要信息，在后续对话中自然引用。",
        "每次对话结束后，内部状态（心情、关系等级、互动次数）会自动更新。",
    ]
    system_prompt = "\n".join(system_prompt_parts)

    # 2. 提取 persona_traits
    persona_traits = _extract_persona_traits(goal_inputs.personality)

    # 3. State Fields 初始化
    state_fields = [
        StateField(name="mood", type="string", default="neutral"),
        StateField(name="relationship_level", type="number", default=0),
        StateField(name="interaction_count", type="number", default=0),
        StateField(name="last_seen", type="string", default=now_iso),
    ]

    # 4. 构建 RoleProfile
    role = RoleProfile(
        role_id=f"persona_{goal_inputs.persona_name.lower().replace(' ', '_')[:32]}",
        name=goal_inputs.persona_name,
        description=f"持续角色「{goal_inputs.persona_name}」，带人格记忆与状态演化",
        persona=system_prompt,
        responsibilities=[
            "在多轮对话中保持角色人格一致性",
            "记住重要事件并在后续对话中自然引用",
            "根据互动演化关系等级和情绪状态",
            "在每轮对话结束后更新 State Fields",
        ],
        constraints=[
            f"始终以「{goal_inputs.persona_name}」的性格和语气回应",
            "不跳出角色（break character），保持沉浸感",
            "按 MemoryProfile writeback_policy 触发记忆写回",
        ],
        tools=["memory_read", "state_update"],
        persona_traits=persona_traits,
        state_fields=state_fields,
        metadata={
            "kit_id": "persona_npc_kit",
            "memory_retention": goal_inputs.memory_retention,
            "enable_relationships": goal_inputs.enable_relationships,
            "backstory_seed": goal_inputs.backstory[:500] if goal_inputs.backstory else "",
            "memory_preset": {k: v for k, v in preset.items() if not k.startswith("display_")},
        },
    )

    # 5. MemoryProfile — 从预设映射
    memory_profile = MemoryProfile(
        scope="user",
        writeback_target="memory",
        enabled=True,
        metadata={
            "working_memory_limit": preset["working_memory_limit"],
            "episodic_retention_days": preset["episodic_retention_days"],
            "semantic_retrieval_top_k": preset["semantic_retrieval_top_k"],
            "writeback_policy": preset["writeback_policy"],
            "memory_retention_preset": goal_inputs.memory_retention,
        },
    )

    # 6. RelationshipHooks（条件注入）
    relationship_hooks: List[Dict[str, Any]] = []
    if goal_inputs.enable_relationships:
        relationship_hooks = [
            RelationshipHook(
                hook_id="on_interaction_count_reaches_10",
                trigger_type="interaction_count_reaches",
                threshold=10,
                effect="relationship_level += 5；角色语气微调（更亲切）",
                metadata={"relationship_level_delta": 5, "tone_adjust": "warmer"},
            ).model_dump(),
            RelationshipHook(
                hook_id="on_mentioned_name_self",
                trigger_type="mentioned_name",
                threshold=goal_inputs.persona_name,
                effect="触发情景记忆写入（episodic memory）",
                metadata={"writeback_trigger": "episodic", "event_type": "name_mention"},
            ).model_dump(),
        ]

    # 7. EvalProfile — 3 个检查项
    eval_profile = EvalProfile(
        smoke_eval_enabled=True,
        eval_criteria=[
            "persona_tone_check: keyword_consistency — personality 关键词在回答中的覆盖率 ≥ 60%",
            "memory_retention_check: cross_turn_reference — 第 3 轮引用第 1 轮信息为 pass",
            "state_update_check: field_updated — interaction_count 每轮递增为 pass",
        ],
        regression_gate=False,
        metadata={
            "smoke_cases": ["turn_1_greeting", "turn_2_memory_recall", "turn_3_state_check"],
            "persona_tone_keyword_threshold": 0.6,
            "memory_retention_preset": goal_inputs.memory_retention,
            "enable_relationships": goal_inputs.enable_relationships,
        },
    )

    # 8. Blueprint metadata（含 State Fields 初始值 + RelationshipHooks）
    blueprint_metadata: Dict[str, Any] = {
        "kit_id": "persona_npc_kit",
        "persona_name": goal_inputs.persona_name,
        "memory_retention": goal_inputs.memory_retention,
        "enable_relationships": goal_inputs.enable_relationships,
        "initial_state_fields": {
            "mood": "neutral",
            "relationship_level": 0,
            "interaction_count": 0,
            "last_seen": now_iso,
        },
        "relationship_hooks": relationship_hooks,
        "memory_preset": {k: v for k, v in preset.items() if not k.startswith("display_")},
    }

    blueprint = AgentBlueprint(
        name=goal_inputs.persona_name,
        goal=(
            f"作为持续角色「{goal_inputs.persona_name}」，在多轮对话中保持人格一致性，"
            "记住重要事件，随互动演化关系状态，提供有温度的角色扮演体验。"
        ),
        audience="角色扮演爱好者 / 陪伴型 AI 用户 / 游戏 NPC 创作者",
        mode="single",
        role_profiles=[role],
        memory_profile=memory_profile,
        eval_profile=eval_profile,
        metadata=blueprint_metadata,
    )

    return blueprint


# ---------------------------------------------------------------------------
# T2: PERSONA_NPC_SMOKE_CASES — 3 轮连续对话最小 case
# ---------------------------------------------------------------------------

PERSONA_NPC_SMOKE_CASES: List[Dict[str, Any]] = [
    {
        "turn": 1,
        "name": "turn_1_greeting",
        "description": "第 1 轮：用户打招呼并自我介绍，角色用语气与 personality 一致的方式回应",
        "input": "你好，我是小明，第一次来找你聊天。",
        "expected": "角色语气与 personality 一致",
        "eval_check": "persona_tone_check",
        "pass_condition": "personality keywords coverage >= 60% in response",
        "state_snapshot": {"interaction_count": 1},
    },
    {
        "turn": 2,
        "name": "turn_2_memory_recall",
        "description": "第 2 轮：用户测试角色是否记得第 1 轮信息",
        "input": "你还记得我是谁吗？",
        "expected": "引用第 1 轮中用户提到的名字「小明」",
        "eval_check": "memory_retention_check",
        "pass_condition": "response contains cross_turn_reference to turn_1 input",
        "state_snapshot": {"interaction_count": 2},
    },
    {
        "turn": 3,
        "name": "turn_3_state_check",
        "description": "第 3 轮：验证 interaction_count 比第 1 轮大 2",
        "input": "今天聊得很开心，谢谢你！",
        "expected": "角色语气温暖、互动计数递增",
        "eval_check": "state_update_check",
        "pass_condition": "interaction_count == turn_1_count + 2",
        "state_snapshot": {"interaction_count": 3},
        "assertions": [
            {"field": "interaction_count", "op": "eq", "value": 3},
            {"field": "mood", "op": "not_empty"},
            {"field": "last_seen", "op": "not_empty"},
        ],
    },
]


# ---------------------------------------------------------------------------
# PERSONA_NPC_EVAL_PROFILE — 独立 EvalProfile（供 test 层导入）
# ---------------------------------------------------------------------------

PERSONA_NPC_EVAL_PROFILE: Dict[str, Any] = {
    "checks": {
        "persona_tone_check": {
            "method": "keyword_consistency",
            "description": "personality 关键词在回答中的覆盖率 ≥ 60%",
            "pass_threshold": 0.6,
        },
        "memory_retention_check": {
            "method": "cross_turn_reference",
            "description": "第 3 轮引用第 1 轮信息",
            "reference_turn": 1,
            "check_turn": 3,
        },
        "state_update_check": {
            "method": "field_updated",
            "description": "interaction_count 每轮递增",
            "field": "interaction_count",
            "expected_delta": 1,
        },
    }
}


# ---------------------------------------------------------------------------
# PERSONA_NPC_KIT_DEFINITION — 完整注册合同
# ---------------------------------------------------------------------------

_DEFAULT_BLUEPRINT = create_persona_npc_blueprint(
    PersonaNPCGoalInputs(
        persona_name="Aria",
        personality="温柔、善解人意、略带神秘感",
        backstory="一个来自遥远星球的旅者，以倾听和陪伴为使命。",
        memory_retention="balanced",
        enable_relationships=True,
    )
)

_DEFAULT_POLICY_PROFILE = PolicyProfile(
    profile_id="persona_npc_policy",
    display_name="Persona NPC Policy — 角色持续性规则",
    default_tool_permission="ask",
    allow_tool_ids=["memory_read", "state_update"],
    deny_tool_ids=[],
    require_approval_for=[],
    metadata={
        "policy_rules": [
            "persona_consistency: 不跳出角色，始终以 persona_name 语气回应",
            "state_writeback: 每轮对话结束后触发 State Fields 更新",
            "memory_retention: 按 writeback_policy 触发记忆写回",
        ],
        "enable_relationships": True,
    },
)

_DEFAULT_EVAL_PROFILE = EvalProfile(
    smoke_eval_enabled=True,
    eval_criteria=[
        "persona_tone_check: keyword_consistency — personality 关键词在回答中的覆盖率 ≥ 60%",
        "memory_retention_check: cross_turn_reference — 第 3 轮引用第 1 轮信息为 pass",
        "state_update_check: field_updated — interaction_count 每轮递增为 pass",
    ],
    regression_gate=False,
    metadata={
        "smoke_cases": [c["name"] for c in PERSONA_NPC_SMOKE_CASES],
        "persona_tone_keyword_threshold": 0.6,
    },
)

_DEFAULT_SCENE = SceneDefinition(
    scene_id="persona_npc_default",
    display_name="Persona NPC — 持续角色场景",
    root_roles=[
        SceneRoleNode(
            role_id="persona_aria",
            role_name="Aria",
            role_type="solo",
            description="持续角色本体，带人格记忆与状态演化",
            metadata={
                "kit_id": "persona_npc_kit",
                "default_result_view": "agent_dm_with_state",
                "state_fields": ["mood", "relationship_level", "interaction_count", "last_seen"],
            },
        ),
    ],
)

PERSONA_NPC_KIT_DEFINITION = KitDefinition(
    kit_id="persona_npc_kit",
    display_name="Persona / NPC Kit（角色 · 记忆 · 状态）",
    description=(
        "面向角色扮演、陪伴型 AI 和游戏 NPC 的持续角色 Kit。"
        "支持人格定义、记忆保留策略（轻量/平衡/丰富）、状态演化（心情/关系/互动计数）"
        "和关系钩子（互动 10 次升级感情 +5），让 Agent 不再是一次性执行器，"
        "而是有温度、有记忆、随时间演化的角色伙伴。"
    ),
    category="persona",
    supported_modes=["goal", "scene"],
    default_blueprint=_DEFAULT_BLUEPRINT,
    default_scene=_DEFAULT_SCENE,
    default_policy_profile=_DEFAULT_POLICY_PROFILE,
    default_eval_profile=_DEFAULT_EVAL_PROFILE,
    default_result_view="agent_dm_with_state",
    recommended_inputs=[
        "persona_name",
        "personality",
        "backstory",
        "memory_retention",
        "enable_relationships",
    ],
    icon="🎭",
)


# ---------------------------------------------------------------------------
# 模块级自动注册
# ---------------------------------------------------------------------------

REGISTRY.register(PERSONA_NPC_KIT_DEFINITION)


__all__ = [
    "PersonaNPCGoalInputs",
    "RelationshipHook",
    "MEMORY_RETENTION_PRESETS",
    "PERSONA_NPC_KIT_DEFINITION",
    "PERSONA_NPC_SMOKE_CASES",
    "PERSONA_NPC_EVAL_PROFILE",
    "create_persona_npc_blueprint",
]
