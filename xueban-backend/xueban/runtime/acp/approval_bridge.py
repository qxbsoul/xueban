"""ACP approval bridge (thin re-export shim).

The implementation now lives in xueban.runtime.approval.bridge.
This module is kept as a thin re-export to preserve the existing public
import path:

    from xueban.runtime.acp.approval_bridge import AcpApprovalBridge

No behavior change.
"""

from __future__ import annotations

from xueban.runtime.approval.bridge import AcpApprovalBridge  # noqa: F401

__all__ = ["AcpApprovalBridge"]
