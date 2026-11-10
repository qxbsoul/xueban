"""ValidationHookSpec — N1/002

Pydantic schemas for `team.validation_hooks: HookSpec[]` (see
`docs/design/team-validation-hook-v1.md` §5 Hook Schema).

This module owns the **canonical** field shape; the TypeScript twin at
`server/src/workflow/validation-hook-types.ts` mirrors these fields and MUST
stay in sync (manual mirror — see header note there).

PM decisions (2026-05-26) reflected here:

* Q12.1 — `team.validation_hooks` lives at the top level of the team record,
  parallel to `policy_obj` / `policy_matrix` / `workflow`. Loaders should
  expect this exact key (not nested under policy).
* Q12.2 — `on_fail: "retry"` retries **only the hook** (the DAG is not
  rolled back). `max_retries` default is `0` (= disabled); retry is only
  appropriate for idempotent "wait + check" scenarios (poll-CI / readiness
  probe), NOT "let the LLM re-edit and try again". See `max_retries`
  docstring for the exact warning surfaced to users.
* Q12.3 — Hook config gains `expose_error_details: bool = False`. When
  `False` (default), the runner-side `results_json` serializer MUST redact
  upstream node `error.message` / `error.stack` to `"[redacted]"` before
  passing into the hook template (preventing accidental leak of internal
  errors to webhooks / shell commands). When `True` the user has explicitly
  opted in.

The runner (N1/003) and validator registry (N1/004) consume these schemas;
this file does NOT execute hooks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# kind-specific config sub-models
# ---------------------------------------------------------------------------


class ShellSuccessWhen(BaseModel):
    """Success criterion for a shell hook (default: exit_code == 0)."""

    model_config = ConfigDict(extra="forbid")

    exit_code: int = 0


class ShellHookConfig(BaseModel):
    """Spawn a child process; success governed by `success_when.exit_code`.

    `cmd` is an argv list (NOT a shell-interpreted string) to avoid the
    classic shell-injection foot-gun. `cwd` accepts the `${workspace}`
    template variable which the runner expands to the DAG workspace dir.
    """

    model_config = ConfigDict(extra="forbid")

    cmd: List[str] = Field(..., min_length=1)
    cwd: str = "${workspace}"
    env: Dict[str, str] = Field(default_factory=dict)
    success_when: ShellSuccessWhen = Field(default_factory=ShellSuccessWhen)


class WebhookSuccessWhen(BaseModel):
    """Success criterion for a webhook hook.

    `status_code` is always checked. If both `json_path` and `equals` are
    provided, the JSON body must additionally match. `json_path` is a
    minimal `$.foo.bar` style path (no expression language — kept tiny on
    purpose; expr-eval is for DAG edge conditions).
    """

    model_config = ConfigDict(extra="forbid")

    status_code: int = 200
    json_path: Optional[str] = None
    equals: Any = None


class WebhookHookConfig(BaseModel):
    """POST/GET an HTTP endpoint; treat response per `success_when`.

    `headers` / `body_template` may interpolate `${workspace}`, `${team_id}`,
    `${results_json}`, and `${credential.<name>}` placeholders (resolved by
    the runner against the Python `/api/settings` Fernet store; see H5 in
    the design doc).
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: Dict[str, str] = Field(default_factory=dict)
    body_template: Optional[str] = None
    success_when: WebhookSuccessWhen = Field(default_factory=WebhookSuccessWhen)


class BuiltinHookConfig(BaseModel):
    """Reference to an in-process validator (see §9 seed list).

    `name` is the registry key (`file-exists`, `tsc-check`, …); `args` is
    the validator-specific JSON blob (schema enforced by the validator
    registry in N1/004, not here, so this module stays decoupled).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    args: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level HookSpec
# ---------------------------------------------------------------------------


HookKind = Literal["shell", "webhook", "builtin"]
HookOnFail = Literal["retry", "blocker", "warn"]


class ValidationHookSpec(BaseModel):
    """One validation hook entry inside `team.validation_hooks: [...]`.

    Exactly ONE of `shell` / `webhook` / `builtin` MUST be set, matching
    the `kind` discriminator. The validator below enforces this contract
    so a malformed YAML (`kind=shell` with no `shell:` block, or
    `kind=builtin` with both `builtin:` and `webhook:` set) gets rejected
    at load time rather than at runtime.

    Field invariants:

    * `id` is unique within a team (the API endpoint enforces uniqueness;
      this model does not, so a partial unit-test caller can build single
      specs without round-tripping through the team record).
    * `on_fail="retry"` only makes sense if `max_retries > 0`; otherwise it
      is functionally identical to `blocker` (PM decision Q12.2 — retry
      budget is independent of per-node retry).
    * `expose_error_details=True` opts into shipping upstream-node
      `error.message` + `error.stack` into the hook template payload;
      default `False` redacts them per PM decision Q12.3.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    kind: HookKind
    on_fail: HookOnFail = "blocker"
    enabled: bool = True
    description: str = ""
    timeout_ms: int = Field(60_000, gt=0, le=600_000)

    # PM Q12.2: turn-level budget, disjoint from per-node retry. Default 0
    # = no retry; users must opt in. Only meaningful for idempotent checks.
    max_retries: int = Field(0, ge=0, le=10)

    # PM Q12.3: opt-in flag for shipping unredacted error.message/stack
    # into the hook template payload. Default False (redacted).
    expose_error_details: bool = False

    # Discriminated config (exactly one of these per `kind`).
    shell: Optional[ShellHookConfig] = None
    webhook: Optional[WebhookHookConfig] = None
    builtin: Optional[BuiltinHookConfig] = None

    @model_validator(mode="after")
    def _enforce_kind_discriminator(self) -> "ValidationHookSpec":
        configs = {
            "shell": self.shell,
            "webhook": self.webhook,
            "builtin": self.builtin,
        }
        present = {k for k, v in configs.items() if v is not None}
        if self.kind not in present:
            raise ValueError(
                f"hook {self.id!r}: kind={self.kind!r} requires a {self.kind!r}: "
                f"config block"
            )
        extra = present - {self.kind}
        if extra:
            raise ValueError(
                f"hook {self.id!r}: kind={self.kind!r} but unrelated config "
                f"blocks present: {sorted(extra)}"
            )
        return self


# ---------------------------------------------------------------------------
# Collection wrapper (used by GET/PUT endpoint body and team.yaml top-level)
# ---------------------------------------------------------------------------


class ValidationHooksConfig(BaseModel):
    """Wrapper used by `PUT /api/teams/{id}/validation-hooks` request body.

    Stored on the team record as `record["validation_hooks"] = [...]` (a
    raw list — the wrapper is only for transport / validation symmetry
    with `TeamWorkflow` and `TeamPolicyRequest`).
    """

    model_config = ConfigDict(extra="forbid")

    validation_hooks: List[ValidationHookSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_unique_ids(self) -> "ValidationHooksConfig":
        seen: set[str] = set()
        for hook in self.validation_hooks:
            if hook.id in seen:
                raise ValueError(
                    f"duplicate hook id {hook.id!r} (each hook.id must be "
                    f"unique within team.validation_hooks)"
                )
            seen.add(hook.id)
        return self


__all__ = [
    "ShellHookConfig",
    "ShellSuccessWhen",
    "WebhookHookConfig",
    "WebhookSuccessWhen",
    "BuiltinHookConfig",
    "HookKind",
    "HookOnFail",
    "ValidationHookSpec",
    "ValidationHooksConfig",
]
