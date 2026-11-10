"""Policy Matrix 辅助函数 + 内置最佳实践规则库 (thin re-export shim).

The implementation now lives in xueban.runtime.approval.policy.
This module is kept as a thin re-export to preserve the existing public
import path:

    from xueban.runtime.policy_matrix import can_send, can_reject, validate_best_practices

All names below are forwarded by reference — no behavior change.
"""

from __future__ import annotations

from xueban.runtime.approval.policy import (  # noqa: F401
    NOT_RECOMMENDED_PATTERNS,
    _check_dict,
    _check_empty_lists,
    _check_self_loops,
    _EMPTY_LIST_RULE,
    _SELF_LOOP_RULE,
    can_reject,
    can_send,
    validate_best_practices,
)

__all__ = [
    "NOT_RECOMMENDED_PATTERNS",
    "can_reject",
    "can_send",
    "validate_best_practices",
]
