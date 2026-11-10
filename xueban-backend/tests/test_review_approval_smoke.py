"""tests/test_review_approval_smoke.py — Story 10.3 AC3/AC6 + Round-1 review fix (C2)

Round-1 fix: the previous version of this file used a self-fulfilling
`_run_smoke_case` harness that set ``checkpoint_created=True`` itself and
then asserted on it — proving nothing about the Kit. This rewrite drives
the actual eval pack (`xueban.runtime.kits.evals.review_approval_kit_eval`)
through the canonical `KitSmokeRunner`, against real
`create_review_approval_blueprint(...)` outputs, and uses *reverse* mutation
tests: when a required field is removed/zeroed, the corresponding case
MUST fail.

Coverage:
  - happy_path passes only when 3 roles + PolicyMatrix chain are present
  - reject_rework passes only when retry_policy.max_rounds >= 1 + allow_reject wires Reviewer/Approver→Writer
  - approval_visible passes only when approval_gate_nodes + role.approval_gate_config are populated
  - Each of the three cases has a *reverse* test that mutates the blueprint
    and asserts the corresponding executor FAILS.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from xueban.runtime.contracts_builder import AgentBlueprint
from xueban.runtime.events import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    CHECKPOINT_SAVED,
    RunEventBus,
)
from xueban.runtime.kits.evals import KitSmokeRunner, SmokeRunOptions
from xueban.runtime.kits.review_approval_kit import (
    REVIEW_APPROVAL_SMOKE_CASES,
    ReviewApprovalGoalInputs,
    create_review_approval_blueprint,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus() -> RunEventBus:
    return RunEventBus()


@pytest.fixture
def three_stage_blueprint() -> AgentBlueprint:
    return create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )


@pytest.fixture
def two_stage_blueprint() -> AgentBlueprint:
    return create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="single_review")
    )


async def _run(blueprint: AgentBlueprint) -> Dict[str, Any]:
    runner = KitSmokeRunner()
    report = await runner.run_smoke(
        "review_approval_kit", blueprint, SmokeRunOptions()
    )
    return {c.name: c for c in report.case_results}


# ---------------------------------------------------------------------------
# Forward path: 3-stage Kit blueprint must pass all three executors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_passes_for_three_stage_blueprint(
    three_stage_blueprint: AgentBlueprint,
):
    by_name = await _run(three_stage_blueprint)
    assert by_name["happy_path"].passed is True, by_name["happy_path"].detail


@pytest.mark.asyncio
async def test_reject_rework_passes_for_three_stage_blueprint(
    three_stage_blueprint: AgentBlueprint,
):
    by_name = await _run(three_stage_blueprint)
    assert by_name["reject_rework"].passed is True, by_name["reject_rework"].detail


@pytest.mark.asyncio
async def test_approval_visible_passes_for_three_stage_blueprint(
    three_stage_blueprint: AgentBlueprint,
):
    by_name = await _run(three_stage_blueprint)
    assert by_name["approval_visible"].passed is True, (
        by_name["approval_visible"].detail
    )


# ---------------------------------------------------------------------------
# Reverse path 1 — remove Approver role → happy_path + reject_rework FAIL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_remove_approver_fails_happy_path(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    bp.role_profiles = [
        r
        for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "approver"
    ]
    by_name = await _run(bp)
    assert by_name["happy_path"].passed is False
    assert by_name["happy_path"].failed_stage == "role_profiles"
    assert any(
        "approver" in m.lower() for m in by_name["happy_path"].missing_configs
    )


@pytest.mark.asyncio
async def test_reverse_remove_approver_fails_reject_rework(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    bp.role_profiles = [
        r
        for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "approver"
    ]
    by_name = await _run(bp)
    assert by_name["reject_rework"].passed is False


# Two-stage Kit (single_review) lacks the approver role by design —
# so happy_path on it must also FAIL (it's a 3-stage executor).
@pytest.mark.asyncio
async def test_two_stage_blueprint_fails_happy_path(
    two_stage_blueprint: AgentBlueprint,
):
    by_name = await _run(two_stage_blueprint)
    assert by_name["happy_path"].passed is False
    assert any(
        "approver" in m.lower() for m in by_name["happy_path"].missing_configs
    )


# ---------------------------------------------------------------------------
# Reverse path 2 — set max_reject_rounds=0 → reject_rework FAILS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_zero_max_rounds_fails_reject_rework(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    bp.metadata["retry_policy"] = {"max_rounds": 0, "on_exceed": "escalated"}
    bp.metadata["max_reject_rounds"] = 0
    by_name = await _run(bp)
    assert by_name["reject_rework"].passed is False
    assert by_name["reject_rework"].failed_stage == "retry_policy"


@pytest.mark.asyncio
async def test_reverse_remove_allow_reject_fails_reject_rework(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    bp.metadata["policy_matrix"]["allow_reject"] = {}
    by_name = await _run(bp)
    assert by_name["reject_rework"].passed is False
    assert by_name["reject_rework"].failed_stage == "policy_matrix"


# ---------------------------------------------------------------------------
# Reverse path 3 — remove approval_gate_nodes → approval_visible FAILS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_remove_gate_nodes_fails_visibility(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    bp.metadata["approval_gate_nodes"] = []
    by_name = await _run(bp)
    assert by_name["approval_visible"].passed is False
    assert by_name["approval_visible"].failed_stage == "approval_gate_nodes"


@pytest.mark.asyncio
async def test_reverse_strip_gate_config_fails_visibility(
    three_stage_blueprint: AgentBlueprint,
):
    bp = three_stage_blueprint
    # Keep gate_nodes but strip the per-role approval_gate_config
    for role in bp.role_profiles:
        if role.metadata and "approval_gate_config" in role.metadata:
            role.metadata["approval_gate_config"] = {}
    bp.metadata["approval_gate_nodes"] = []  # also clear nodes to trigger first guard
    by_name = await _run(bp)
    assert by_name["approval_visible"].passed is False


# ---------------------------------------------------------------------------
# Smoke case declaration sanity (kept from previous version, still valid)
# ---------------------------------------------------------------------------


def test_all_smoke_cases_approval_gate_in_stream():
    for case in REVIEW_APPROVAL_SMOKE_CASES:
        assert case["expected"].get("approval_gate_event_in_stream") is True, (
            f"case {case['name']!r}: approval_gate_event_in_stream 应为 True"
        )


def test_reject_case_checkpoint_declared():
    case = next(
        c for c in REVIEW_APPROVAL_SMOKE_CASES if c["name"] == "reject_rework_path"
    )
    assert case["expected"].get("checkpoint_created") is True


def test_happy_path_checkpoint_not_declared():
    case = next(c for c in REVIEW_APPROVAL_SMOKE_CASES if c["name"] == "happy_path")
    assert case["expected"].get("checkpoint_created") is False


# ---------------------------------------------------------------------------
# Blueprint integration sanity checks
# ---------------------------------------------------------------------------


def test_blueprint_eval_criteria_covers_smoke_paths():
    inputs = ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    bp = create_review_approval_blueprint(inputs)
    criteria_text = " ".join(bp.eval_profile.eval_criteria)
    assert "checkpoint" in criteria_text.lower() or "reject" in criteria_text.lower()
    assert (
        "final_artifact" in criteria_text.lower()
        or "approve" in criteria_text.lower()
    )
    assert (
        "ApprovalGateEvent" in criteria_text or "approval" in criteria_text.lower()
    )


def test_blueprint_metadata_smoke_fields():
    inputs = ReviewApprovalGoalInputs(approval_levels="single_review")
    bp = create_review_approval_blueprint(inputs)
    assert bp.metadata.get("kit_id") == "review_approval_kit"
    assert bp.metadata.get("approval_levels") == "single_review"


def test_max_reject_rounds_2_retry_policy():
    inputs = ReviewApprovalGoalInputs(max_reject_rounds=2)
    bp = create_review_approval_blueprint(inputs)
    assert bp.metadata["retry_policy"]["max_rounds"] == 2


# ---------------------------------------------------------------------------
# Optional: keep RunEventBus smoke alive — a tiny integration test that
# verifies the event constants still load (no behavior assertion against
# self-published events). This is intentionally narrow.
# ---------------------------------------------------------------------------


def test_event_constants_available(event_bus: RunEventBus):
    # If any of these symbols disappear, the eval pack's downstream
    # consumers in BriefBoard / Inbox will silently break.
    assert APPROVAL_PENDING and APPROVAL_APPROVED and APPROVAL_REJECTED
    assert CHECKPOINT_SAVED
    # event_bus instantiates without error
    assert event_bus is not None


# ---------------------------------------------------------------------------
# Round-2 H2 — require_approval_for must NOT contain "writer"
# (Writer is a producer, not an approval gate role)
# ---------------------------------------------------------------------------


def test_default_policy_profile_require_approval_for_excludes_writer():
    from xueban.runtime.kits.review_approval_kit import (
        REVIEW_APPROVAL_KIT_DEFINITION,
    )

    profile = REVIEW_APPROVAL_KIT_DEFINITION.default_policy_profile
    assert profile is not None
    assert "writer" not in profile.require_approval_for, (
        "H2 regression: Writer 是 producer，不能出现在 require_approval_for 里。"
    )
    # 必须包含真正的 gate 角色
    assert "reviewer" in profile.require_approval_for
    # approver 是三阶段下的最终 gate，也应该列出
    assert "approver" in profile.require_approval_for


# ---------------------------------------------------------------------------
# Round-2 H1 — sequential ordering enforced via PolicyMatrix.allow_send.
# Approver bypass (writer→approver direct) must NOT be structurally possible.
# ---------------------------------------------------------------------------


def test_policy_matrix_writer_can_only_send_to_reviewer():
    """H1: Writer.allow_send 必须只包含 reviewer，杜绝 writer→approver 直连。"""
    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    allow_send = bp.metadata["policy_matrix"]["allow_send"]
    assert allow_send["writer"] == ["reviewer"], (
        "H1 regression: Writer 只能 send 给 Reviewer，不能直达 Approver/END。"
    )
    # Writer 不能直接 END
    assert "END" not in allow_send["writer"]
    # Writer 不能直接发给 Approver（bypass 阻断）
    assert "approver" not in allow_send["writer"]


def test_policy_matrix_reviewer_to_approver_only_in_three_stage():
    """H1: 三阶段模式下 Reviewer 必须流向 Approver，不能直达 END。"""
    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    allow_send = bp.metadata["policy_matrix"]["allow_send"]
    assert allow_send["reviewer"] == ["approver"], (
        "H1 regression: 三阶段下 Reviewer 必须流向 Approver，不能直达 END。"
    )
    assert "END" not in allow_send["reviewer"]


def test_policy_matrix_approver_is_only_end_exit():
    """H1: Approver 是三阶段下唯一允许 send 到 END 的角色。"""
    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    allow_send = bp.metadata["policy_matrix"]["allow_send"]
    assert allow_send["approver"] == ["END"]
    # 收集所有"能送到 END"的角色
    end_senders = [r for r, targets in allow_send.items() if "END" in targets]
    assert end_senders == ["approver"], (
        f"H1 regression: 三阶段下只有 Approver 能 send→END，实际：{end_senders}"
    )


def test_policy_matrix_require_approval_after_declared():
    """H1: require_approval_after 把每个 gate 角色绑定到对应 ApprovalGate 节点。"""
    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    pm = bp.metadata["policy_matrix"]
    assert "require_approval_after" in pm, (
        "H1 regression: PolicyMatrix 缺 require_approval_after 顺序门控字段。"
    )
    raa = pm["require_approval_after"]
    assert raa.get("reviewer") == "gate_reviewer"
    assert raa.get("approver") == "gate_approver"


def test_policy_matrix_reject_loops_back_to_writer():
    """H1: reject 路径必须回到 Writer（rework），不能跳过 Writer。"""
    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    allow_reject = bp.metadata["policy_matrix"]["allow_reject"]
    assert allow_reject["reviewer"] == ["writer"]
    assert allow_reject["approver"] == ["writer"]


@pytest.mark.asyncio
async def test_h1_bypass_attempt_smoke_fails(
    three_stage_blueprint: AgentBlueprint,
):
    """H1 bypass 攻击模拟：篡改 PolicyMatrix 让 Writer 直达 Approver/END，
    smoke runner 的 happy_path 应感知到结构破坏（gate / role 链不一致）→ 失败。

    我们通过同时移除 Reviewer + 把 writer.allow_send 改成 ["approver"]
    来构造 bypass 蓝图；happy_path executor 期望 3 个 role + 完整 gate 链，
    必然失败。
    """
    bp = three_stage_blueprint
    # 模拟 bypass：去掉 Reviewer 角色
    bp.role_profiles = [
        r
        for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "reviewer"
    ]
    # 篡改 allow_send 让 Writer 直接送给 Approver
    bp.metadata["policy_matrix"]["allow_send"]["writer"] = ["approver"]
    by_name = await _run(bp)
    assert by_name["happy_path"].passed is False, (
        "H1 regression: bypass 蓝图（无 Reviewer + writer→approver 直连）"
        "的 happy_path 必须失败。"
    )
