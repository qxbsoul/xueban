"""tests/test_review_approval_kit.py — Review & Approval Kit 单元测试 (Story 10.3 AC6)

覆盖：
  - Blueprint 结构（2 vs 3 角色，取决于 approval_levels）
  - PolicyProfile 规则正确性（Writer 禁止直接 Deliver）
  - max_reject_rounds=2 时 RetryPolicy 上限为 2
  - approval_levels="single_review" 时 Blueprint 不含 Approver 角色
  - Kit 元数据 / KitDefinition 字段正确性
  - REGISTRY 注册验证
"""
from __future__ import annotations

import pytest

from xueban.runtime.kits.review_approval_kit import (
    REVIEW_APPROVAL_KIT_DEFINITION,
    REVIEW_APPROVAL_SMOKE_CASES,
    ReviewApprovalGoalInputs,
    create_review_approval_blueprint,
)
from xueban.runtime.kits.registry import REGISTRY
from xueban.runtime.contracts_builder import AgentBlueprint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def single_review_inputs() -> ReviewApprovalGoalInputs:
    """approval_levels=single_review → 2 角色。"""
    return ReviewApprovalGoalInputs(
        content_type="document",
        approval_levels="single_review",
        max_reject_rounds=3,
        output_format="markdown",
    )


@pytest.fixture
def review_then_approve_inputs() -> ReviewApprovalGoalInputs:
    """approval_levels=review_then_approve → 3 角色。"""
    return ReviewApprovalGoalInputs(
        content_type="proposal",
        approval_levels="review_then_approve",
        max_reject_rounds=2,
        output_format="markdown",
        reviewer_name="Chief Reviewer",
        approver_name="Final Approver",
    )


@pytest.fixture
def single_review_bp(single_review_inputs: ReviewApprovalGoalInputs) -> AgentBlueprint:
    return create_review_approval_blueprint(single_review_inputs)


@pytest.fixture
def review_then_approve_bp(
    review_then_approve_inputs: ReviewApprovalGoalInputs,
) -> AgentBlueprint:
    return create_review_approval_blueprint(review_then_approve_inputs)


# ---------------------------------------------------------------------------
# AC1 — 向导输入校验（ReviewApprovalGoalInputs）
# ---------------------------------------------------------------------------


def test_inputs_defaults():
    """默认输入字段正确。"""
    inputs = ReviewApprovalGoalInputs()
    assert inputs.content_type == "document"
    assert inputs.approval_levels == "single_review"
    assert inputs.max_reject_rounds == 3
    assert inputs.output_format == "markdown"
    assert inputs.reviewer_name == "Reviewer"
    assert inputs.approver_name == "Approver"


def test_inputs_invalid_content_type():
    with pytest.raises(ValueError, match="content_type"):
        ReviewApprovalGoalInputs(content_type="video")


def test_inputs_invalid_approval_levels():
    with pytest.raises(ValueError, match="approval_levels"):
        ReviewApprovalGoalInputs(approval_levels="multi_level")


def test_inputs_invalid_output_format():
    with pytest.raises(ValueError, match="output_format"):
        ReviewApprovalGoalInputs(output_format="xml")


def test_inputs_max_reject_rounds_range():
    """max_reject_rounds 必须在 1–10 之间。"""
    with pytest.raises(ValueError):
        ReviewApprovalGoalInputs(max_reject_rounds=0)
    with pytest.raises(ValueError):
        ReviewApprovalGoalInputs(max_reject_rounds=11)
    # 边界值正常
    inp = ReviewApprovalGoalInputs(max_reject_rounds=1)
    assert inp.max_reject_rounds == 1
    inp2 = ReviewApprovalGoalInputs(max_reject_rounds=10)
    assert inp2.max_reject_rounds == 10


# ---------------------------------------------------------------------------
# AC2 — Blueprint 角色结构（2 vs 3 角色）
# ---------------------------------------------------------------------------


def test_single_review_has_two_roles(single_review_bp: AgentBlueprint):
    """single_review → 2 角色（Writer + Reviewer），不含 Approver。"""
    assert len(single_review_bp.role_profiles) == 2


def test_single_review_no_approver(single_review_bp: AgentBlueprint):
    """approval_levels="single_review" 时 Blueprint 不包含 Approver 角色。"""
    role_names = [r.name.lower() for r in single_review_bp.role_profiles]
    role_ids = [r.role_id.lower() for r in single_review_bp.role_profiles]
    assert "approver" not in role_names
    assert "approver" not in role_ids


def test_review_then_approve_has_three_roles(review_then_approve_bp: AgentBlueprint):
    """review_then_approve → 3 角色（Writer + Reviewer + Approver）。"""
    assert len(review_then_approve_bp.role_profiles) == 3


def test_review_then_approve_has_approver(review_then_approve_bp: AgentBlueprint):
    """review_then_approve 时 Blueprint 包含 Approver 角色。"""
    role_ids = [r.role_id.lower() for r in review_then_approve_bp.role_profiles]
    assert "approver" in role_ids


def test_role_ids_are_correct(single_review_bp: AgentBlueprint):
    """Writer 和 Reviewer 的 role_id 正确。"""
    role_ids = {r.role_id for r in single_review_bp.role_profiles}
    assert "writer" in role_ids
    assert "reviewer" in role_ids


def test_custom_role_names(review_then_approve_bp: AgentBlueprint):
    """自定义角色姓名正确注入 Blueprint。"""
    role_names = {r.name for r in review_then_approve_bp.role_profiles}
    assert "Chief Reviewer" in role_names
    assert "Final Approver" in role_names


def test_blueprint_mode_is_team(single_review_bp: AgentBlueprint):
    """Blueprint mode 必须是 team。"""
    assert single_review_bp.mode == "team"


# ---------------------------------------------------------------------------
# AC2 — PolicyProfile 规则正确性
# ---------------------------------------------------------------------------


def test_policy_matrix_in_metadata(single_review_bp: AgentBlueprint):
    """Blueprint metadata 中存在 policy_matrix 字段。"""
    assert "policy_matrix" in single_review_bp.metadata
    pm = single_review_bp.metadata["policy_matrix"]
    assert "allow_send" in pm
    assert "allow_reject" in pm


def test_writer_cannot_send_directly_to_end(single_review_bp: AgentBlueprint):
    """Writer 不能直接 deliver（allow_send[writer] 中不含 END）。"""
    pm = single_review_bp.metadata["policy_matrix"]
    writer_send = pm["allow_send"].get("writer", [])
    assert "END" not in writer_send, "Writer 不应被允许直接发送到 END（绕过审核）"


def test_writer_can_only_send_to_reviewer(single_review_bp: AgentBlueprint):
    """Writer 只能发给 Reviewer（send_rule 拦截）。"""
    pm = single_review_bp.metadata["policy_matrix"]
    writer_send = pm["allow_send"].get("writer", [])
    assert "reviewer" in writer_send
    assert len(writer_send) == 1, f"Writer 只应能发给 Reviewer，当前：{writer_send}"


def test_reviewer_can_reject_writer(single_review_bp: AgentBlueprint):
    """Reviewer 可以 reject 回 Writer（reject_rule 正确配置）。"""
    pm = single_review_bp.metadata["policy_matrix"]
    reviewer_reject = pm["allow_reject"].get("reviewer", [])
    assert "writer" in reviewer_reject


def test_approver_can_reject_writer(review_then_approve_bp: AgentBlueprint):
    """Approver 可以 reject 回 Writer。"""
    pm = review_then_approve_bp.metadata["policy_matrix"]
    approver_reject = pm["allow_reject"].get("approver", [])
    assert "writer" in approver_reject


def test_single_review_policy_reviewer_sends_to_end(single_review_bp: AgentBlueprint):
    """single_review 时 Reviewer 通过后发往 END。"""
    pm = single_review_bp.metadata["policy_matrix"]
    reviewer_send = pm["allow_send"].get("reviewer", [])
    assert "END" in reviewer_send


def test_review_then_approve_reviewer_sends_to_approver(
    review_then_approve_bp: AgentBlueprint,
):
    """review_then_approve 时 Reviewer 通过后发往 Approver（不是 END）。"""
    pm = review_then_approve_bp.metadata["policy_matrix"]
    reviewer_send = pm["allow_send"].get("reviewer", [])
    assert "approver" in reviewer_send
    assert "END" not in reviewer_send


# ---------------------------------------------------------------------------
# AC2 — RetryPolicy / max_reject_rounds 映射
# ---------------------------------------------------------------------------


def test_max_reject_rounds_mapping():
    """max_reject_rounds=2 时 RetryPolicy.max_rounds 也为 2。"""
    inputs = ReviewApprovalGoalInputs(max_reject_rounds=2)
    bp = create_review_approval_blueprint(inputs)
    # 在 metadata 的 retry_policy 中检查
    retry = bp.metadata.get("retry_policy", {})
    assert retry.get("max_rounds") == 2, f"RetryPolicy max_rounds 应为 2，当前：{retry}"


def test_max_reject_rounds_in_writer_metadata():
    """max_reject_rounds 也注入到 Writer 角色的 metadata 中。"""
    inputs = ReviewApprovalGoalInputs(max_reject_rounds=5)
    bp = create_review_approval_blueprint(inputs)
    writer = next((r for r in bp.role_profiles if r.role_id == "writer"), None)
    assert writer is not None
    assert writer.metadata.get("max_reject_rounds") == 5
    assert writer.metadata.get("retry_policy", {}).get("max_rounds") == 5


# ---------------------------------------------------------------------------
# AC4 — ApprovalGate 节点配置注入
# ---------------------------------------------------------------------------


def test_approval_gate_nodes_in_metadata(single_review_bp: AgentBlueprint):
    """Blueprint metadata 包含 approval_gate_nodes 列表。"""
    assert "approval_gate_nodes" in single_review_bp.metadata
    nodes = single_review_bp.metadata["approval_gate_nodes"]
    assert isinstance(nodes, list)
    assert len(nodes) >= 1


def test_approval_gate_reviewer_node(single_review_bp: AgentBlueprint):
    """Reviewer 的 ApprovalGate 节点配置正确。"""
    nodes = single_review_bp.metadata["approval_gate_nodes"]
    reviewer_gate = next(
        (n for n in nodes if n.get("approver") == "reviewer"), None
    )
    assert reviewer_gate is not None, "应有 Reviewer 的 approval_gate 节点"
    assert reviewer_gate["type"] == "approval_gate"
    assert reviewer_gate["on_reject"] == "retry"


def test_approval_gate_approver_node_in_three_role(
    review_then_approve_bp: AgentBlueprint,
):
    """review_then_approve 时也有 Approver 的 ApprovalGate 节点。"""
    nodes = review_then_approve_bp.metadata["approval_gate_nodes"]
    approver_gate = next(
        (n for n in nodes if n.get("approver") == "approver"), None
    )
    assert approver_gate is not None, "应有 Approver 的 approval_gate 节点"


def test_single_review_only_one_gate(single_review_bp: AgentBlueprint):
    """single_review 时只有一个 ApprovalGate 节点（Reviewer）。"""
    nodes = single_review_bp.metadata["approval_gate_nodes"]
    assert len(nodes) == 1


def test_review_then_approve_has_two_gates(review_then_approve_bp: AgentBlueprint):
    """review_then_approve 时有两个 ApprovalGate 节点（Reviewer + Approver）。"""
    nodes = review_then_approve_bp.metadata["approval_gate_nodes"]
    assert len(nodes) == 2


# ---------------------------------------------------------------------------
# KitDefinition 字段正确性
# ---------------------------------------------------------------------------


def test_kit_definition_kit_id():
    assert REVIEW_APPROVAL_KIT_DEFINITION.kit_id == "review_approval_kit"


def test_kit_definition_display_name():
    assert "Review & Approval Kit" in REVIEW_APPROVAL_KIT_DEFINITION.display_name


def test_kit_definition_supported_modes():
    modes = REVIEW_APPROVAL_KIT_DEFINITION.supported_modes
    assert "goal" in modes
    assert "scene" in modes
    assert "graph" in modes


def test_kit_definition_category():
    assert REVIEW_APPROVAL_KIT_DEFINITION.category == "review"


def test_kit_definition_result_view():
    assert REVIEW_APPROVAL_KIT_DEFINITION.default_result_view == "approval_inbox"


def test_kit_definition_recommended_inputs():
    inputs = REVIEW_APPROVAL_KIT_DEFINITION.recommended_inputs
    for field in ["content_type", "approval_levels", "max_reject_rounds", "output_format"]:
        assert field in inputs, f"recommended_inputs 应包含 {field!r}"


def test_kit_definition_eval_profile_has_criteria():
    """EvalProfile 必须有 eval_criteria。"""
    ep = REVIEW_APPROVAL_KIT_DEFINITION.default_eval_profile
    assert ep.smoke_eval_enabled is True
    assert len(ep.eval_criteria) >= 1


# ---------------------------------------------------------------------------
# REGISTRY 注册
# ---------------------------------------------------------------------------


def test_registry_has_review_approval_kit():
    """REGISTRY 中已注册 review_approval_kit。"""
    kit = REGISTRY.get("review_approval_kit")
    assert kit is not None, "REGISTRY 中应有 review_approval_kit"
    assert kit.kit_id == "review_approval_kit"


# ---------------------------------------------------------------------------
# Smoke Cases 基本结构
# ---------------------------------------------------------------------------


def test_smoke_cases_count():
    """REVIEW_APPROVAL_SMOKE_CASES 包含 3 个内置 case。"""
    assert len(REVIEW_APPROVAL_SMOKE_CASES) == 3


def test_smoke_case_names():
    names = {c["name"] for c in REVIEW_APPROVAL_SMOKE_CASES}
    assert "happy_path" in names
    assert "reject_rework_path" in names
    assert "approvalgate_visibility" in names


def test_smoke_cases_have_expected_fields():
    for case in REVIEW_APPROVAL_SMOKE_CASES:
        assert "name" in case
        assert "description" in case
        assert "steps" in case
        assert "expected" in case


def test_reject_rework_case_checkpoint():
    """reject_rework_path case 的 expected 中 checkpoint_created=True。"""
    case = next(
        c for c in REVIEW_APPROVAL_SMOKE_CASES if c["name"] == "reject_rework_path"
    )
    assert case["expected"].get("checkpoint_created") is True


def test_all_cases_have_approval_gate_event():
    """所有 case 的 expected 中 approval_gate_event_in_stream=True。"""
    for case in REVIEW_APPROVAL_SMOKE_CASES:
        assert case["expected"].get("approval_gate_event_in_stream") is True, (
            f"case {case['name']!r} 应标记 approval_gate_event_in_stream=True"
        )
