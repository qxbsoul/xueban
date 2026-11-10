"""Research Kit — Story 10.1

规划-搜集-总结-报告四角色研究闭环 Kit。

设计原则：
  - Kit = 预配置的 AgentBlueprint 工厂 + KitDefinition 注册条目
  - 4 个 RoleProfile（Planner / Researcher / Summarizer / Report Writer）
  - citation_required 开关控制 KnowledgeBinding 的 citation_required 字段
  - Smoke Run 产物格式：todos / progress_log / summary / report / citations

当 Story 10.5 的 KitRegistry 已存在时，本模块在模块级调用
REGISTRY.register(RESEARCH_KIT) 完成自动注册，
由 discover_and_register_kits() 在服务启动时触发。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    KnowledgeBinding,
    MemoryProfile,
    RoleProfile,
    ToolPolicy,
)
from xueban.runtime.kits.registry import (
    REGISTRY,
    KitDefinition,
    PolicyProfile,
    SceneDefinition,
    SceneRoleNode,
)


# ---------------------------------------------------------------------------
# 向导输入 Pydantic 模型
# ---------------------------------------------------------------------------


class ResearchGoalInputs(BaseModel):
    """Research Kit Goal Mode 向导的 5 个输入字段。

    Pydantic v2，field_validator 进行范围和枚举校验。
    """

    research_topic: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="研究主题（必填）",
    )
    output_format: str = Field(
        default="report",
        description="研究目标/输出形式：answer / report / structured_outline",
    )
    freshness: str = Field(
        default="any",
        description="资料新鲜度：latest / within_month / any",
    )
    citation_required: bool = Field(
        default=True,
        description="是否强制引用（true=需要附引用来源）",
    )
    max_search_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="最大搜索轮次 / 深度（1–5，默认 2）",
    )

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, v: str) -> str:
        allowed = {"answer", "report", "structured_outline"}
        if v not in allowed:
            raise ValueError(f"output_format 必须是 {allowed} 之一，当前值：{v!r}")
        return v

    @field_validator("freshness")
    @classmethod
    def validate_freshness(cls, v: str) -> str:
        allowed = {"latest", "within_month", "any"}
        if v not in allowed:
            raise ValueError(f"freshness 必须是 {allowed} 之一，当前值：{v!r}")
        return v


# ---------------------------------------------------------------------------
# Blueprint 工厂函数
# ---------------------------------------------------------------------------


def create_research_blueprint(goal_inputs: ResearchGoalInputs) -> AgentBlueprint:
    """从向导输入生成 Research Kit 默认 AgentBlueprint。

    包含 4 个 RoleProfile：
      1. Planner      — 拆解研究主题为子任务/搜索计划（can_spawn_tasks=True）
      2. Researcher   — 执行搜索/文档检索，输出原始片段
      3. Summarizer   — 对原始片段去重整理，产出中间摘要
      4. Report Writer — 把摘要整合为最终结构化报告

    若 citation_required=True，Researcher 角色的 metadata 标记 citation_required=True。
    """
    topic = goal_inputs.research_topic
    output_fmt = goal_inputs.output_format
    freshness = goal_inputs.freshness
    cite = goal_inputs.citation_required
    rounds = goal_inputs.max_search_rounds

    # ── 角色 1：Planner ──────────────────────────────────────────────────────
    planner = RoleProfile(
        name="Planner",
        description=(
            f"把研究主题「{topic}」拆解为结构化子任务和搜索计划，"
            f"最多规划 {rounds} 轮搜索。"
        ),
        persona="资深研究规划专家，擅长将模糊主题分解为可执行的子问题",
        responsibilities=[
            "分析研究主题，识别核心问题和子问题",
            f"制定最多 {rounds} 轮搜索计划，每轮产出 TODO 列表",
            "确保子任务覆盖研究目标的各个维度",
        ],
        constraints=[
            "输出必须是结构化 TODO 列表，不是自然语言段落",
            "每个 TODO 项对应一个可独立执行的搜索查询",
        ],
        tools=["builtin:web_search"],
        can_spawn_tasks=True,
        metadata={
            "kit_id": "research_kit",
            "role_type": "planner",
            "max_search_rounds": rounds,
        },
    )

    # ── 角色 2：Researcher ───────────────────────────────────────────────────
    researcher_meta: Dict[str, Any] = {
        "kit_id": "research_kit",
        "role_type": "researcher",
        "citation_required": cite,
        "freshness": freshness,
    }
    researcher_constraints = [
        "每个搜索结果必须保留原始 URL 来源",
        f"资料新鲜度要求：{freshness}",
    ]
    if cite:
        researcher_constraints.append(
            "每个检索片段必须附带 citation_trace（pack_id / source_id / excerpt）"
        )

    researcher = RoleProfile(
        name="Researcher",
        description=(
            "执行 Planner 分配的搜索任务，收集原始信息片段并整理为结构化证据集。"
        ),
        persona="专注细节的信息分析师，擅长从多源检索中提取关键证据",
        responsibilities=[
            "按 Planner 的 TODO 列表逐项执行搜索",
            "记录每条信息的来源 URL 和摘要",
            "追加研究进度日志（progress_log）",
        ],
        constraints=researcher_constraints,
        tools=["builtin:web_search", "builtin:fetch_url", "builtin:read_knowledge_pack"],
        metadata=researcher_meta,
    )

    # ── 角色 3：Summarizer ───────────────────────────────────────────────────
    summarizer = RoleProfile(
        name="Summarizer",
        description="对 Researcher 输出的原始信息片段进行去重、整合，产出中间摘要。",
        persona="编辑型分析师，擅长从海量信息中提炼核心洞察",
        responsibilities=[
            "去除重复和低质量信息片段",
            "按主题维度归纳整理，产出中间摘要",
            "保留引用来源索引供 Report Writer 使用",
        ],
        constraints=[
            "摘要必须保留原始引用来源索引",
            "不引入未出现在原始片段中的新信息",
        ],
        tools=[],
        metadata={
            "kit_id": "research_kit",
            "role_type": "summarizer",
        },
    )

    # ── 角色 4：Report Writer ────────────────────────────────────────────────
    output_format_desc = {
        "answer": "简洁直接的问答形式",
        "report": "含标题/章节/结论的完整 Markdown 报告",
        "structured_outline": "层级化结构大纲，每节含关键要点",
    }.get(output_fmt, "Markdown 报告")

    writer = RoleProfile(
        name="Report Writer",
        description=(
            f"将 Summarizer 的中间摘要整合为{output_format_desc}，"
            "若启用引用则附来源引用列表。"
        ),
        persona="专业技术写作者，擅长将复杂信息转化为清晰易读的报告",
        responsibilities=[
            f"将摘要整合为{output_format_desc}",
            "确保报告结构完整（含标题/章节/结论）",
            "若 citation_required=true，在报告末尾附来源引用列表",
        ],
        constraints=[
            "输出必须是 Markdown 格式",
            "每个章节必须有明确标题",
            "结论部分不可省略",
        ],
        tools=[],
        metadata={
            "kit_id": "research_kit",
            "role_type": "report_writer",
            "output_format": output_fmt,
        },
    )

    # ── Knowledge Bindings ───────────────────────────────────────────────────
    # 注意：AgentBlueprint 的 model_validator 要求 citation_required=True 时
    # source_type != 'unspecified'，所以占位 KB 用 citation_required=False，
    # 用户选择知识来源后由 Story 10.5 补全真实 source_type 和 source_ref。
    knowledge_bindings: List[KnowledgeBinding] = []

    # ── Tool Policies ────────────────────────────────────────────────────────
    tool_policies: List[ToolPolicy] = [
        ToolPolicy(
            tool_id="builtin:web_search",
            visibility="enabled",
            default_permission="allow",
            side_effects="read_only",
        ),
        ToolPolicy(
            tool_id="builtin:fetch_url",
            visibility="enabled",
            default_permission="allow",
            side_effects="read_only",
        ),
        ToolPolicy(
            tool_id="builtin:read_knowledge_pack",
            visibility="enabled",
            default_permission="allow",
            side_effects="read_only",
        ),
    ]

    # ── Blueprint metadata ───────────────────────────────────────────────────
    bp_metadata: Dict[str, Any] = {
        "kit_id": "research_kit",
        "kit_version": "1.0",
        "goal_inputs": goal_inputs.model_dump(),
        "citation_required": cite,
        "output_format": output_fmt,
        "freshness": freshness,
        "max_search_rounds": rounds,
    }

    return AgentBlueprint(
        name=f"Research: {topic[:60]}",
        goal=(
            f"研究主题：{topic}。"
            f"输出形式：{output_fmt}。"
            f"资料新鲜度：{freshness}。"
            f"最大搜索轮次：{rounds}。"
        ),
        audience="研究型用户 / 分析师 / 内容策划",
        mode="team",
        role_profiles=[planner, researcher, summarizer, writer],
        knowledge_bindings=knowledge_bindings,
        tool_policies=tool_policies,
        memory_profile=MemoryProfile(scope="session", enabled=True),
        eval_profile=EvalProfile(
            smoke_eval_enabled=True,
            eval_criteria=[
                "Planner 输出 todos 列表非空",
                "Researcher 追加 progress_log 条目",
                "Summarizer 产出 summary 字段",
                "Report Writer 产出 report 字段（Markdown）",
                "citation_required=true 时 citations 列表非空",
            ],
        ),
        metadata=bp_metadata,
    )


# ---------------------------------------------------------------------------
# 默认 AgentBlueprint（用于 KitDefinition.default_blueprint）
# ---------------------------------------------------------------------------

_DEFAULT_GOAL_INPUTS = ResearchGoalInputs(
    research_topic="研究主题占位（由用户在向导中填写）",
    output_format="report",
    freshness="any",
    citation_required=True,
    max_search_rounds=2,
)

_DEFAULT_BLUEPRINT = create_research_blueprint(_DEFAULT_GOAL_INPUTS)

# ---------------------------------------------------------------------------
# 默认 SceneDefinition（4 角色层级）
# ---------------------------------------------------------------------------

_DEFAULT_SCENE = SceneDefinition(
    scene_id="research_default_scene",
    display_name="研究闭环场景（4 角色）",
    root_roles=[
        SceneRoleNode(
            role_id="planner",
            role_name="Planner",
            role_type="boss",
            description="拆解研究主题为子任务/搜索计划",
            sub_roles=[
                SceneRoleNode(
                    role_id="researcher",
                    role_name="Researcher",
                    role_type="worker",
                    description="执行搜索/文档检索，输出原始片段",
                ),
                SceneRoleNode(
                    role_id="summarizer",
                    role_name="Summarizer",
                    role_type="worker",
                    description="对原始片段去重整理，产出中间摘要",
                ),
                SceneRoleNode(
                    role_id="report_writer",
                    role_name="Report Writer",
                    role_type="worker",
                    description="把摘要整合为最终结构化报告",
                ),
            ],
        )
    ],
)

# ---------------------------------------------------------------------------
# KitDefinition 注册条目（使用 Story 10.5 的正式 KitDefinition 类型）
# ---------------------------------------------------------------------------

RESEARCH_KIT_DEFINITION_OBJ = KitDefinition(
    kit_id="research_kit",
    display_name="Research Kit（规划-搜集-总结-报告）",
    description=(
        "从研究主题快速得到带引用、可追踪、可复用的研究结果。"
        "自动构建规划/搜集/总结/报告四角色流水线，无需手工拼节点。"
    ),
    category="research",
    supported_modes=["goal", "scene", "graph"],
    default_blueprint=_DEFAULT_BLUEPRINT,
    default_scene=_DEFAULT_SCENE,
    default_policy_profile=PolicyProfile(
        profile_id="research_default_policy",
        display_name="Research Kit 默认权限",
        default_tool_permission="allow",
        allow_tool_ids=[
            "builtin:web_search",
            "builtin:fetch_url",
            "builtin:read_knowledge_pack",
        ],
        deny_tool_ids=[],
        require_approval_for=[],
    ),
    default_eval_profile=EvalProfile(
        smoke_eval_enabled=True,
        eval_criteria=[
            "Planner 输出 todos 列表非空",
            "Researcher 追加 progress_log 条目",
            "Summarizer 产出 summary 字段",
            "Report Writer 产出 report 字段（Markdown）",
            "citation_required=true 时 citations 列表非空",
        ],
        regression_gate=False,
    ),
    default_result_view="research_report",
    recommended_inputs=[
        "research_topic",
        "output_format",
        "freshness",
        "citation_required",
        "max_search_rounds",
    ],
    icon="flask",
)

# 向 REGISTRY 注册（discover_and_register_kits() 导入本模块时自动触发）
REGISTRY.register(RESEARCH_KIT_DEFINITION_OBJ)

# 向后兼容别名（供 __init__.py 导出）
RESEARCH_KIT_DEFINITION = RESEARCH_KIT_DEFINITION_OBJ

# ---------------------------------------------------------------------------
# Smoke Run 产物结构（Research Kit 专属）
# ---------------------------------------------------------------------------


class CitationTrace(TypedDict, total=False):
    """单条引用追踪记录（占位；Story 9.2 Citation Service 提供完整实现）。"""

    pack_id: str
    source_id: str
    excerpt: str
    url: str


class ResearchSmokeResult(TypedDict):
    """Research Kit Smoke Run 五类产物结构。

    Smoke Run 不执行真实搜索，产物为结构化占位数据，
    供 Builder 验证面板折叠展示。
    """

    todos: List[str]
    progress_log: List[str]
    summary: str
    report: str
    citations: List[CitationTrace]


def build_smoke_result(
    goal_inputs: ResearchGoalInputs,
    *,
    mock_search_results: Optional[List[str]] = None,
) -> ResearchSmokeResult:
    """生成 Research Kit Smoke Run 占位产物。

    参数
    ----
    goal_inputs : ResearchGoalInputs
        向导输入。
    mock_search_results : list[str], optional
        测试时注入的模拟搜索结果片段列表。

    返回
    ----
    ResearchSmokeResult 字典，含 5 个字段（todos / progress_log / summary / report / citations）。
    """
    topic = goal_inputs.research_topic
    rounds = goal_inputs.max_search_rounds
    cite = goal_inputs.citation_required
    fmt = goal_inputs.output_format

    snippets = mock_search_results or []

    # Planner 输出：TODO 列表（每轮一条占位）
    todos: List[str] = [
        f"[轮次 {i + 1}] 搜索「{topic}」相关内容" for i in range(rounds)
    ]

    # Researcher 输出：进度日志
    progress_log: List[str] = [
        f"[Researcher] 轮次 {i + 1} 完成：检索到 {len(snippets)} 条片段"
        for i in range(rounds)
    ]

    # Summarizer 输出：中间摘要
    summary: str = (
        f"【摘要占位】主题「{topic}」的研究摘要，"
        f"共执行 {rounds} 轮搜索，整合 {len(snippets)} 条原始片段。"
        "（实际摘要由 Summarizer 角色在运行时生成）"
    )

    # Report Writer 输出：最终报告
    report_header = f"# 研究报告：{topic}\n\n"
    if fmt == "answer":
        report_body = f"**结论**：关于「{topic}」的直接回答将在运行时由 Report Writer 生成。\n"
    elif fmt == "structured_outline":
        report_body = (
            "## 1. 研究背景\n\n## 2. 核心发现\n\n## 3. 结论\n\n"
            "（以上为结构大纲占位，实际内容由 Report Writer 生成）\n"
        )
    else:
        report_body = (
            "## 研究背景\n\n（占位）\n\n"
            "## 核心发现\n\n（占位）\n\n"
            "## 结论\n\n（占位）\n\n"
            "（实际报告内容由 Report Writer 角色在运行时生成）\n"
        )
    report: str = report_header + report_body

    # 引用列表
    citations: List[CitationTrace] = []
    if cite:
        for i, snippet in enumerate(snippets):
            citations.append(
                CitationTrace(
                    pack_id="placeholder-pack",
                    source_id=f"src-{i:03d}",
                    excerpt=snippet[:200] if snippet else "(占位)",
                    url="",
                )
            )
        if not citations:
            # 无真实片段时，保留至少一条占位引用（Smoke Run 验证引用字段存在）
            citations.append(
                CitationTrace(
                    pack_id="placeholder-pack",
                    source_id="src-000",
                    excerpt="（引用占位，等待真实搜索结果）",
                    url="",
                )
            )

    return ResearchSmokeResult(
        todos=todos,
        progress_log=progress_log,
        summary=summary,
        report=report,
        citations=citations,
    )
