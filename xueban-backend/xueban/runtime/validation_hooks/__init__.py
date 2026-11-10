"""Validation hook schemas — N1/002.

The runtime executor for these schemas lives Node-side (see
`server/src/workflow/validation-hook-types.ts` and the runner shipped in
N1/003). Python only owns the schema definition and the team-record CRUD
endpoints in `xueban.api.teams`.
"""

from xueban.runtime.validation_hooks.schema import (
    BuiltinHookConfig,
    HookKind,
    HookOnFail,
    ShellHookConfig,
    ShellSuccessWhen,
    ValidationHookSpec,
    ValidationHooksConfig,
    WebhookHookConfig,
    WebhookSuccessWhen,
)

__all__ = [
    "BuiltinHookConfig",
    "HookKind",
    "HookOnFail",
    "ShellHookConfig",
    "ShellSuccessWhen",
    "ValidationHookSpec",
    "ValidationHooksConfig",
    "WebhookHookConfig",
    "WebhookSuccessWhen",
]
