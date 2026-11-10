"""内置秘书播种（2026-11-06，GrokBot「主Bot派活」范式；显示名同日定稿「教学主管」）。

产品口径：
  - 用户第一次进产品就有一个贴身主控 Agent 可对话：接需求 → 拆活 →
    推荐招募教研员/组建团队 → 派活 → 汇报。
  - 它的第一句话是常规欢迎语（SECRETARY_GREETING），播种时直接写进会话
    日志，用户点进来就有一条欢迎消息，而不是空会话。

幂等设计：
  - Agent 以固定 id ``agent-shadow-secretary`` 存于 .xueban/agents/，
    已存在（含被用户改过名/改过 soul）则**完全不碰**——用户编辑后重启不回滚。
  - 记录带 ``source: "secretary"`` 标记（AgentRecord.source union 前端扩展），
    前端据此渲染常驻入口与去重。
  - DM 会话在用户首次 resolve 时由 groups.resolve_dm_conversation 预置问候，
    这里不预建会话（避免在用户没进过产品时就占一条侧栏会话行）。

失败策略：任何异常只告警不阻塞启动（与 xueban_seed 同款纪律）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("xueban.secretary")

# 固定 id：幂等锚点（不随改名变动，改 id 会孤儿化存量会话）。
# alllowlist 正则 ^[a-zA-Z0-9_-]+$ 兼容。
SECRETARY_AGENT_ID = "agent-shadow-secretary"
SECRETARY_NAME = "教学主管"
# 发给用户的开场白（2026-11-06：「Hello World!」→ 常规欢迎语）。
SECRETARY_GREETING = "你好，我是你的教学主管，很高兴为你服务。有什么想做的事，尽管吩咐。"

# soul 按 GrokBot「主Bot派活」精神写：接需求 → 拆活 → 招人/组队 → 派活 → 汇报。
SECRETARY_SOUL = (
    "你是「教学主管」，Xueban 用户的贴身秘书兼首席幕僚。你的职责："
    "1) 倾听老师的需求，把模糊的想法拆解成可执行的任务；"
    "2) 评估任务需要什么能力，建议老师招募哪些教研员（Agent）或组建什么团队，"
    "并协助完成招募与编队；"
    "3) 把拆好的活派给合适的教研员或团队，跟进进度；"
    "4) 向老师汇报结果，沉淀经验。"
    "你的风格：简洁、可靠、有分寸。老师不在场时守好大本营，老师发话时使命必达。"
)

_AGENTS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "agents"


def _agents_dir() -> Path:
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    return _AGENTS_DIR


def ensure_secretary() -> Dict[str, Any] | None:
    """幂等播种内置秘书。已存在则返回 None；新建则返回记录 dict。

    注意：与 agents.py 的 _save_agent 不同，这里不做 path-traversal 校验
    （固定常量 id，无用户输入），保持模块零依赖（不 import fastapi）。
    """
    target = _agents_dir() / f"{SECRETARY_AGENT_ID}.json"
    if target.exists():
        return None  # 幂等：用户可能已改名/改 soul，绝不覆盖

    now = datetime.now(timezone.utc).isoformat()
    record: Dict[str, Any] = {
        "agent_id": SECRETARY_AGENT_ID,
        "name": SECRETARY_NAME,
        "soul": SECRETARY_SOUL,
        "workspace_id": None,  # 全工作区可见（listAgents 不带过滤时返回）
        "blueprint": {
            "name": SECRETARY_NAME,
            "goal": SECRETARY_SOUL,
            "role_profiles": [],
            "tool_policies": [],
            "metadata": {"source": "secretary"},
        },
        "status": "idle",
        "source": "secretary",
        "created_at": now,
        "avatar_color": None,
        "claw_id": None,
        "avatar_style": "lorelei",
        # 固定 seed：头像稳定不随 hash 抖动（2026-11-06 换默认头像：
        # dylan 大汉款 → lorelei·mishu 暖笑亲和款）。
        "avatar_seed": "mishu",
        "avatar_image": None,
    }
    target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record
