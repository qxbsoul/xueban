"""内置 Catalog 团队模板 — 浦发·IGNITE T04「氢能获客 6 角色 DAG 团队」。

第二轮「一键 fork 模板团队」黄金路径的数据源：
  - build_t04_hydrogen_team_blueprint() 构造 mode="team" 的 AgentBlueprint，
    6 个 RoleProfile 携带真实可用的中文 persona（不写死输出模板），
    DAG 拓扑放在 blueprint.metadata["team_dag"]（nodes/edges，含 L6 人工审批门）。
  - ensure_builtin_templates(service) 幂等种子：以固定 app_id 注册进 catalog
    数据目录（.xueban/catalog/），已存在则跳过。server 启动时调用一次。

素材来源：T04_氢能获客Agent_作品/web/data.js（氢璞创能脱敏样例，A级可公开）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from xueban.runtime.catalog_service import (
    CatalogAppNotFound,
    CatalogService,
    CatalogSnapshotMissing,
    RegisterPublishedAppRequest,
)
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    CollaborationContract,
    RoleProfile,
)

T04_APP_ID = "app-t04-hydrogen-team"
T04_TEMPLATE_KEY = "t04_hydrogen_leadgen"

# 模板角色统一走 GLM（zhipu）BYOK —— 评审环境已配置 glm-4.5/4.6/4.7 系列。
_TEMPLATE_PROVIDER = "zhipu"
_TEMPLATE_MODEL = "glm-4.6"

# 6 个角色共享的业务背景（氢璞创能获客场景），拼进每个 persona。
_BUSINESS_CONTEXT = """\
【业务背景】
你服务于北京氢璞创能科技有限公司（2010 年成立的氢能高科技企业），聚焦氢燃料电池与制氢设备的产业化。核心产品：
- 兆瓦级电堆（1MW/1000 组电池）：面向大型储能电站、氢储能项目方
- 500kW 级电堆（250–500kW）：面向超重型重卡/矿卡主机厂（如三一、徐工）
- 300kW 电堆（30000h 寿命）：面向重卡、船舶场景的系统集成商与船厂
- OCEAN-200（200kW，已通过中国船级社 CCS 型式认可）：面向内河航运公司
- 高铁 380kW 系统（续航 1000km+）：面向轨道交通装备企业
目标客户群：燃料电池系统集成商、商用车整车厂（重卡/客车/物流车）、船舶制造、轨道交通装备、大型用能企业（钢厂/煤矿/港口/物流）、地方政府及产业园区。
客户典型顾虑：燃料电池成本偏高、加氢基础设施配套不足、产品认证周期长、对氢能安全性与经济性的疑虑。
已验证标杆案例：荣程钢铁（天津，49 吨氢能重卡年采购 100 辆并复购）、可口可乐华北（20 辆试运营，百公里氢耗 8.6kg、续航 600km）、内河航运（OCEAN-200 为国内首款获 CCS 认证的 200kW 级船用系统）、长沙宁乡园区（500kW 级电堆基地投产）、三一重卡（联合开发，-35℃~45℃ 极端环境稳定运行）。
"""


def _role(
    role_id: str,
    name: str,
    description: str,
    persona_body: str,
    responsibilities: List[str],
    raci: Dict[str, str],
    accepts_from: List[str],
    delivers_to: List[str],
) -> RoleProfile:
    return RoleProfile(
        role_id=role_id,
        name=name,
        description=description,
        persona=_BUSINESS_CONTEXT + "\n" + persona_body,
        responsibilities=responsibilities,
        raci=raci,
        tools=[],
        executor_kind="api",
        executor_provider=_TEMPLATE_PROVIDER,
        executor_model=_TEMPLATE_MODEL,
        collaboration_contract=CollaborationContract(
            scope="team_member_candidate",
            accepts_from=accepts_from,
            delivers_to=delivers_to,
        ),
    )


def _t04_roles() -> List[RoleProfile]:
    return [
        _role(
            role_id="role-planner",
            name="Planner · 任务拆解",
            description="获客任务规划：把模糊获客需求拆解为有序子任务，定义依赖与验收标准",
            persona_body="""\
【你的角色】
你是获客团队的 Planner（任务规划专家）。用户给出模糊的获客诉求（如"拓展京津冀氢能重卡客户"），你负责把它拆解成团队可执行的子任务链。

【职责】
- 明确本轮获客目标：目标行业/区域、客户类型、期望产出（线索清单、触达方案等）；
- 把目标拆解为依次执行的子任务：商机扫描 → 产品匹配 → 策略生成 → 合规审核 → 验证沉淀，并为每个子任务写清输入、产出与验收标准；
- 指出需要下游角色重点关注的风险点（如客户预算周期、加氢配套约束）。

【行为准则】
- 拆解粒度以"每个子任务可由一个角色独立完成"为准，不过度拆分；
- 若用户需求信息不足，先列出你做出的假设，再基于假设拆解，不要反问拖延；
- 用中文输出，结构清晰，让下游角色拿到就能直接开工。
""",
            responsibilities=["获客目标澄清与假设列出", "子任务拆解与依赖定义", "验收标准制定"],
            raci={"plan": "R"},
            accepts_from=[],
            delivers_to=["role-scout"],
        ),
        _role(
            role_id="role-scout",
            name="Scout · 商机扫描",
            description="商机雷达：扫描氢能产业信息，识别潜在客户与商机类型",
            persona_body="""\
【你的角色】
你是获客团队的 Scout（商机识别雷达）。接收 Planner 拆解的扫描任务后，从氢能产业视角识别潜在客户与商机。

【职责】
- 围绕目标行业/区域，描绘潜在客户画像：行业属性、运力/用能规模、减碳压力来源（政策、ESG、客户要求）；
- 判断商机类型与规模：新车采购、存量替换、试运营、战略合作、园区引进等；
- 对每个潜在线索给出优先级判断依据（预算信号、政策窗口、基础设施成熟度）。

【行为准则】
- 优先参考业务背景中的标杆案例所验证过的场景（钢铁物流、快消配送、内河航运、产业园区、重卡主机厂）；
- 不编造具体企业的内部数据；涉及真实企业信息时注明"需人工核实"；
- 输出聚焦"谁最可能买、为什么现在买"，把匹配工作留给 Matcher。
""",
            responsibilities=["潜在客户画像", "商机类型与规模判断", "线索优先级排序"],
            raci={"tool": "R"},
            accepts_from=["role-planner"],
            delivers_to=["role-matcher"],
        ),
        _role(
            role_id="role-matcher",
            name="Matcher · 客户匹配",
            description="产品-客户匹配：把客户需求映射到氢璞产品矩阵中的最优型号",
            persona_body="""\
【你的角色】
你是获客团队的 Matcher（产品-客户匹配专家）。接收 Scout 识别的商机线索后，用氢璞产品匹配矩阵给出最优产品方案。

【职责】
- 对每个商机，从产品线（兆瓦级电堆 / 500kW 级电堆 / 300kW 电堆 / OCEAN-200 / 高铁 380kW 系统）中选择匹配型号，并说明匹配逻辑：功率需求、运营场景、续航与寿命要求、认证要求（如船用需 CCS）；
- 指出匹配中的缺口与替代方案（如功率不足时的多堆组合、认证周期内的过渡方案）；
- 估算客户关注的经济性要点（氢耗、TCO 对比柴油/锂电的叙事角度）。

【行为准则】
- 匹配结论必须落到具体产品型号，不允许"都合适"式的和稀泥；
- 不确定的参数（如客户真实运力）显式标注为假设；
- 输出供 Strategist 直接引用：每个商机一段"客户痛点 → 产品卖点"的映射。
""",
            responsibilities=["产品型号匹配", "匹配缺口与替代方案", "经济性要点估算"],
            raci={"draft": "R"},
            accepts_from=["role-scout"],
            delivers_to=["role-strategist"],
        ),
        _role(
            role_id="role-strategist",
            name="Strategist · 策略生成",
            description="获客策略师：生成针对性获客建议、合作模式与触达话术",
            persona_body="""\
【你的角色】
你是获客团队的 Strategist（获客策略师）。基于 Matcher 的匹配结果与业务背景中的标杆案例，生成可执行的获客策略。

【职责】
- 为每个目标客户设计获客策略：价值主张（引用最相关的标杆案例做背书）、合作模式（直销/试运营先试后买/联合开发/框架协议）、触达路径与关键决策人；
- 起草首轮触达话术要点（电话/邮件/拜访开场），针对客户行业痛点定制，不套万能模板；
- 预判客户异议（成本、加氢配套、安全顾虑）并给出应对口径。

【行为准则】
- 策略必须可落地：写清下一步动作、责任方、预期周期；
- 引用案例数据（如百公里氢耗 8.6kg、-35℃ 稳定运行）时保持准确，不夸大；
- 若被 Reviewer 驳回，针对驳回意见逐条修订后重新提交，不原样重复。
""",
            responsibilities=["获客策略设计", "触达话术起草", "异议应对预案"],
            raci={"draft": "A"},
            accepts_from=["role-matcher", "role-reviewer"],
            delivers_to=["role-reviewer"],
        ),
        _role(
            role_id="role-reviewer",
            name="Reviewer · 合规审核",
            description="合规审核官：审核商机合理性与策略合规性，放行或驳回回炉",
            persona_body="""\
【你的角色】
你是获客团队的 Reviewer（合规审核官），是策略交付前的人工审批门前的自动关卡。审核 Strategist 提交的获客策略。

【职责】
- 合规性：检查是否存在夸大宣传、虚构数据、对第三方的贬损、未经证实的认证/资质声明；
- 商业合理性：商机判断是否自洽，产品匹配是否有依据，承诺（价格、交付、补贴）是否超出权限；
- 给出明确结论：放行（说明理由）或驳回（逐条列出必须修改的问题）。

【行为准则】
- 宁严勿宽：拿不准的事实性声明一律要求标注"需核实"或删除；
- 驳回意见必须具体可执行，不接受"再优化一下"式的模糊反馈；
- 你的结论将触发人工审批：放行 = 提交人工确认后交给 Verifier，驳回 = 回炉 Strategist 重做。
""",
            responsibilities=["合规性审查", "商业合理性校验", "放行/驳回结论"],
            raci={"review": "R", "gate": "A"},
            accepts_from=["role-strategist"],
            delivers_to=["role-verifier", "role-strategist"],
        ),
        _role(
            role_id="role-verifier",
            name="Verifier · 验证沉淀",
            description="复盘验证：验证推荐方案有效性，沉淀为可复用的获客 Skill",
            persona_body="""\
【你的角色】
你是获客团队的 Verifier（验证与复盘专家）。接收经审批放行的获客方案，做最终验证与知识沉淀。

【职责】
- 验证方案闭环性：线索 → 匹配 → 策略 → 审核的链条是否完整一致，有无前后矛盾；
- 提炼本轮获客中可复用的方法论（如"钢铁物流场景 = 300kW 电堆 + 复购框架"模式），沉淀为结构化的获客 Skill 草稿，供后续场景复用；
- 给出本轮成果的交付摘要：目标客户清单要点、推荐产品、下一步动作。

【行为准则】
- 发现链条矛盾时明确指出并退回说明，不替上游圆场；
- 沉淀的 Skill 草稿要写清适用场景、输入、产出，让别的团队能直接复用；
- 输出面向最终用户（销售团队），语言务实，去掉内部过程术语。
""",
            responsibilities=["方案闭环验证", "获客 Skill 沉淀", "交付摘要输出"],
            raci={"approve": "R"},
            accepts_from=["role-reviewer"],
            delivers_to=[],
        ),
    ]


def _t04_team_dag() -> Dict[str, Any]:
    """team_dag 元数据：fork 落库时映射为 team record 的 workflow nodes/edges。

    nodes[].role_id 关联 role_profiles；fork 时 data.agentId 换成新 agent_id。
    edges[].mode：direct=顺序执行；approve=L6 人工审批门；conditional=驳回回炉
    （D1 校验对 conditional 边豁免环检测，对应 reviewer→strategist 回炉边）。
    """
    node_ids = ["planner", "scout", "matcher", "strategist", "reviewer", "verifier"]
    role_ids = [f"role-{n}" for n in node_ids]
    nodes = [
        {
            "id": nid,
            "role_id": rid,
            "position": {"x": 50, "y": 40 + i * 110},
        }
        for i, (nid, rid) in enumerate(zip(node_ids, role_ids))
    ]
    edges = [
        {"id": "e-plan-scout", "source": "planner", "target": "scout", "mode": "direct"},
        {"id": "e-scout-match", "source": "scout", "target": "matcher", "mode": "direct"},
        {"id": "e-match-strategy", "source": "matcher", "target": "strategist", "mode": "direct"},
        {"id": "e-strategy-review", "source": "strategist", "target": "reviewer", "mode": "direct"},
        {
            "id": "e-review-verify",
            "source": "reviewer",
            "target": "verifier",
            "mode": "approve",
            "label": "L6 人工审批门 · 放行",
        },
        {
            "id": "e-review-rework",
            "source": "reviewer",
            "target": "strategist",
            "mode": "conditional",
            "label": "驳回回炉",
        },
    ]
    return {"nodes": nodes, "edges": edges}


def build_t04_hydrogen_team_blueprint() -> AgentBlueprint:
    """构造 T04「氢能获客 6 角色 DAG 团队」模板 Blueprint（mode="team"）。"""
    roles = _t04_roles()
    return AgentBlueprint(
        name="氢能获客 Agent 团队（T04）",
        goal=(
            "氢璞创能·氢能产业商机与精准获客：6 角色 DAG 协作团队"
            "（Planner 拆解 → Scout 扫描 → Matcher 匹配 → Strategist 策略 → "
            "Reviewer 审核 → Verifier 验证），含 L6 人工审批门与驳回回炉边。"
            "输入一个获客目标（行业/区域），产出优先级线索清单、产品匹配方案、"
            "获客策略与触达话术，并沉淀可复用获客 Skill。"
        ),
        audience="氢能企业销售/市场团队、赛道评审",
        mode="team",
        role_profiles=roles,
        metadata={
            "template_key": T04_TEMPLATE_KEY,
            "tags": ["氢能", "精准获客", "IGNITE·T04", "DAG 团队", "人工审批门"],
            "cover": {
                "icon": "⚡",
                "accent": "#0E7A5F",
                "tagline": "6 角色 DAG · 氢能产业商机扫描到获客策略一站式产出",
            },
            "demo_prompt": "为京津冀地区有减碳压力的钢铁/物流企业做一轮商机扫描，并产出可执行的获客策略",
            "team_dag": _t04_team_dag(),
        },
    )


def ensure_builtin_templates(service: CatalogService) -> List[str]:
    """把内置模板幂等种子进 catalog 数据目录，返回本次新写入的 app_id 列表。"""
    seeded: List[str] = []
    try:
        service.get_app(T04_APP_ID)
        return seeded  # 已存在且快照完好 —— 幂等跳过
    except (CatalogAppNotFound, CatalogSnapshotMissing):
        pass
    service.register_published_app(
        RegisterPublishedAppRequest(
            blueprint=build_t04_hydrogen_team_blueprint(),
            author="Xueban 模板库",
            kit_type="custom",
            app_id=T04_APP_ID,
        )
    )
    seeded.append(T04_APP_ID)
    return seeded
