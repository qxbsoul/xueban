"""Kit Defaults Registry — Story 10.5

KitDefinition 是每个 Epic 10 Kit 的完整注册合同，包含：
  - 完整的默认 AgentBlueprint
  - 默认 PolicyProfile（权限规则）
  - 默认 EvalProfile（评测标准）
  - 前端元数据（display_name / description / category / icon / supported_modes）

KitRegistry 是模块级单例，通过 discover_and_register_kits() 自动扫描
kits/ 目录中的 *_kit.py 文件并导入，触发各 Kit 模块的 REGISTRY.register() 调用。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from xueban.runtime.contracts_builder import AgentBlueprint, EvalProfile
from xueban.runtime.errors import XuebanError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SceneDefinition — Scene Tree 默认角色结构
# ---------------------------------------------------------------------------


class SceneRoleNode(BaseModel):
    """Scene Tree 中的单个角色节点（递归嵌套）。"""

    role_id: str
    role_name: str
    role_type: Literal["boss", "worker", "solo"] = "solo"
    description: str = ""
    sub_roles: List["SceneRoleNode"] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# 允许自引用（递归嵌套）
SceneRoleNode.model_rebuild()


class SceneDefinition(BaseModel):
    """Scene Tree 默认角色层级结构。

    root_roles 是顶层角色列表（Boss 或 Solo 角色），
    每个角色可嵌套 sub_roles（Worker 层级）。
    """

    scene_id: str = ""
    display_name: str = ""
    root_roles: List[SceneRoleNode] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# PolicyProfile — 默认权限规则
# ---------------------------------------------------------------------------


class PolicyProfile(BaseModel):
    """Kit 级别的默认 Policy Profile（权限规则集）。

    与 contracts_builder.ToolPolicy 配合使用，定义 Kit 层面的
    默认工具访问策略与角色间交互权限。
    """

    profile_id: str = ""
    display_name: str = ""
    default_tool_permission: Literal["allow", "ask", "deny"] = "ask"
    allow_tool_ids: List[str] = Field(default_factory=list)
    deny_tool_ids: List[str] = Field(default_factory=list)
    require_approval_for: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# KitValidationError
# ---------------------------------------------------------------------------


class KitValidationError(XuebanError):
    """Kit 注册失败 — 缺少必要字段或数据结构不合法。

    code = KIT_VALIDATION_FAILED
    """

    code = "KIT_VALIDATION_FAILED"

    def __init__(self, kit_id: str, missing_field: str, extra_detail: str = "") -> None:
        msg = (
            f"Kit '{kit_id}' is missing required field: {missing_field}.\n"
            f"Cannot register kit without all required default configurations."
        )
        if extra_detail:
            msg += f"\nDetail: {extra_detail}"
        super().__init__(
            msg,
            details={"kit_id": kit_id, "missing_field": missing_field},
        )


# ---------------------------------------------------------------------------
# KitDefinition
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = frozenset({"research", "knowledge", "review", "persona", "custom"})
_VALID_RESULT_VIEWS = frozenset(
    {"scene_report", "agent_dm_with_state", "approval_inbox", "research_report"}
)
_VALID_MODES = frozenset({"goal", "scene", "graph"})


class KitDefinition(BaseModel):
    """Epic 10 Kit 的完整注册合同。

    所有 11 个字段都是必填项。model_validator 负责校验必填的
    default_blueprint / default_policy_profile / default_eval_profile，
    确保注册前即阻断不完整的 Kit。
    """

    # 身份信息
    kit_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1, max_length=2000)

    # 分类与模式
    category: str = Field(..., description="research / knowledge / review / persona / custom")
    supported_modes: List[str] = Field(
        ..., min_length=1, description="goal / scene / graph 的子集"
    )

    # 默认配置（三件套）
    default_blueprint: AgentBlueprint
    default_scene: SceneDefinition = Field(default_factory=SceneDefinition)
    default_policy_profile: PolicyProfile
    default_eval_profile: EvalProfile

    # 展示信息
    default_result_view: str = Field(
        ...,
        description="scene_report / agent_dm_with_state / approval_inbox / research_report",
    )
    recommended_inputs: List[str] = Field(
        ..., description="用户在 Goal Mode 向导中看到的字段列表"
    )
    icon: str = Field(..., min_length=1, description="emoji 或 icon_id")

    @model_validator(mode="after")
    def validate_kit_completeness(self) -> "KitDefinition":
        # 分类校验
        if self.category not in _VALID_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(_VALID_CATEGORIES)}, got {self.category!r}"
            )

        # 模式校验
        invalid_modes = [m for m in self.supported_modes if m not in _VALID_MODES]
        if invalid_modes:
            raise ValueError(
                f"supported_modes contains invalid values: {invalid_modes}. "
                f"Valid: {sorted(_VALID_MODES)}"
            )

        # result_view 校验
        if self.default_result_view not in _VALID_RESULT_VIEWS:
            raise ValueError(
                f"default_result_view must be one of {sorted(_VALID_RESULT_VIEWS)}, "
                f"got {self.default_result_view!r}"
            )

        return self

    def metadata_only(self) -> Dict[str, Any]:
        """返回仅含元数据的字典（不含 default_blueprint 等大对象），用于 API 列表响应。"""
        return {
            "kit_id": self.kit_id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "supported_modes": self.supported_modes,
            "default_result_view": self.default_result_view,
            "recommended_inputs": self.recommended_inputs,
            "icon": self.icon,
        }

    def blueprint_summary(self) -> Dict[str, Any]:
        """返回含 blueprint summary 的字典（用于 GET /builder/kits/{kit_id}）。"""
        meta = self.metadata_only()
        meta["default_blueprint_summary"] = {
            "blueprint_id": self.default_blueprint.blueprint_id,
            "name": self.default_blueprint.name,
            "goal": self.default_blueprint.goal,
            "mode": self.default_blueprint.mode,
            "role_count": len(self.default_blueprint.role_profiles),
        }
        meta["default_scene"] = self.default_scene.model_dump(mode="python")
        meta["default_policy_profile"] = self.default_policy_profile.model_dump(mode="python")
        meta["default_eval_profile"] = self.default_eval_profile.model_dump(mode="python")
        return meta


# ---------------------------------------------------------------------------
# KitRegistry
# ---------------------------------------------------------------------------


class KitRegistry:
    """Kit 注册表 — 支持注册、查询、列表、按类别过滤。

    设计为模块级单例 REGISTRY，不需要也不应该在其他地方重复实例化。
    """

    def __init__(self) -> None:
        self._kits: Dict[str, KitDefinition] = {}

    def register(self, kit: KitDefinition) -> None:
        """注册一个 Kit。校验完整性，否则抛 KitValidationError。

        注意：KitDefinition.model_validator 已经在 Pydantic 层完成了结构校验。
        此处额外检查「是否真正提供了有意义的 default_eval_profile」（AC1 语义）。
        """
        if kit.kit_id in self._kits:
            logger.warning("KitRegistry: overwriting existing kit_id=%r", kit.kit_id)

        # 语义完整性检查：不允许注册空的 eval_profile（无任何 criteria 且未启用 smoke_eval）
        ep = kit.default_eval_profile
        if not ep.smoke_eval_enabled and not ep.eval_criteria and not ep.regression_gate:
            raise KitValidationError(
                kit_id=kit.kit_id,
                missing_field="default_eval_profile",
                extra_detail=(
                    "default_eval_profile must have at least one of: "
                    "smoke_eval_enabled=True, eval_criteria (non-empty), or regression_gate=True"
                ),
            )

        self._kits[kit.kit_id] = kit
        logger.info("KitRegistry: registered kit_id=%r", kit.kit_id)

    def get(self, kit_id: str) -> Optional[KitDefinition]:
        """按 kit_id 查询，不存在返回 None。"""
        return self._kits.get(kit_id)

    def list_kits(self, category: Optional[str] = None) -> List[KitDefinition]:
        """返回所有已注册 Kit，可按 category 过滤。"""
        kits = list(self._kits.values())
        if category is not None:
            kits = [k for k in kits if k.category == category]
        return kits

    def get_default_blueprint(self, kit_id: str) -> AgentBlueprint:
        """获取 Kit 的默认 AgentBlueprint，不存在则抛 KeyError。"""
        kit = self._kits.get(kit_id)
        if kit is None:
            raise KeyError(f"Kit not found: {kit_id!r}")
        return kit.default_blueprint

    def get_default_eval_profile(self, kit_id: str) -> EvalProfile:
        """获取 Kit 的默认 EvalProfile，不存在则抛 KeyError。"""
        kit = self._kits.get(kit_id)
        if kit is None:
            raise KeyError(f"Kit not found: {kit_id!r}")
        return kit.default_eval_profile


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

REGISTRY = KitRegistry()


# ---------------------------------------------------------------------------
# discover_and_register_kits — importlib 动态扫描
# ---------------------------------------------------------------------------


def discover_and_register_kits() -> int:
    """自动扫描 xueban/runtime/kits/ 目录中的 *_kit.py 文件并导入。

    各 Kit 模块在模块级调用 REGISTRY.register(KIT_DEFINITION)，
    导入时自动触发注册。

    Returns:
        成功导入的 kit 模块数量（不等于注册成功数，注册失败会有日志）。
    """
    kits_pkg_path = Path(__file__).parent
    imported = 0

    for module_info in pkgutil.iter_modules([str(kits_pkg_path)]):
        name = module_info.name
        if not name.endswith("_kit"):
            continue

        module_fqn = f"xueban.runtime.kits.{name}"
        try:
            importlib.import_module(module_fqn)
            imported += 1
            logger.debug("discover_and_register_kits: imported %s", module_fqn)
        except KitValidationError as exc:
            logger.error(
                "discover_and_register_kits: KitValidationError in %s — %s",
                module_fqn,
                exc.message,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "discover_and_register_kits: failed to import %s — %s: %s",
                module_fqn,
                type(exc).__name__,
                exc,
            )

    if imported == 0:
        logger.info(
            "discover_and_register_kits: no *_kit.py modules found in %s "
            "(this is expected before Epic 10.1-10.4 kits are implemented)",
            kits_pkg_path,
        )
    else:
        logger.info(
            "discover_and_register_kits: imported %d kit module(s), "
            "%d kit(s) registered",
            imported,
            len(REGISTRY.list_kits()),
        )

    return imported
