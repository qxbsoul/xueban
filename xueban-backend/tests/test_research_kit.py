"""tests/test_research_kit.py — Story 10.1 AC6

覆盖 ResearchGoalInputs 校验逻辑 + create_research_blueprint() 产出结构正确性。

测试用例：
  1. max_search_rounds 超出范围（>5 / <1）→ 校验失败
  2. output_format 非法值 → 校验失败
  3. freshness 非法值 → 校验失败
  4. 合法输入 → create_research_blueprint() 返回 4 个 RoleProfile
  5. citation_required=True → Researcher 角色 metadata.citation_required=True
  6. citation_required=False → Researcher 角色 metadata.citation_required=False
  7. Planner 角色 can_spawn_tasks=True
  8. 角色顺序：Planner → Researcher → Summarizer → Report Writer
  9. Blueprint 包含正确的 mode='team' 和 4 角色
  10. RESEARCH_KIT_DEFINITION 已在 REGISTRY 中注册
"""
import pytest
from pydantic import ValidationError

from xueban.runtime.kits.research_kit import (
    ResearchGoalInputs,
    create_research_blueprint,
    RESEARCH_KIT_DEFINITION,
)
from xueban.runtime.kits.registry import REGISTRY


# ---------------------------------------------------------------------------
# ResearchGoalInputs 校验测试
# ---------------------------------------------------------------------------


class TestResearchGoalInputsValidation:
    """ResearchGoalInputs Pydantic v2 校验测试。"""

    def test_max_search_rounds_too_high_raises(self):
        """max_search_rounds > 5 时应抛出 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            ResearchGoalInputs(
                research_topic="测试主题",
                max_search_rounds=6,
            )
        errors = exc_info.value.errors()
        field_errors = [e for e in errors if "max_search_rounds" in str(e.get("loc", ""))]
        assert field_errors, f"期望 max_search_rounds 校验错误，实际 errors={errors}"

    def test_max_search_rounds_too_low_raises(self):
        """max_search_rounds < 1 时应抛出 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            ResearchGoalInputs(
                research_topic="测试主题",
                max_search_rounds=0,
            )
        errors = exc_info.value.errors()
        field_errors = [e for e in errors if "max_search_rounds" in str(e.get("loc", ""))]
        assert field_errors, f"期望 max_search_rounds 校验错误，实际 errors={errors}"

    def test_max_search_rounds_boundary_1_valid(self):
        """max_search_rounds=1 是合法的边界值。"""
        inputs = ResearchGoalInputs(research_topic="测试主题", max_search_rounds=1)
        assert inputs.max_search_rounds == 1

    def test_max_search_rounds_boundary_5_valid(self):
        """max_search_rounds=5 是合法的边界值。"""
        inputs = ResearchGoalInputs(research_topic="测试主题", max_search_rounds=5)
        assert inputs.max_search_rounds == 5

    def test_invalid_output_format_raises(self):
        """非法 output_format 应抛出 ValidationError。"""
        with pytest.raises(ValidationError):
            ResearchGoalInputs(
                research_topic="测试主题",
                output_format="invalid_format",
            )

    def test_valid_output_formats(self):
        """所有合法 output_format 值应通过校验。"""
        for fmt in ("answer", "report", "structured_outline"):
            inputs = ResearchGoalInputs(research_topic="测试主题", output_format=fmt)
            assert inputs.output_format == fmt

    def test_invalid_freshness_raises(self):
        """非法 freshness 应抛出 ValidationError。"""
        with pytest.raises(ValidationError):
            ResearchGoalInputs(
                research_topic="测试主题",
                freshness="yesterday",
            )

    def test_valid_freshness_values(self):
        """所有合法 freshness 值应通过校验。"""
        for f in ("latest", "within_month", "any"):
            inputs = ResearchGoalInputs(research_topic="测试主题", freshness=f)
            assert inputs.freshness == f

    def test_empty_research_topic_raises(self):
        """空 research_topic 应抛出 ValidationError。"""
        with pytest.raises(ValidationError):
            ResearchGoalInputs(research_topic="")

    def test_default_values(self):
        """验证各字段默认值符合规范。"""
        inputs = ResearchGoalInputs(research_topic="测试主题")
        assert inputs.output_format == "report"
        assert inputs.freshness == "any"
        assert inputs.citation_required is True
        assert inputs.max_search_rounds == 2

    def test_citation_required_true_default(self):
        """citation_required 默认值应为 True。"""
        inputs = ResearchGoalInputs(research_topic="测试主题")
        assert inputs.citation_required is True

    def test_citation_required_false(self):
        """citation_required=False 应通过校验。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            citation_required=False,
        )
        assert inputs.citation_required is False


# ---------------------------------------------------------------------------
# create_research_blueprint() 产出结构测试
# ---------------------------------------------------------------------------


class TestCreateResearchBlueprint:
    """create_research_blueprint() 返回的 AgentBlueprint 结构正确性测试。"""

    @pytest.fixture
    def default_inputs(self) -> ResearchGoalInputs:
        return ResearchGoalInputs(
            research_topic="2025 年大模型推理优化方法",
            output_format="report",
            freshness="any",
            citation_required=True,
            max_search_rounds=2,
        )

    def test_blueprint_contains_4_role_profiles(self, default_inputs):
        """create_research_blueprint() 应返回包含 4 个 RoleProfile 的 Blueprint。"""
        blueprint = create_research_blueprint(default_inputs)
        assert len(blueprint.role_profiles) == 4, (
            f"期望 4 个 RoleProfile，实际 {len(blueprint.role_profiles)}"
        )

    def test_role_names_are_correct(self, default_inputs):
        """4 个角色名称应为 Planner / Researcher / Summarizer / Report Writer。"""
        blueprint = create_research_blueprint(default_inputs)
        names = [r.name for r in blueprint.role_profiles]
        assert "Planner" in names, f"缺少 Planner 角色，实际角色：{names}"
        assert "Researcher" in names, f"缺少 Researcher 角色，实际角色：{names}"
        assert "Summarizer" in names, f"缺少 Summarizer 角色，实际角色：{names}"
        assert "Report Writer" in names, f"缺少 Report Writer 角色，实际角色：{names}"

    def test_role_order_planner_first(self, default_inputs):
        """Planner 应在角色列表的第一位（顺序依赖：Planner → Researcher → ...）。"""
        blueprint = create_research_blueprint(default_inputs)
        assert blueprint.role_profiles[0].name == "Planner"

    def test_role_order_report_writer_last(self, default_inputs):
        """Report Writer 应在角色列表的最后一位。"""
        blueprint = create_research_blueprint(default_inputs)
        assert blueprint.role_profiles[-1].name == "Report Writer"

    def test_planner_can_spawn_tasks(self, default_inputs):
        """Planner 角色的 can_spawn_tasks 应为 True。"""
        blueprint = create_research_blueprint(default_inputs)
        planner = next(r for r in blueprint.role_profiles if r.name == "Planner")
        assert planner.can_spawn_tasks is True

    def test_citation_required_true_researcher_metadata(self):
        """citation_required=True 时，Researcher 角色 metadata.citation_required=True。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            citation_required=True,
        )
        blueprint = create_research_blueprint(inputs)
        researcher = next(r for r in blueprint.role_profiles if r.name == "Researcher")
        assert researcher.metadata.get("citation_required") is True, (
            f"期望 Researcher.metadata.citation_required=True，实际 metadata={researcher.metadata}"
        )

    def test_citation_required_false_researcher_metadata(self):
        """citation_required=False 时，Researcher 角色 metadata.citation_required=False。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            citation_required=False,
        )
        blueprint = create_research_blueprint(inputs)
        researcher = next(r for r in blueprint.role_profiles if r.name == "Researcher")
        assert researcher.metadata.get("citation_required") is False, (
            f"期望 Researcher.metadata.citation_required=False，实际 metadata={researcher.metadata}"
        )

    def test_blueprint_mode_is_team(self, default_inputs):
        """Blueprint 的 mode 应为 'team'（多角色协作）。"""
        blueprint = create_research_blueprint(default_inputs)
        assert blueprint.mode == "team"

    def test_blueprint_name_contains_topic(self, default_inputs):
        """Blueprint 名称应包含研究主题（截断到前 60 字符）。"""
        blueprint = create_research_blueprint(default_inputs)
        assert default_inputs.research_topic[:30] in blueprint.name

    def test_researcher_has_search_tools(self, default_inputs):
        """Researcher 角色应包含搜索相关工具。"""
        blueprint = create_research_blueprint(default_inputs)
        researcher = next(r for r in blueprint.role_profiles if r.name == "Researcher")
        assert "builtin:web_search" in researcher.tools, (
            f"期望 Researcher 有 web_search 工具，实际工具：{researcher.tools}"
        )

    def test_blueprint_metadata_includes_kit_id(self, default_inputs):
        """Blueprint metadata 应包含 kit_id='research_kit'。"""
        blueprint = create_research_blueprint(default_inputs)
        assert blueprint.metadata.get("kit_id") == "research_kit"

    def test_eval_profile_smoke_eval_enabled(self, default_inputs):
        """EvalProfile.smoke_eval_enabled 应为 True。"""
        blueprint = create_research_blueprint(default_inputs)
        assert blueprint.eval_profile.smoke_eval_enabled is True

    def test_eval_criteria_not_empty(self, default_inputs):
        """EvalProfile.eval_criteria 应非空。"""
        blueprint = create_research_blueprint(default_inputs)
        assert len(blueprint.eval_profile.eval_criteria) > 0


# ---------------------------------------------------------------------------
# RESEARCH_KIT_DEFINITION 注册测试
# ---------------------------------------------------------------------------


class TestResearchKitDefinition:
    """RESEARCH_KIT_DEFINITION KitDefinition 对象正确性测试。"""

    def test_kit_id_is_research_kit(self):
        """RESEARCH_KIT_DEFINITION.kit_id 应为 'research_kit'。"""
        assert RESEARCH_KIT_DEFINITION.kit_id == "research_kit"

    def test_supported_modes_contains_required(self):
        """supported_modes 应包含 goal / scene / graph。"""
        modes = RESEARCH_KIT_DEFINITION.supported_modes
        for mode in ("goal", "scene", "graph"):
            assert mode in modes, f"supported_modes 缺少 '{mode}'，实际：{modes}"

    def test_category_is_research(self):
        """category 应为 'research'。"""
        assert RESEARCH_KIT_DEFINITION.category == "research"

    def test_default_result_view(self):
        """default_result_view 应为 'research_report'。"""
        assert RESEARCH_KIT_DEFINITION.default_result_view == "research_report"

    def test_recommended_inputs_complete(self):
        """recommended_inputs 应包含 5 个向导字段。"""
        expected = {
            "research_topic",
            "output_format",
            "freshness",
            "citation_required",
            "max_search_rounds",
        }
        actual = set(RESEARCH_KIT_DEFINITION.recommended_inputs)
        assert expected.issubset(actual), (
            f"recommended_inputs 缺少字段：{expected - actual}"
        )

    def test_registry_contains_research_kit(self):
        """REGISTRY 应包含 'research_kit' 注册条目。"""
        kit = REGISTRY.get("research_kit")
        assert kit is not None, "REGISTRY 中未找到 'research_kit'"
        assert kit.kit_id == "research_kit"

    def test_default_blueprint_has_4_roles(self):
        """KitDefinition.default_blueprint 应包含 4 个 RoleProfile。"""
        assert len(RESEARCH_KIT_DEFINITION.default_blueprint.role_profiles) == 4

    def test_default_eval_profile_has_criteria(self):
        """KitDefinition.default_eval_profile 应有 eval_criteria。"""
        assert len(RESEARCH_KIT_DEFINITION.default_eval_profile.eval_criteria) > 0
