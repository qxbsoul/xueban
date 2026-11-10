"""Story 13.2: Smoke Run workflow_binding check 测试。

测试覆盖：
1. execution_mode=None (无配置) → workflow_binding: skipped
2. execution_mode.mode="react" → workflow_binding: skipped
3. execution_mode.mode="workflow", workflow_ref 有效 → workflow_binding: passed
4. execution_mode.mode="workflow", workflow_ref 为空 → workflow_binding: warning
5. 完整 smoke run 返回 6 项 check（包含 workflow_binding）
"""

from __future__ import annotations

import pytest

from xueban.runtime.builder_service import (
    BuilderService,
    SmokeRunBlueprintRequest,
)
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    ExecutionMode,
    RoleProfile,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _minimal_blueprint(**overrides) -> AgentBlueprint:
    """最小合法 Blueprint（mode=single，含一个 role）。"""
    defaults = dict(
        name="Test Agent",
        goal="测试用最小 Blueprint 目标描述",
        mode="single",
        role_profiles=[RoleProfile(name="test_role")],
    )
    defaults.update(overrides)
    return AgentBlueprint(**defaults)


@pytest.fixture()
def service() -> BuilderService:
    return BuilderService()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestWorkflowBindingCheck:
    """workflow_binding Smoke Run check 系列测试。"""

    def test_no_execution_mode_skipped(self, service: BuilderService):
        """execution_mode=None → workflow_binding: skipped。"""
        bp = _minimal_blueprint(execution_mode=None)
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        wb = next((c for c in result.checks if c.check_id == "workflow_binding"), None)
        assert wb is not None, "workflow_binding check 应存在"
        assert wb.status == "skipped"
        assert "ReAct" in wb.reason

    def test_react_mode_skipped(self, service: BuilderService):
        """execution_mode.mode='react' → workflow_binding: skipped。"""
        bp = _minimal_blueprint(execution_mode=ExecutionMode(mode="react"))
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        wb = next((c for c in result.checks if c.check_id == "workflow_binding"), None)
        assert wb is not None
        assert wb.status == "skipped"

    def test_workflow_mode_with_valid_ref_passed(self, service: BuilderService):
        """execution_mode.mode='workflow' + workflow_ref 有效 → passed。"""
        em = ExecutionMode(
            mode="workflow",
            workflow_ref="wf-12345678",
            workflow_name="研究助手工作流",
        )
        bp = _minimal_blueprint(execution_mode=em)
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        wb = next((c for c in result.checks if c.check_id == "workflow_binding"), None)
        assert wb is not None
        assert wb.status == "passed"
        assert "wf-12345678" in wb.reason

    def test_workflow_mode_empty_ref_warning(self, service: BuilderService):
        """execution_mode.mode='workflow' + workflow_ref 为空 → warning。"""
        em = ExecutionMode(mode="workflow", workflow_ref=None, workflow_name=None)
        bp = _minimal_blueprint(execution_mode=em)
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        wb = next((c for c in result.checks if c.check_id == "workflow_binding"), None)
        assert wb is not None
        assert wb.status == "warning"
        assert "ReAct" in wb.reason or "workflow_ref" in wb.reason.lower() or "未指定" in wb.reason

    def test_smoke_run_returns_workflow_binding_check(self, service: BuilderService):
        """完整 smoke run 返回 checks 列表中包含 workflow_binding。"""
        bp = _minimal_blueprint()
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        check_ids = {c.check_id for c in result.checks}
        assert "workflow_binding" in check_ids

    def test_workflow_binding_does_not_block_overall_status(self, service: BuilderService):
        """workflow_binding warning 不将整体状态提升为 failed（只有 failed 状态检查会）。"""
        em = ExecutionMode(mode="workflow", workflow_ref=None)
        bp = _minimal_blueprint(execution_mode=em)
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        # 整体状态应为 warning 而不是 failed（其他项均通过）
        assert result.status in ("passed", "warning")
        assert result.status != "failed"

    def test_workflow_binding_passes_for_any_nonempty_ref_known_gap(
        self, service: BuilderService
    ):
        """H2 follow-up — 显式登记 known-gap：smoke check 当前是 tautology。

        任何非空字符串 workflow_ref 都会被 passed。真实 template registry 校验
        是 Phase-2 工作（见 spec Completion Notes "Phase-1 装饰性"）。本测试锁定
        此行为，避免下游误以为 smoke 已做存在性校验。
        """
        for fake_ref in ("deleted-wf", "wf-does-not-exist", "x", "🦄"):
            em = ExecutionMode(mode="workflow", workflow_ref=fake_ref)
            bp = _minimal_blueprint(execution_mode=em)
            req = SmokeRunBlueprintRequest(blueprint=bp)
            result = service.smoke_run_blueprint(req)
            wb = next(
                (c for c in result.checks if c.check_id == "workflow_binding"), None
            )
            assert wb is not None
            # known-gap：未做真实存在性校验，全部 passed
            assert wb.status == "passed", (
                f"workflow_ref={fake_ref!r} expected to pass under Phase-1 "
                f"tautology semantics; if this changed, update spec known-gap."
            )

    def test_workflow_binding_check_has_correct_failure_category(self, service: BuilderService):
        """workflow_binding check 的 failure_category 应为 'none'（不触发 Fix Action 路由）。"""
        em = ExecutionMode(mode="workflow", workflow_ref=None)
        bp = _minimal_blueprint(execution_mode=em)
        req = SmokeRunBlueprintRequest(blueprint=bp)
        result = service.smoke_run_blueprint(req)

        wb = next((c for c in result.checks if c.check_id == "workflow_binding"), None)
        assert wb is not None
        assert wb.failure_category == "none"
