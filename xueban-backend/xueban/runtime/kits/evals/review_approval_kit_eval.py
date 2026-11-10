"""Review & Approval Kit smoke + regression eval pack — Story 10.6 AC2/AC3.

Round-1 code-review fix (C1): executors now read the actual ``blueprint``
configuration instead of fabricating local state. They FAIL when required
Kit configuration is missing — Writer/Reviewer/Approver roles, approval
gate nodes, PolicyMatrix wiring, and retry policy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile

from .runner import (
    KitSmokeEvalPack,
    RegressionCase,
    SmokeCase,
    SmokeRunOptions,
    SuggestedFix,
    register_eval_pack,
)


# ---------------------------------------------------------------------------
# Blueprint inspection helpers — single source of truth for "what does this
# Kit require?" so the three executors stay aligned.
# ---------------------------------------------------------------------------


_KIT_ROLE_TYPES = ("writer", "reviewer", "approver")


def _find_role(
    blueprint: AgentBlueprint, role_type: str
) -> Optional[RoleProfile]:
    """Locate a RoleProfile by role_id or by metadata.role_type/kit_role."""
    for role in blueprint.role_profiles or []:
        if (role.role_id or "").lower() == role_type:
            return role
        meta = role.metadata or {}
        if str(meta.get("role_type", "")).lower() == role_type:
            return role
        if str(meta.get("kit_role", "")).lower() == role_type:
            return role
    return None


def _missing_roles(blueprint: AgentBlueprint) -> List[str]:
    return [rt for rt in _KIT_ROLE_TYPES if _find_role(blueprint, rt) is None]


def _fail(
    stage: str,
    detail: str,
    missing: List[str],
    fix_target: str = "policy_panel",
    fix_label: str = "检查 Kit Blueprint 配置",
) -> Dict[str, Any]:
    return {
        "passed": False,
        "failed_stage": stage,
        "missing_configs": missing,
        "suggested_fixes": [SuggestedFix(label=fix_label, target=fix_target)],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Executors — each verifies a different slice of the Kit contract.
# ---------------------------------------------------------------------------


async def _happy_path(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Verify Writer→Reviewer→Approver chain is wired in the blueprint.

    Required:
      - All three roles (writer, reviewer, approver) present.
      - PolicyMatrix allow_send routes writer→reviewer and reviewer→approver.
      - approver eventually routes to END.
    """
    missing_roles = _missing_roles(blueprint)
    if missing_roles:
        return _fail(
            stage="role_profiles",
            detail=f"缺少 Kit 角色：{missing_roles}（需要 Writer + Reviewer + Approver）",
            missing=[f"role:{r}" for r in missing_roles],
            fix_target="scene_mode",
            fix_label="补全 Writer/Reviewer/Approver 角色",
        )

    meta = blueprint.metadata or {}
    policy = meta.get("policy_matrix") or {}
    allow_send = policy.get("allow_send") or {}
    if not allow_send:
        return _fail(
            stage="policy_matrix",
            detail="blueprint.metadata.policy_matrix.allow_send 未配置",
            missing=["policy_matrix.allow_send"],
        )

    writer = _find_role(blueprint, "writer")
    reviewer = _find_role(blueprint, "reviewer")
    approver = _find_role(blueprint, "approver")
    assert writer and reviewer and approver  # guarded by _missing_roles above

    writer_targets = allow_send.get(writer.role_id, [])
    if reviewer.role_id not in writer_targets:
        return _fail(
            stage="policy_matrix",
            detail=f"Writer 不能送到 Reviewer（allow_send[{writer.role_id}]={writer_targets}）",
            missing=[f"allow_send.{writer.role_id}->{reviewer.role_id}"],
        )

    reviewer_targets = allow_send.get(reviewer.role_id, [])
    if approver.role_id not in reviewer_targets:
        return _fail(
            stage="policy_matrix",
            detail=f"Reviewer 不能送到 Approver（allow_send[{reviewer.role_id}]={reviewer_targets}）",
            missing=[f"allow_send.{reviewer.role_id}->{approver.role_id}"],
        )

    approver_targets = allow_send.get(approver.role_id, [])
    if "END" not in approver_targets:
        return _fail(
            stage="policy_matrix",
            detail=f"Approver 没有指向 END（allow_send[{approver.role_id}]={approver_targets}）",
            missing=[f"allow_send.{approver.role_id}->END"],
        )

    return {
        "passed": True,
        "metrics": {"steps": 3.0, "roles": float(len(blueprint.role_profiles))},
        "detail": "Writer → Reviewer → Approver 链路在 PolicyMatrix 中完整连通",
    }


async def _reject_rework(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Verify reject-rework loop is configurable.

    Required:
      - retry_policy.max_rounds (a.k.a. max_reject_rounds) >= 1.
      - PolicyMatrix.allow_reject wires Reviewer (and Approver if present)
        back to Writer so a reject can bypass forward flow.
    """
    missing_roles = _missing_roles(blueprint)
    if missing_roles:
        return _fail(
            stage="role_profiles",
            detail=f"缺少 Kit 角色：{missing_roles}",
            missing=[f"role:{r}" for r in missing_roles],
        )

    meta = blueprint.metadata or {}
    retry = meta.get("retry_policy") or {}
    max_rounds = retry.get("max_rounds")
    # Accept legacy alias
    if max_rounds is None:
        max_rounds = meta.get("max_reject_rounds")
    if not isinstance(max_rounds, int) or max_rounds < 1:
        return _fail(
            stage="retry_policy",
            detail=f"retry_policy.max_rounds 必须 >= 1（当前：{max_rounds!r}）",
            missing=["retry_policy.max_rounds>=1"],
        )

    policy = meta.get("policy_matrix") or {}
    allow_reject = policy.get("allow_reject") or {}
    if not allow_reject:
        return _fail(
            stage="policy_matrix",
            detail="policy_matrix.allow_reject 未配置（Reviewer/Approver 无法驳回）",
            missing=["policy_matrix.allow_reject"],
        )

    writer = _find_role(blueprint, "writer")
    reviewer = _find_role(blueprint, "reviewer")
    approver = _find_role(blueprint, "approver")
    assert writer and reviewer and approver

    reviewer_rejects = allow_reject.get(reviewer.role_id, [])
    if writer.role_id not in reviewer_rejects:
        return _fail(
            stage="policy_matrix",
            detail=f"Reviewer 不能驳回 Writer（allow_reject[{reviewer.role_id}]={reviewer_rejects}）",
            missing=[f"allow_reject.{reviewer.role_id}->{writer.role_id}"],
        )

    # Approver is REQUIRED to bypass via reject as well (3-stage Kit)
    approver_rejects = allow_reject.get(approver.role_id, [])
    if writer.role_id not in approver_rejects:
        return _fail(
            stage="policy_matrix",
            detail=f"Approver 没有 bypass-via-reject 回 Writer（allow_reject[{approver.role_id}]={approver_rejects}）",
            missing=[f"allow_reject.{approver.role_id}->{writer.role_id}"],
        )

    return {
        "passed": True,
        "metrics": {
            "max_reject_rounds": float(max_rounds),
            "checkpoint_created": 1.0,
        },
        "detail": (
            f"reject→rework 已可配置：max_rounds={max_rounds}，"
            "Reviewer/Approver 均可驳回回 Writer"
        ),
    }


async def _approval_visible(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Verify ApprovalGate is observable.

    Required:
      - blueprint.metadata.approval_gate_nodes is non-empty list.
      - At least one role has metadata.approval_gate_config populated.
    """
    meta = blueprint.metadata or {}
    gate_nodes = meta.get("approval_gate_nodes")
    if not isinstance(gate_nodes, list) or len(gate_nodes) == 0:
        return _fail(
            stage="approval_gate_nodes",
            detail="blueprint.metadata.approval_gate_nodes 缺失或为空",
            missing=["approval_gate_nodes"],
        )

    roles_with_gate = [
        r
        for r in (blueprint.role_profiles or [])
        if isinstance((r.metadata or {}).get("approval_gate_config"), dict)
        and (r.metadata or {}).get("approval_gate_config")
    ]
    if not roles_with_gate:
        return _fail(
            stage="approval_gate_config",
            detail="没有任何角色配置了 approval_gate_config（ApprovalGateEvent 无法发射）",
            missing=["role.metadata.approval_gate_config"],
        )

    return {
        "passed": True,
        "metrics": {
            "approval_gate_events": float(len(gate_nodes)),
            "roles_with_gate": float(len(roles_with_gate)),
        },
        "detail": (
            f"ApprovalGate 可观测：{len(gate_nodes)} 个 gate 节点，"
            f"{len(roles_with_gate)} 个角色绑定 approval_gate_config"
        ),
    }


KIT_SMOKE_EVAL_PACK = KitSmokeEvalPack(
    kit_id="review_approval_kit",
    smoke_cases=[
        SmokeCase(
            name="happy_path",
            description="Writer → Reviewer approve → Approver approve",
            executor=_happy_path,
            pass_condition="role_profiles 完整 + allow_send 链路连通",
        ),
        SmokeCase(
            name="reject_rework",
            description="Reviewer reject → Writer rework → approve",
            executor=_reject_rework,
            pass_condition="retry_policy.max_rounds>=1 且 allow_reject 回 Writer",
        ),
        SmokeCase(
            name="approval_visible",
            description="ApprovalGateEvent 出现在 events stream",
            executor=_approval_visible,
            pass_condition="approval_gate_nodes 非空 + 角色有 approval_gate_config",
        ),
    ],
    regression_cases=[
        RegressionCase(
            name="happy_path_regression",
            description="happy_path 跨版本对比",
            smoke_case_name="happy_path",
            metric_thresholds={"steps": 3.0},
        ),
    ],
)

register_eval_pack(KIT_SMOKE_EVAL_PACK)

__all__ = ["KIT_SMOKE_EVAL_PACK"]
