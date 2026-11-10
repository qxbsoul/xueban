"""Approval-related policy validation helpers (extracted from policy_matrix.py).

This module holds the pure business logic for policy matrix validation:
  - can_send / can_reject : matrix lookup primitives
  - validate_best_practices : non-blocking advisory warnings
  - NOT_RECOMMENDED_PATTERNS rule library

Behavior is byte-for-byte identical to the original xueban.runtime.policy_matrix.
The original module is kept as a thin re-export shim for backward compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from xueban.runtime.contracts import PolicyWarning, WorkflowPolicyMatrixSpec

NOT_RECOMMENDED_PATTERNS: List[dict] = [
    {
        "code": "POLICY_NOT_RECOMMENDED",
        "pattern": "factchecker->legal",
        "reason": "事实核查员直接写入法务节点会绕过内容审核层，违反四眼原则",
        "reference_url": "https://xueban.dev/docs/policy-matrix#best-practices",
        "scope": "allow_send",
        "sender_aliases": {"factchecker", "fact_checker", "事实核查员"},
        "receiver_aliases": {"legal", "法务"},
    },
    {
        "code": "POLICY_NOT_RECOMMENDED",
        "pattern": "content_officer->editor_direct_reject",
        "reason": "内容官直接驳回主编缺少中间协商环节，容易造成决策短路",
        "reference_url": "https://xueban.dev/docs/policy-matrix#best-practices",
        "scope": "allow_reject",
        "sender_aliases": {"content_officer", "内容官"},
        "receiver_aliases": {"editor", "主编"},
    },
]

_SELF_LOOP_RULE: dict = {
    "code": "SELF_APPROVAL_DISCOURAGED",
    "pattern": "self->self",
    "reason": "自我审批/自环违反分权原则，角色不应审批自己的输出",
    "reference_url": "https://xueban.dev/docs/policy-matrix#best-practices",
}

_EMPTY_LIST_RULE: dict = {
    "code": "POLICY_EMPTY_RECEIVER_LIST",
    "pattern": "role->[]",
    "reason": "空接收者列表语义歧义（deny-all vs 遗漏配置），建议移除该键或显式添加接收者",
    "reference_url": "https://xueban.dev/docs/policy-matrix#best-practices",
}


def can_send(matrix: "WorkflowPolicyMatrixSpec", sender: str, receiver: str) -> bool:
    """检查 sender 是否被允许向 receiver 发送消息。"""
    allowed_receivers = matrix.allow_send.get(sender, [])
    return receiver in allowed_receivers


def can_reject(matrix: "WorkflowPolicyMatrixSpec", reviewer: str, target: str) -> bool:
    """检查 reviewer 是否被允许驳回 target。"""
    allowed_targets = matrix.allow_reject.get(reviewer, [])
    return target in allowed_targets


def validate_best_practices(
    matrix: "WorkflowPolicyMatrixSpec",
) -> List["PolicyWarning"]:
    """对矩阵执行最佳实践校验，返回警告列表（非阻塞）。"""
    from xueban.runtime.contracts import PolicyWarning

    warnings: List[PolicyWarning] = []
    seen: set = set()

    for rule in NOT_RECOMMENDED_PATTERNS:
        sender_aliases = rule["sender_aliases"]
        receiver_aliases = rule["receiver_aliases"]
        scope = rule["scope"]

        if scope in ("allow_send", "both"):
            _check_dict(matrix.allow_send, sender_aliases, receiver_aliases,
                        rule, "allow_send", warnings, seen)

        if scope in ("allow_reject", "both"):
            _check_dict(matrix.allow_reject, sender_aliases, receiver_aliases,
                        rule, "allow_reject", warnings, seen)

    _check_self_loops(matrix, warnings, seen)
    _check_empty_lists(matrix, warnings, seen)

    return warnings


def _check_dict(
    mapping: dict,
    sender_aliases: set,
    receiver_aliases: set,
    rule: dict,
    scope_label: str,
    warnings: list,
    seen: set,
) -> None:
    from xueban.runtime.contracts import PolicyWarning

    for sender, receivers in mapping.items():
        if sender.lower() not in sender_aliases:
            continue
        for receiver in dict.fromkeys(receivers):
            if receiver.lower() in receiver_aliases:
                key = (rule["pattern"], sender, receiver)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        PolicyWarning(
                            code=rule["code"],
                            pattern=f"{sender}->{receiver}",
                            reason=rule["reason"],
                            reference_url=rule["reference_url"],
                        )
                    )


def _check_self_loops(
    matrix: "WorkflowPolicyMatrixSpec",
    warnings: list,
    seen: set,
) -> None:
    from xueban.runtime.contracts import PolicyWarning

    for reviewer, targets in matrix.allow_reject.items():
        for target in dict.fromkeys(targets):
            if reviewer == target:
                key = ("self_loop", reviewer, target)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        PolicyWarning(
                            code=_SELF_LOOP_RULE["code"],
                            pattern=f"{reviewer}->{target}",
                            reason=_SELF_LOOP_RULE["reason"],
                            reference_url=_SELF_LOOP_RULE["reference_url"],
                        )
                    )


def _check_empty_lists(
    matrix: "WorkflowPolicyMatrixSpec",
    warnings: list,
    seen: set,
) -> None:
    from xueban.runtime.contracts import PolicyWarning

    for label, mapping in [("allow_send", matrix.allow_send), ("allow_reject", matrix.allow_reject)]:
        for role, targets in mapping.items():
            if len(targets) == 0:
                key = ("empty_list", label, role)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        PolicyWarning(
                            code=_EMPTY_LIST_RULE["code"],
                            pattern=f"{label}.{role}->[]",
                            reason=_EMPTY_LIST_RULE["reason"],
                            reference_url=_EMPTY_LIST_RULE["reference_url"],
                        )
                    )
