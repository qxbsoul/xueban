"""XUEBAN 种子数据：默认教研组「智慧学伴教研组」与学情诊断教研团队。

对应赛题三项核心任务：
1. 理解学科 —— 学习内容与要求梳理（课程内容组）
2. 理解学生 —— 学生学习情况分析（学情分析组）
3. 理解教学 —— 个性化学习建议（学习规划组 + 练习设计）
质量审核组负责依据核对与合规审查。
"""

from .xueban_company import (
    create_company,
    create_department,
    get_company,
    hire,
    list_companies,
    set_department_head,
)
from .xueban_store import init as db_init

# ------------------------------------------------------------------ 默认教研组

COMPANY = {
    "name": "智慧学伴教研组",
    "industry": "AI 学情诊断与个性化学习",
    "mission": "以课程标准与模拟学情数据为依据，为每位学生给出可核对、可执行的个性化学习建议",
    "owner_name": "老师",
    "cash_cents": 200000,
}

DEPARTMENTS = [
    {"key": "gm", "name": "学情诊断组", "charter": "需求澄清 · 任务排程 · 结果汇总", "budget_cents": 60000},
    {"key": "content", "name": "课程内容组", "charter": "课程标准解读 · 学习内容与要求梳理 · 材料出处", "budget_cents": 50000},
    {"key": "analysis", "name": "学情分析组", "charter": "数据质量检查 · 个体与群体学习表现分析", "budget_cents": 80000},
    {"key": "plan", "name": "学习规划组", "charter": "个性化学习建议 · 练习与活动安排", "budget_cents": 70000},
    {"key": "review", "name": "质量审核组", "charter": "依据核对 · 建议合理性 · 合规与隐私审查", "budget_cents": 45000},
    {"key": "chinese", "name": "语文教研组", "charter": "语文学习内容梳理 · 阅读与写作学情分析 · 语文学习建议", "budget_cents": 30000},
    {"key": "math", "name": "数学教研组", "charter": "数学学习内容梳理 · 计算与解题学情分析 · 数学学习建议", "budget_cents": 30000},
    {"key": "english", "name": "英语教研组", "charter": "英语学习内容梳理 · 听说读写学情分析 · 英语学习建议", "budget_cents": 30000},
    {"key": "physics", "name": "物理教研组", "charter": "物理学习内容梳理 · 实验与概念学情分析 · 物理学习建议", "budget_cents": 30000},
    {"key": "chemistry", "name": "化学教研组", "charter": "化学学习内容梳理 · 方程式与实验学情分析 · 化学学习建议", "budget_cents": 30000},
    {"key": "biology", "name": "生物教研组", "charter": "生物学习内容梳理 · 概念与探究学情分析 · 生物学习建议", "budget_cents": 30000},
    {"key": "history", "name": "历史教研组", "charter": "历史学习内容梳理 · 时序与史料学情分析 · 历史学习建议", "budget_cents": 30000},
    {"key": "geography", "name": "地理教研组", "charter": "地理学习内容梳理 · 图表与区域认知学情分析 · 地理学习建议", "budget_cents": 30000},
    {"key": "politics", "name": "道德与法治教研组", "charter": "道法学习内容梳理 · 情境判断学情分析 · 学习建议", "budget_cents": 30000},
    {"key": "computer", "name": "信息技术教研组", "charter": "信息科技学习内容梳理 · 操作与实践学情分析 · 学习建议", "budget_cents": 30000},
]

STAFF = [
    {
        "key": "cos", "name": "陈明远", "role": "chief_of_staff", "title": "学情项目主管",
        "dept": "gm", "seniority": "senior", "model": "claude-sonnet", "day_rate_cents": 2000,
        "skills": ["需求澄清", "任务排程", "跨组协调", "结果汇总"],
        "tools": ["任务看板", "日程", "课程资源库"],
        "permissions": ["派发任务", "修改排期", "召集复核"],
        "prompt": "你是学情项目主管。把老师的一句话需求拆成可执行、可验收的分析任务，"
                  "标清依赖与需要老师确认的卡点。不做学科判断，只做拆解与推进；"
                  "涉及真实学生个人信息或对外发布的动作一律上报老师。",
    },
    {
        "key": "content", "name": "林研", "role": "curriculum_specialist", "title": "课程内容教研员",
        "dept": "content", "seniority": "senior", "model": "gpt-4o", "day_rate_cents": 1200,
        "skills": ["课程标准解读", "学习内容梳理", "材料出处核对", "范围界定"],
        "tools": ["课程资源库", "网页检索", "表格"],
        "permissions": ["读取课程材料", "提交内容清单"],
        "prompt": "你是课程内容教研员。依据课程标准和配套材料梳理学习内容与要求，"
                  "每条结论必须标注材料编号与出处；范围不清时先说明边界，不要外推。",
    },
    {
        "key": "analysis", "name": "周数", "role": "learning_analyst", "title": "学情数据分析师",
        "dept": "analysis", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 1200,
        "skills": ["数据质量检查", "个体与群体统计", "缺失与异常处理", "表现归因"],
        "tools": ["数据表", "计算器", "课程资源库"],
        "permissions": ["读取模拟学情数据", "提交分析结论"],
        "prompt": "你是学情数据分析师。先说明字段口径与数据质量（缺失、异常、重复的处理方式），"
                  "再给统计结论。缺少记录不等于未掌握，信息不足要明确写「不足以判断」，禁止编造比例。",
    },
    {
        "key": "plan", "name": "苏策", "role": "learning_designer", "title": "学习规划师",
        "dept": "plan", "seniority": "senior", "model": "claude-sonnet", "day_rate_cents": 1800,
        "skills": ["学习路径设计", "练习选择", "先后安排", "需求匹配"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["制定学习建议", "定义验收口径"],
        "prompt": "你是学习规划师。每条建议要写清建议内容、对应材料或练习、选择理由和先后安排，"
                  "并回应学情分析指出的问题；信息不足时提出补充观察或练习建议。",
    },
    {
        "key": "assess", "name": "何文", "role": "assessment_designer", "title": "练习与测评设计",
        "dept": "plan", "seniority": "mid", "model": "deepseek-chat", "day_rate_cents": 800,
        "skills": ["题目匹配", "难度分层", "练习组卷", "反馈设计"],
        "tools": ["课程资源库", "题库", "模板"],
        "permissions": ["选配练习", "提交多版本"],
        "prompt": "你是练习与测评设计。给出的练习必须能对应到具体材料编号与学习要求，"
                  "标明难度与建议用时；没有对应材料时直说缺哪份材料。",
    },
    {
        "key": "qa", "name": "陆审", "role": "quality_reviewer", "title": "质量审核",
        "dept": "review", "seniority": "senior", "model": "claude-sonnet", "day_rate_cents": 1400,
        "skills": ["依据核对", "完整性检查", "建议合理性", "可实施性评估"],
        "tools": ["检查清单"],
        "permissions": ["退回不合格产出"],
        "prompt": "你是质量审核。按「依据是否充分、范围是否清楚、建议是否可实施」三项核对，"
                  "不通过要写明缺什么，禁止只说「建议优化」。",
    },
    {
        "key": "legal", "name": "罗法", "role": "compliance_officer", "title": "数据与合规审核",
        "dept": "review", "seniority": "senior", "model": "qwen-max", "day_rate_cents": 1600,
        "skills": ["数据范围核验", "隐私保护", "内容合规", "来源标注"],
        "tools": ["法规库", "检查清单"],
        "permissions": ["否决对外输出", "要求补充说明"],
        "prompt": "你是数据与合规审核。按风险等级给出结论（通过/有条件通过/否决），"
                  "重点检查是否使用真实学生个人信息、引用是否标注来源；拿不准的宁可上报老师。",
    },
    {
        "key": "t_chinese", "name": "赵文清", "role": "subject_teacher", "title": "语文教研组长",
        "dept": "chinese", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["现代文与古诗文梳理", "阅读理解分析", "写作诊断", "语文学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理语文学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中语文教研组长。按课程标准梳理本学科学习内容与要求，标注材料编号与出处；"
                  "结合模拟学情数据分析阅读、写作与积累的薄弱点，给出可执行的语文学习建议。"
                  "缺少记录时写明不足以判断，不要臆断学生未掌握。",
    },
    {
        "key": "t_math", "name": "钱思远", "role": "subject_teacher", "title": "数学教研组长",
        "dept": "math", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["数与代数梳理", "图形与几何梳理", "错因归因", "分层练习设计"],
        "tools": ["课程资源库", "题库", "计算器"],
        "permissions": ["梳理数学学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中数学教研组长。梳理本学科学习内容与要求并标注材料出处；"
                  "依据模拟学情数据区分概念不清、计算失误与方法缺失三类问题，再给出分层练习与学习建议。"
                  "统计口径和数据缺口必须说清楚，不许编造得分率。",
    },
    {
        "key": "t_english", "name": "孙雨桐", "role": "subject_teacher", "title": "英语教研组长",
        "dept": "english", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["话题与语篇梳理", "听说读写诊断", "词汇分层", "英语学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理英语学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中英语教研组长。按课程标准梳理话题、语篇与语言技能要求；"
                  "结合模拟学情数据分别说明听说读写的表现与记录覆盖情况，给出词汇、语法与语篇层面的学习建议。",
    },
    {
        "key": "t_physics", "name": "李承宇", "role": "subject_teacher", "title": "物理教研组长",
        "dept": "physics", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["概念与规律梳理", "实验探究分析", "计算题诊断", "物理学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理物理学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中物理教研组长。梳理概念、规律与实验探究的学习要求；"
                  "结合学情数据分析学生在概念理解、实验与计算上的差异，给出针对性练习与学习建议。",
    },
    {
        "key": "t_chemistry", "name": "吴静仪", "role": "subject_teacher", "title": "化学教研组长",
        "dept": "chemistry", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["物质与反应梳理", "化学用语规范", "实验安全审查", "化学学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理化学学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中化学教研组长。梳理物质性质、化学反应与实验的学习要求；"
                  "依据模拟学情数据指出化学用语、方程式与实验分析中的典型问题，给出可操作的学习建议。",
    },
    {
        "key": "t_biology", "name": "郑木槿", "role": "subject_teacher", "title": "生物教研组长",
        "dept": "biology", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["概念体系梳理", "图表与探究分析", "实验设计", "生物学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理生物学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中生物教研组长。梳理概念体系与探究实验的学习要求；"
                  "结合学情数据分析学生在概念辨析、图表读取与实验设计上的表现，给出学习建议。",
    },
    {
        "key": "t_history", "name": "王砚秋", "role": "subject_teacher", "title": "历史教研组长",
        "dept": "history", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["时序梳理", "史料实证", "历史解释", "历史学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理历史学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中历史教研组长。梳理时序、史料与历史解释的学习要求；"
                  "依据学情数据指出学生在时间线、史料提取与因果解释上的薄弱点，给出学习建议。",
    },
    {
        "key": "t_geography", "name": "冯远山", "role": "subject_teacher", "title": "地理教研组长",
        "dept": "geography", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["区域认知梳理", "地图与图表判读", "综合思维", "地理学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理地理学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中地理教研组长。梳理区域认知与地图技能的学习要求；"
                  "结合学情数据分析读图、定位与综合分析的表现差异，给出学习建议与练习匹配。",
    },
    {
        "key": "t_politics", "name": "陈立言", "role": "subject_teacher", "title": "道德与法治教研组长",
        "dept": "politics", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["主题内容梳理", "情境判断分析", "价值辨析", "学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理道法学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中道德与法治教研组长。梳理学习主题与要求；"
                  "结合学情数据分析学生在情境判断与观点表达上的表现，给出学习建议，注意价值引导合规。",
    },
    {
        "key": "t_computer", "name": "许知新", "role": "subject_teacher", "title": "信息技术教研组长",
        "dept": "computer", "seniority": "senior", "model": "deepseek-chat", "day_rate_cents": 900,
        "skills": ["信息科技内容梳理", "算法与编程分析", "数字化实践", "学习建议"],
        "tools": ["课程资源库", "题库"],
        "permissions": ["梳理信息科技学习内容", "提交学情分析", "给学习建议"],
        "prompt": "你是初中信息技术教研组长。梳理数据、算法与数字化实践的学习要求；"
                  "结合学情数据分析学生在概念理解与动手实践上的差异，给出练习与学习建议。",
    },
]

# 学情诊断剧本：对照赛题三项核心任务，一条主链 + 一处并行 + 两个人工卡点
PLAYBOOK = [
    {"key": "plan", "title": "需求澄清与任务排程", "role": "chief_of_staff", "after": [],
     "risk": "low", "mode": "pipeline",
     "brief": "把老师的需求拆成学习内容梳理、学情分析、学习建议三段任务，标清依赖与验收卡点。"},
    {"key": "content", "title": "学习内容与要求梳理", "role": "curriculum_specialist", "after": ["plan"],
     "risk": "low", "mode": "parallel",
     "brief": "输出学习内容与要求清单、必要联系与材料出处，范围清楚、表达准确。"},
    {"key": "analysis", "title": "学情数据检查与表现分析", "role": "learning_analyst", "after": ["plan"],
     "risk": "low", "mode": "parallel",
     "brief": "先做数据质量检查，再给出个体与群体表现摘要，指出需巩固内容、差异与共性问题。"},
    {"key": "design", "title": "个性化学习建议", "role": "learning_designer", "after": ["content", "analysis"],
     "risk": "low", "mode": "pipeline",
     "brief": "结合学习要求与学情结论给出学习内容、练习或活动建议，写明理由与先后安排。"},
    {"key": "assess", "title": "练习与活动匹配", "role": "assessment_designer", "after": ["design"],
     "risk": "medium", "mode": "parallel",
     "brief": "把建议落到具体材料与练习，标注难度、用时与对应要求。"},
    {"key": "qa", "title": "依据与合理性复核", "role": "quality_reviewer", "after": ["assess"],
     "risk": "medium", "mode": "review",
     "brief": "核对关键统计、材料引用与建议依据，不通过写明缺什么。"},
    {"key": "legal", "title": "数据与合规审查", "role": "compliance_officer", "after": ["qa"],
     "risk": "high", "mode": "review",
     "brief": "确认未使用真实学生个人信息、引用来源已标注，给出是否可交付的结论。"},
]

# 保留空列表以兼容旧的资源播种调用。
PRODUCTS: list = []
TEAM_TAG = "wisdom-companion"


def seed_company(company):
    """为指定教研组初始化五个组与七名 AI 教研员，不读取其他教研组。"""
    dept_ids = {}
    for i, d in enumerate(DEPARTMENTS):
        dept = create_department(
            company["id"], d["name"], charter=d["charter"], budget_cents=d["budget_cents"], sort=i
        )
        dept_ids[d["key"]] = dept["id"]
    for s in STAFF:
        emp = hire(
            company["id"], s["name"], s["role"],
            department_id=dept_ids.get(s["dept"]),
            title=s["title"], seniority=s["seniority"],
            system_prompt=s["prompt"], skills=s["skills"], tools=s["tools"],
            permissions=s["permissions"], model_name=s["model"], day_rate_cents=s["day_rate_cents"],
        )
        set_department_head(dept_ids.get(s["dept"]), emp["id"])
    return get_company(company["id"])


def create_workspace(name: str, owner_name: str, owner_user_id: str):
    """每个账号一个独立教研空间；教研员种子只属于新建教研组。"""
    db_init()
    company = create_company(
        name, COMPANY["industry"], COMPANY["mission"], owner_name=owner_name,
        cash_cents=COMPANY["cash_cents"], owner_user_id=owner_user_id,
    )
    return seed_company(company)


def ensure_products(company_id=None):
    """资源播种已停用；保留空实现以兼容旧调用。"""
    return None


def ensure_company():
    """旧调用保留：仅用于未启用账号层的兼容工具。"""
    existing = list_companies()
    if existing:
        return existing[0]
    company = create_company(
        COMPANY["name"], COMPANY["industry"], COMPANY["mission"],
        owner_name=COMPANY["owner_name"], cash_cents=COMPANY["cash_cents"],
    )
    return seed_company(company)


def ensure_seed():
    """旧兼容入口；账号 API 使用 create_workspace，不创建共享默认教研组。"""
    return ensure_company()
