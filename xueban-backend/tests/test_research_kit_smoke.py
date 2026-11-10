"""tests/test_research_kit_smoke.py — Story 10.1 AC6

Smoke Run 最小闭环测试（mock 搜索工具）。

测试用例：
  1. build_smoke_result() 返回的产物包含 5 个字段（todos / progress_log / summary / report / citations）
  2. citation_required=True 时 citations 非空
  3. citation_required=False 时 citations 为空列表
  4. mock_search_results 注入时 citations 包含对应条目
  5. todos 长度等于 max_search_rounds
  6. progress_log 长度等于 max_search_rounds
  7. summary 包含研究主题
  8. report 包含 Markdown 标题（以 "# " 开头）
  9. SmokeRunBlueprintRequest 接受 kit_id 字段
  10. Research Kit Blueprint 通过 smoke_run_blueprint() 5 项检查
"""
import pytest

from xueban.runtime.kits.research_kit import (
    ResearchGoalInputs,
    ResearchSmokeResult,
    build_smoke_result,
    create_research_blueprint,
)
from xueban.runtime.builder_service import (
    BuilderService,
    SmokeRunBlueprintRequest,
)


# ---------------------------------------------------------------------------
# build_smoke_result() 产物结构测试
# ---------------------------------------------------------------------------


class TestBuildSmokeResult:
    """build_smoke_result() 五类产物字段存在性及基本内容测试。"""

    @pytest.fixture
    def default_inputs(self) -> ResearchGoalInputs:
        return ResearchGoalInputs(
            research_topic="大模型推理优化方法",
            output_format="report",
            freshness="any",
            citation_required=True,
            max_search_rounds=2,
        )

    def test_result_has_todos_field(self, default_inputs):
        """产物应包含 todos 字段（列表类型）。"""
        result = build_smoke_result(default_inputs)
        assert "todos" in result
        assert isinstance(result["todos"], list)

    def test_result_has_progress_log_field(self, default_inputs):
        """产物应包含 progress_log 字段（列表类型）。"""
        result = build_smoke_result(default_inputs)
        assert "progress_log" in result
        assert isinstance(result["progress_log"], list)

    def test_result_has_summary_field(self, default_inputs):
        """产物应包含 summary 字段（字符串类型）。"""
        result = build_smoke_result(default_inputs)
        assert "summary" in result
        assert isinstance(result["summary"], str)

    def test_result_has_report_field(self, default_inputs):
        """产物应包含 report 字段（字符串类型）。"""
        result = build_smoke_result(default_inputs)
        assert "report" in result
        assert isinstance(result["report"], str)

    def test_result_has_citations_field(self, default_inputs):
        """产物应包含 citations 字段（列表类型）。"""
        result = build_smoke_result(default_inputs)
        assert "citations" in result
        assert isinstance(result["citations"], list)

    def test_all_five_artifact_fields_present(self, default_inputs):
        """一次性验证所有 5 类产物字段都存在（AC6 主验证）。"""
        result = build_smoke_result(default_inputs)
        required_fields = {"todos", "progress_log", "summary", "report", "citations"}
        missing = required_fields - set(result.keys())
        assert not missing, f"产物缺少以下字段：{missing}"

    def test_todos_length_equals_max_search_rounds(self, default_inputs):
        """todos 列表长度应等于 max_search_rounds。"""
        result = build_smoke_result(default_inputs)
        assert len(result["todos"]) == default_inputs.max_search_rounds

    def test_progress_log_length_equals_max_search_rounds(self, default_inputs):
        """progress_log 列表长度应等于 max_search_rounds。"""
        result = build_smoke_result(default_inputs)
        assert len(result["progress_log"]) == default_inputs.max_search_rounds

    def test_todos_length_with_custom_rounds(self):
        """自定义 max_search_rounds 时 todos 长度应对应。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            max_search_rounds=4,
        )
        result = build_smoke_result(inputs)
        assert len(result["todos"]) == 4

    def test_summary_contains_topic(self, default_inputs):
        """summary 应包含研究主题。"""
        result = build_smoke_result(default_inputs)
        assert default_inputs.research_topic in result["summary"], (
            f"summary 应包含研究主题 '{default_inputs.research_topic}'，"
            f"实际 summary='{result['summary'][:100]}...'"
        )

    def test_report_is_markdown(self, default_inputs):
        """report 应是 Markdown 格式（以 '# ' 开头）。"""
        result = build_smoke_result(default_inputs)
        assert result["report"].startswith("# "), (
            f"report 应以 Markdown H1 标题开头，实际：{result['report'][:50]}"
        )

    def test_citation_required_true_citations_not_empty(self, default_inputs):
        """citation_required=True 时，citations 应非空（至少含一条占位引用）。"""
        result = build_smoke_result(default_inputs)
        assert len(result["citations"]) > 0, (
            "citation_required=True 时 citations 应至少含一条占位引用"
        )

    def test_citation_required_false_citations_empty(self):
        """citation_required=False 时，citations 应为空列表。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            citation_required=False,
        )
        result = build_smoke_result(inputs)
        assert result["citations"] == [], (
            f"citation_required=False 时 citations 应为空列表，实际：{result['citations']}"
        )

    def test_mock_search_results_injected_in_citations(self):
        """mock_search_results 注入时，citations 应包含对应数量的条目。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            citation_required=True,
        )
        mock_results = ["搜索结果 1", "搜索结果 2", "搜索结果 3"]
        result = build_smoke_result(inputs, mock_search_results=mock_results)
        assert len(result["citations"]) == len(mock_results), (
            f"期望 {len(mock_results)} 条引用，实际 {len(result['citations'])} 条"
        )

    def test_citation_trace_structure(self, default_inputs):
        """citations 中每条应包含 pack_id / source_id / excerpt 字段。"""
        result = build_smoke_result(
            default_inputs,
            mock_search_results=["示例片段"],
        )
        citation = result["citations"][0]
        assert "pack_id" in citation, f"citation 缺少 pack_id，实际：{citation}"
        assert "source_id" in citation, f"citation 缺少 source_id，实际：{citation}"
        assert "excerpt" in citation, f"citation 缺少 excerpt，实际：{citation}"

    def test_report_format_answer(self):
        """output_format='answer' 时 report 应包含直接回答格式。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            output_format="answer",
        )
        result = build_smoke_result(inputs)
        assert result["report"].startswith("# "), "report 应以 # 标题开头"

    def test_report_format_structured_outline(self):
        """output_format='structured_outline' 时 report 应包含层级大纲。"""
        inputs = ResearchGoalInputs(
            research_topic="测试主题",
            output_format="structured_outline",
        )
        result = build_smoke_result(inputs)
        assert "##" in result["report"], "structured_outline 格式 report 应含 ## 章节标题"


# ---------------------------------------------------------------------------
# SmokeRunBlueprintRequest 支持 kit_id 字段
# ---------------------------------------------------------------------------


class TestSmokeRunWithKitId:
    """SmokeRunBlueprintRequest 接受 kit_id 字段（Story 10.1 T2）。"""

    def test_smoke_run_request_accepts_kit_id(self):
        """SmokeRunBlueprintRequest 应接受 kit_id='research_kit'。"""
        inputs = ResearchGoalInputs(
            research_topic="大模型推理优化",
            citation_required=True,
        )
        blueprint = create_research_blueprint(inputs)
        req = SmokeRunBlueprintRequest(blueprint=blueprint, kit_id="research_kit")
        assert req.kit_id == "research_kit"

    def test_smoke_run_request_kit_id_optional(self):
        """SmokeRunBlueprintRequest.kit_id 应为可选字段（默认 None）。"""
        inputs = ResearchGoalInputs(research_topic="测试主题")
        blueprint = create_research_blueprint(inputs)
        req = SmokeRunBlueprintRequest(blueprint=blueprint)
        assert req.kit_id is None

    def test_smoke_run_blueprint_passes_4_roles(self):
        """Research Kit Blueprint（4 角色）应通过 smoke_run_blueprint 的角色初始化检查。"""
        inputs = ResearchGoalInputs(
            research_topic="一个足够清晰的研究主题，用于验证最小任务闭环",
            citation_required=True,
            max_search_rounds=2,
        )
        blueprint = create_research_blueprint(inputs)
        svc = BuilderService()
        req = SmokeRunBlueprintRequest(blueprint=blueprint, kit_id="research_kit")
        response = svc.smoke_run_blueprint(req)

        # 角色初始化检查应通过
        role_check = next(
            (c for c in response.checks if c.check_id == "role_init"), None
        )
        assert role_check is not None, "smoke_run 应包含 role_init 检查"
        assert role_check.status == "passed", (
            f"role_init 检查应通过，实际 status={role_check.status}，"
            f"reason={role_check.reason}"
        )

    def test_smoke_run_blueprint_passes_min_task_loop(self):
        """Research Kit Blueprint 应通过最小任务闭环检查。

        注意：smoke_run min_task_loop 检查用 goal.split() 计算词数，
        中文字符串无空格时词数=1（不足3词），因此使用含空格的主题。
        """
        # 使用含空格的研究主题确保 goal 字段 word count >= 3
        inputs = ResearchGoalInputs(
            research_topic="LLM inference optimization methods 2025",
            citation_required=False,
        )
        blueprint = create_research_blueprint(inputs)
        svc = BuilderService()
        req = SmokeRunBlueprintRequest(blueprint=blueprint, kit_id="research_kit")
        response = svc.smoke_run_blueprint(req)

        min_task_check = next(
            (c for c in response.checks if c.check_id == "min_task_loop"), None
        )
        assert min_task_check is not None
        assert min_task_check.status == "passed", (
            f"min_task_loop 检查应通过，实际 status={min_task_check.status}，"
            f"reason={min_task_check.reason}"
        )

    def test_smoke_run_overall_not_failed_on_valid_blueprint(self):
        """合法 Research Kit Blueprint 的 smoke_run 整体状态不应为 'failed'。"""
        inputs = ResearchGoalInputs(
            research_topic="探索 2025 年大语言模型在多模态任务中的最新进展",
            output_format="report",
            freshness="any",
            citation_required=False,
            max_search_rounds=2,
        )
        blueprint = create_research_blueprint(inputs)
        svc = BuilderService()
        req = SmokeRunBlueprintRequest(blueprint=blueprint, kit_id="research_kit")
        response = svc.smoke_run_blueprint(req)

        assert response.status != "failed", (
            f"合法 Blueprint 的 smoke_run 整体状态不应为 'failed'，"
            f"实际 status={response.status}，summary={response.summary}"
        )


# ---------------------------------------------------------------------------
# 端到端：Blueprint 产生 + Smoke Run 产物完整闭环
# ---------------------------------------------------------------------------


class TestResearchKitEndToEnd:
    """Research Kit 端到端 mini 闭环：Blueprint 生成 → Smoke Run 产物。"""

    def test_e2e_5_artifact_types(self):
        """端到端验证：Blueprint 生成后的 smoke result 包含 5 类产物字段。"""
        inputs = ResearchGoalInputs(
            research_topic="生成式 AI 在企业内部知识管理中的应用现状",
            output_format="report",
            freshness="within_month",
            citation_required=True,
            max_search_rounds=3,
        )

        # Step 1: 生成 Blueprint
        blueprint = create_research_blueprint(inputs)
        assert len(blueprint.role_profiles) == 4

        # Step 2: 生成 Smoke Result（mock 搜索工具）
        mock_results = [
            "企业知识管理系统 RAG 集成案例",
            "大模型幻觉率在生产环境的评估方法",
            "内部知识图谱构建最佳实践 2024",
        ]
        result: ResearchSmokeResult = build_smoke_result(
            inputs, mock_search_results=mock_results
        )

        # 验证 5 类产物
        assert len(result["todos"]) == 3  # max_search_rounds=3
        assert len(result["progress_log"]) == 3
        assert result["summary"] != ""
        assert result["report"].startswith("# ")
        assert len(result["citations"]) == 3  # 等于 mock_results 长度

    def test_e2e_citation_required_false_no_citations(self):
        """citation_required=False 时端到端验证 citations 为空。"""
        inputs = ResearchGoalInputs(
            research_topic="快速原型开发流程",
            citation_required=False,
            max_search_rounds=1,
        )
        blueprint = create_research_blueprint(inputs)
        assert blueprint.metadata.get("citation_required") is False

        result: ResearchSmokeResult = build_smoke_result(
            inputs, mock_search_results=["某搜索结果"]
        )
        # citation_required=False 时 citations 应为空
        assert result["citations"] == []
