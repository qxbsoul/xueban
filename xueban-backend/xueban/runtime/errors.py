"""Xueban 运行时错误体系。"""

from __future__ import annotations

from typing import Any, Dict


class XuebanError(Exception):
    """所有运行时业务错误的基类。"""

    code: str = "XUEBAN_ERROR"

    def __init__(self, message: str, details: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PolicyViolation(XuebanError):
    """尝试执行策略矩阵不允许的操作（如未授权角色发起驳回）。"""

    code = "POLICY_VIOLATION"

    def __init__(self, reviewer: str, target: str, reason: str = "") -> None:
        super().__init__(
            f"Policy violation: {reviewer!r} is not allowed to reject {target!r}",
            details={"reviewer": reviewer, "target": target, "reason": reason},
        )


class PolicyMismatch(XuebanError):
    """Policy Matrix 与节点角色不一致（compile-time 阻塞校验）。"""

    code = "POLICY_MISMATCH"

    def __init__(self, reason: str, details: dict | None = None) -> None:
        super().__init__(f"Policy mismatch: {reason}", details=details or {})


class ProviderTimeout(XuebanError):
    """Provider 调用超时。"""

    code = "PROVIDER_TIMEOUT"


class SanitizeRejected(XuebanError):
    """内容被 sanitize 扫描拒绝（Epic 5 使用）。"""

    code = "SANITIZE_REJECTED"


class ProviderUnavailableError(XuebanError):
    """Provider 不可用（broker 初始化失败、服务端点无响应等）。"""

    code = "PROVIDER_UNAVAILABLE"


class InsufficientBalanceError(XuebanError):
    """0G Compute 账户余额不足。"""

    code = "INSUFFICIENT_BALANCE"


class MissingChatIdError(XuebanError):
    """0G Compute 响应缺少 ChatID（ZG-Res-Key header 和 data.id 均缺失）。"""

    code = "MISSING_CHAT_ID"


class McpError(Exception):
    """Raised by McpClient / McpAgentExecutor for structured MCP failures (Story 2.4).

    code values:
      MCP_SERVER_UNAVAILABLE  — subprocess failed to start or connect timeout
      MCP_TOOL_NOT_FOUND      — tool name absent from list_tools() response
      MCP_TOOL_ERROR          — tools/call returned an error response
    """

    def __init__(self, code: str, detail: str = "", **kwargs: object) -> None:
        self.code = code
        self.detail = detail
        for k, v in kwargs.items():
            setattr(self, k, v)
        parts = [f"code={code!r}"]
        if detail:
            parts.append(f"detail={detail!r}")
        for k, v in kwargs.items():
            parts.append(f"{k}={v!r}")
        super().__init__(", ".join(parts))
