"""xueban.runtime.kits — Kit Defaults Registry（Story 10.5）

公共 API：
  - KitDefinition       — Kit 完整注册合同（含 default_blueprint / policy / eval）
  - KitRegistry         — 注册表类（register / get / list_kits / ...）
  - KitValidationError  — 缺少必要字段时的校验异常
  - PolicyProfile       — Kit 级别默认权限规则
  - SceneDefinition     — Scene Tree 默认角色层级
  - REGISTRY            — 模块级单例（所有 Kit 向此注册）
  - discover_and_register_kits — 启动时自动扫描 *_kit.py 并导入

Story 10.1 Research Kit 导出（供 builder_service.py 及 API 路由直接引用）：
  - RESEARCH_KIT_DEFINITION  — Research Kit 注册条目（KitDefinition 对象）
  - ResearchGoalInputs       — Research Kit 向导输入 Pydantic 模型
  - create_research_blueprint — 从向导输入生成 AgentBlueprint 的工厂函数

Story 10.3 Review & Approval Kit 导出：
  - REVIEW_APPROVAL_KIT_DEFINITION  — Review & Approval Kit 注册条目
  - ReviewApprovalGoalInputs        — 向导输入 Pydantic 模型
  - create_review_approval_blueprint — 从向导输入生成 AgentBlueprint 的工厂函数
  - REVIEW_APPROVAL_SMOKE_CASES     — 3 路径 Smoke Run case 定义

Story 10.4 Persona / NPC Kit 导出：
  - PERSONA_NPC_KIT_DEFINITION  — Persona / NPC Kit 注册条目
  - PersonaNPCGoalInputs        — 向导输入 Pydantic 模型（5 字段）
  - create_persona_npc_blueprint — 从向导输入生成 AgentBlueprint 的工厂函数
  - PERSONA_NPC_SMOKE_CASES     — 3 轮连续对话 Smoke Run case 定义
  - PERSONA_NPC_EVAL_PROFILE    — EvalProfile 指标字典
  - RelationshipHook            — 关系演化钩子数据结构
  - MEMORY_RETENTION_PRESETS    — 三档记忆保留预设值字典
"""

from xueban.runtime.kits.registry import (
    KitDefinition,
    KitRegistry,
    KitValidationError,
    PolicyProfile,
    SceneDefinition,
    SceneRoleNode,
    REGISTRY,
    discover_and_register_kits,
)

# Story 10.1 Research Kit 导出
# 注意：导入 research_kit 模块会触发 REGISTRY.register(RESEARCH_KIT_DEFINITION_OBJ)
from xueban.runtime.kits.research_kit import (
    RESEARCH_KIT_DEFINITION,
    ResearchGoalInputs,
    create_research_blueprint,
)

# Story 10.3 Review & Approval Kit 导出
# 注意：导入 review_approval_kit 模块会触发 REGISTRY.register(REVIEW_APPROVAL_KIT_DEFINITION)
from xueban.runtime.kits.review_approval_kit import (
    REVIEW_APPROVAL_KIT_DEFINITION,
    ReviewApprovalGoalInputs,
    create_review_approval_blueprint,
    REVIEW_APPROVAL_SMOKE_CASES,
)

# Story 10.2 Knowledge Assistant Kit 导出
# 注意：导入会触发 REGISTRY.register(KNOWLEDGE_ASSISTANT_KIT_DEFINITION)
from xueban.runtime.kits.knowledge_assistant_kit import (
    KNOWLEDGE_ASSISTANT_KIT_DEFINITION,
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
    KNOWLEDGE_ASSISTANT_SMOKE_CASES,
)

# Story 10.4 Persona / NPC Kit 导出
# 注意：导入 persona_npc_kit 模块会触发 REGISTRY.register(PERSONA_NPC_KIT_DEFINITION)
from xueban.runtime.kits.persona_npc_kit import (
    PERSONA_NPC_KIT_DEFINITION,
    PersonaNPCGoalInputs,
    RelationshipHook,
    MEMORY_RETENTION_PRESETS,
    PERSONA_NPC_SMOKE_CASES,
    PERSONA_NPC_EVAL_PROFILE,
    create_persona_npc_blueprint,
)

__all__ = [
    # Story 10.5 — registry
    "KitDefinition",
    "KitRegistry",
    "KitValidationError",
    "PolicyProfile",
    "SceneDefinition",
    "SceneRoleNode",
    "REGISTRY",
    "discover_and_register_kits",
    # Story 10.1 — Research Kit
    "RESEARCH_KIT_DEFINITION",
    "ResearchGoalInputs",
    "create_research_blueprint",
    # Story 10.3 — Review & Approval Kit
    "REVIEW_APPROVAL_KIT_DEFINITION",
    "ReviewApprovalGoalInputs",
    "create_review_approval_blueprint",
    "REVIEW_APPROVAL_SMOKE_CASES",
    # Story 10.2 — Knowledge Assistant Kit
    "KNOWLEDGE_ASSISTANT_KIT_DEFINITION",
    "KnowledgeAssistantGoalInputs",
    "create_knowledge_assistant_blueprint",
    "KNOWLEDGE_ASSISTANT_SMOKE_CASES",
    # Story 10.4 — Persona / NPC Kit
    "PERSONA_NPC_KIT_DEFINITION",
    "PersonaNPCGoalInputs",
    "RelationshipHook",
    "MEMORY_RETENTION_PRESETS",
    "PERSONA_NPC_SMOKE_CASES",
    "PERSONA_NPC_EVAL_PROFILE",
    "create_persona_npc_blueprint",
]
