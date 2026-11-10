"""Story 1.1 — PolicyMatrix 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.contracts import (
    EdgeDefinition,
    NodeDefinition,
    PolicyWarning,
    WorkflowDefinition,
    WorkflowPolicyMatrixSpec,
    WorkflowValidationResult,
)
from xueban.runtime.policy_matrix import can_reject, can_send, validate_best_practices
from xueban.runtime.service import RuntimeService


# ---------------------------------------------------------------------------
# 最小合法工作流 fixture
# ---------------------------------------------------------------------------

def _make_workflow(**kwargs) -> dict:
    base = {
        "workflow_id": "test-wf",
        "version": "1.0",
        "name": "Test",
        "entrypoint": "agent_a",
        "nodes": [
            {"id": "agent_a", "type": "agent"},
            {"id": "agent_b", "type": "agent"},
        ],
        "edges": [{"from": "agent_a", "to": "agent_b", "type": "final"}],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AC #1 — WorkflowPolicyMatrixSpec 序列化 / 反序列化
# ---------------------------------------------------------------------------

class TestWorkflowPolicyMatrixSpec:
    def test_roundtrip_empty(self):
        pm = WorkflowPolicyMatrixSpec()
        data = pm.model_dump()
        pm2 = WorkflowPolicyMatrixSpec.model_validate(data)
        assert pm2.allow_send == {}
        assert pm2.allow_reject == {}
        assert pm2.version == "1.0"

    def test_roundtrip_with_roles(self):
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"agent_a": ["agent_b"]},
            allow_reject={"reviewer": ["agent_a"]},
            description="测试矩阵",
        )
        data = pm.model_dump()
        pm2 = WorkflowPolicyMatrixSpec.model_validate(data)
        assert pm2.allow_send == {"agent_a": ["agent_b"]}
        assert pm2.allow_reject == {"reviewer": ["agent_a"]}
        assert pm2.description == "测试矩阵"

    def test_optional_description(self):
        pm = WorkflowPolicyMatrixSpec(allow_send={"a": ["b"]})
        assert pm.description is None

    def test_default_version(self):
        pm = WorkflowPolicyMatrixSpec()
        assert pm.version == "1.0"


# ---------------------------------------------------------------------------
# AC #1 — WorkflowDefinition policy_matrix 字段 + 向后兼容
# ---------------------------------------------------------------------------

class TestWorkflowDefinitionPolicyMatrix:
    def test_policy_matrix_absent_backward_compat(self):
        """老 YAML 不带 policy_matrix 仍可跑。"""
        wf = WorkflowDefinition.model_validate(_make_workflow())
        assert wf.policy_matrix is None

    def test_policy_matrix_valid_roles(self):
        pm = {
            "allow_send": {"agent_a": ["agent_b"]},
            "allow_reject": {"agent_b": ["agent_a"]},
        }
        wf = WorkflowDefinition.model_validate(_make_workflow(policy_matrix=pm))
        assert wf.policy_matrix is not None
        assert wf.policy_matrix.allow_send == {"agent_a": ["agent_b"]}

    def test_model_validator_captures_invalid_role_ids(self):
        """model_validator 捕获非法 role id（via WorkflowDefinition.validate_graph）。"""
        pm = {
            "allow_send": {"ghost_role": ["agent_a"]},  # ghost_role 不是已声明节点
            "allow_reject": {},
        }
        with pytest.raises(ValidationError, match="undeclared role ids"):
            WorkflowDefinition.model_validate(_make_workflow(policy_matrix=pm))

    def test_model_validator_captures_invalid_receiver(self):
        pm = {
            "allow_send": {"agent_a": ["unknown_target"]},
        }
        with pytest.raises(ValidationError, match="undeclared role ids"):
            WorkflowDefinition.model_validate(_make_workflow(policy_matrix=pm))


# ---------------------------------------------------------------------------
# AC #1 — 帮助函数 can_send / can_reject
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def setup_method(self):
        self.pm = WorkflowPolicyMatrixSpec(
            allow_send={"agent_a": ["agent_b", "agent_c"]},
            allow_reject={"reviewer": ["agent_a"]},
        )

    def test_can_send_allowed(self):
        assert can_send(self.pm, "agent_a", "agent_b") is True

    def test_can_send_not_allowed(self):
        assert can_send(self.pm, "agent_b", "agent_a") is False

    def test_can_send_unknown_sender(self):
        assert can_send(self.pm, "ghost", "agent_b") is False

    def test_can_reject_allowed(self):
        assert can_reject(self.pm, "reviewer", "agent_a") is True

    def test_can_reject_not_allowed(self):
        assert can_reject(self.pm, "agent_a", "reviewer") is False


# ---------------------------------------------------------------------------
# AC #2 — validate_best_practices 返回 POLICY_NOT_RECOMMENDED 警告
# ---------------------------------------------------------------------------

class TestValidateBestPractices:
    def test_factchecker_to_legal_triggers_warning(self):
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"factchecker": ["legal"]},
        )
        warnings = validate_best_practices(pm)
        assert len(warnings) >= 1
        codes = [w.code for w in warnings]
        assert "POLICY_NOT_RECOMMENDED" in codes

    def test_clean_matrix_no_warnings(self):
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"agent_a": ["agent_b"]},
        )
        warnings = validate_best_practices(pm)
        assert warnings == []

    def test_warning_has_reason(self):
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"factchecker": ["legal"]},
        )
        warnings = validate_best_practices(pm)
        assert all(isinstance(w, PolicyWarning) for w in warnings)
        assert all(w.reason for w in warnings)

    def test_warning_model_fields(self):
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"factchecker": ["legal"]},
        )
        warnings = validate_best_practices(pm)
        w = warnings[0]
        assert w.code == "POLICY_NOT_RECOMMENDED"
        assert "factchecker" in w.pattern or "legal" in w.pattern

    def test_substring_no_false_positive(self):
        """legal_advisor 不应触发 factchecker->legal 规则。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"factchecker": ["legal_advisor"]},
        )
        warnings = validate_best_practices(pm)
        codes = [w.code for w in warnings]
        assert "POLICY_NOT_RECOMMENDED" not in codes

    def test_chinese_role_names_trigger_warning(self):
        """中文角色名（事实核查员→法务）应触发最佳实践警告。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"事实核查员": ["法务"]},
        )
        warnings = validate_best_practices(pm)
        assert any(w.code == "POLICY_NOT_RECOMMENDED" for w in warnings)

    def test_content_officer_editor_only_reject(self):
        """content_officer→editor 仅在 allow_reject 中触发，allow_send 中不触发。"""
        pm_send = WorkflowPolicyMatrixSpec(
            allow_send={"content_officer": ["editor"]},
        )
        pm_reject = WorkflowPolicyMatrixSpec(
            allow_reject={"content_officer": ["editor"]},
        )
        warnings_send = validate_best_practices(pm_send)
        warnings_reject = validate_best_practices(pm_reject)
        assert not any(w.code == "POLICY_NOT_RECOMMENDED" for w in warnings_send)
        assert any(w.code == "POLICY_NOT_RECOMMENDED" for w in warnings_reject)

    def test_self_loop_warning(self):
        """自环 allow_reject 应触发 SELF_APPROVAL_DISCOURAGED。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_reject={"alice": ["alice"]},
        )
        warnings = validate_best_practices(pm)
        assert any(w.code == "SELF_APPROVAL_DISCOURAGED" for w in warnings)

    def test_empty_receiver_list_warning(self):
        """空接收者列表应触发 POLICY_EMPTY_RECEIVER_LIST。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"alice": []},
        )
        warnings = validate_best_practices(pm)
        assert any(w.code == "POLICY_EMPTY_RECEIVER_LIST" for w in warnings)

    def test_duplicate_receivers_deduped(self):
        """重复接收者在模型层面被去重。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"alice": ["bob", "bob", "charlie", "bob"]},
        )
        assert pm.allow_send["alice"] == ["bob", "charlie"]

    def test_no_duplicate_warnings_for_same_pair(self):
        """同一 pair 同时在 allow_send 和 allow_reject 中只产生一条警告（如果 scope 限制命中）。"""
        pm = WorkflowPolicyMatrixSpec(
            allow_send={"factchecker": ["legal"]},
            allow_reject={"factchecker": ["legal"]},
        )
        warnings = validate_best_practices(pm)
        factchecker_legal = [w for w in warnings if "factchecker" in w.pattern and "legal" in w.pattern
                            and w.code == "POLICY_NOT_RECOMMENDED"]
        assert len(factchecker_legal) == 1


# ---------------------------------------------------------------------------
# AC #2 — /workflow/validate 非阻塞：warning 返回 200
# ---------------------------------------------------------------------------

class TestValidateEndpointNonBlocking:
    def setup_method(self):
        self.service = RuntimeService()

    def test_validate_returns_200_with_warnings(self):
        """有 policy warning 时 validate_workflow 也返回 valid=True (200 非阻塞)。"""
        wf = WorkflowDefinition.model_validate(
            _make_workflow(
                nodes=[
                    {"id": "factchecker", "type": "agent"},
                    {"id": "legal", "type": "agent"},
                ],
                edges=[{"from": "factchecker", "to": "legal", "type": "final"}],
                entrypoint="factchecker",
                policy_matrix={
                    "allow_send": {"factchecker": ["legal"]},
                },
            )
        )
        result = self.service.validate_workflow(wf)
        assert isinstance(result, WorkflowValidationResult)
        assert result.valid is True
        assert len(result.policy_warnings) >= 1
        assert result.policy_warnings[0].code == "POLICY_NOT_RECOMMENDED"

    def test_validate_no_policy_matrix_still_valid(self):
        wf = WorkflowDefinition.model_validate(_make_workflow())
        result = self.service.validate_workflow(wf)
        assert result.valid is True
        assert result.policy_warnings == []

    def test_validate_result_has_warnings_and_errors_fields(self):
        wf = WorkflowDefinition.model_validate(_make_workflow())
        result = self.service.validate_workflow(wf)
        assert hasattr(result, "warnings")
        assert hasattr(result, "errors")
        assert hasattr(result, "policy_warnings")
