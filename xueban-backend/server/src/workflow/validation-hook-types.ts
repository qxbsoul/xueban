/**
 * workflow/validation-hook-types.ts — N1/002
 *
 * TypeScript twin of `xueban/runtime/validation_hooks/schema.py`. The
 * Python module is the **canonical** schema (Team CRUD persistence happens
 * there); this file mirrors the field shape so the Node-side scheduler
 * (N1/003) and validator runner (N1/004) consume the exact same payload
 * the Python API hands back via `GET /api/teams/{id}/validation-hooks`.
 *
 * Sync discipline: when you change a field, you MUST update both files
 * (no codegen yet — keep diffs in lock-step in the same commit). The
 * Python `ValidationHookSpec` validator is what protects the wire; this
 * file is just structural typing for the Node consumers.
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - Hooks are a post-DAG concern executed by the Orchestration layer
 *     (`workflow/scheduler.ts` driver, after all nodes reach a terminal
 *     state — see the design doc §H1/H2). They never touch transport.
 *   - This file only declares types. The runner lives in
 *     `workflow/hooks/runner.ts` (added in N1/003), and is what the
 *     scheduler will invoke. We deliberately do NOT import scheduler /
 *     executor here so the type layer stays cycle-free.
 *
 * PM decisions reflected:
 *   - Q12.1: validation_hooks lives at the top level of TeamDefV1, parallel
 *            to `policy_obj`. See `team-yaml.ts` for the matching loader
 *            field (added under the same task).
 *   - Q12.2: `on_fail: "retry"` reruns ONLY the hook (no DAG rollback);
 *            `max_retries` default 0. Retry is intended for idempotent
 *            "wait + check" probes, not for "fix-and-rerun".
 *   - Q12.3: `expose_error_details` (default false) gates whether the
 *            runner ships unredacted upstream `error.message` / stack into
 *            the hook template payload.
 */

/** Three supported hook execution modes (matches Python `HookKind`). */
export type HookKind = 'shell' | 'webhook' | 'builtin';

/**
 * What to do when a hook returns a `failed` verdict (matches Python
 * `HookOnFail`):
 *   - `retry`   — rerun only the hook up to `max_retries` more times
 *                 (turn-level budget; disjoint from per-node retry).
 *   - `blocker` — mark the turn `failed`, emit SSE error chunk, close stream.
 *   - `warn`    — emit SSE `validation` chunk with status `failed` and
 *                 continue (next hook still runs; turn ends `done`).
 */
export type HookOnFail = 'retry' | 'blocker' | 'warn';

/** Shell-hook success criterion (default exit_code == 0). */
export interface ShellSuccessWhen {
  exit_code: number;
}

/** Spawn a child process; success governed by `success_when.exit_code`. */
export interface ShellHookConfig {
  /** Argv list. NOT shell-interpreted — no `/bin/sh -c` indirection. */
  cmd: string[];
  /** Working directory; `${workspace}` is expanded by the runner. */
  cwd: string;
  env: Record<string, string>;
  success_when: ShellSuccessWhen;
}

/** Webhook-hook success criterion. status_code is always checked. */
export interface WebhookSuccessWhen {
  status_code: number;
  /** Optional `$.foo.bar` JSON path for an additional body assertion. */
  json_path?: string;
  /** Value the json_path must equal. Type is intentionally loose (number / string / boolean). */
  equals?: unknown;
}

/**
 * POST/GET an HTTP endpoint and treat the response per `success_when`.
 *
 * `headers` / `body_template` may interpolate `${workspace}`, `${team_id}`,
 * `${results_json}`, and `${credential.<name>}` placeholders, resolved by
 * the runner against the Python `/api/settings` Fernet store (design doc H5).
 */
export interface WebhookHookConfig {
  url: string;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  headers: Record<string, string>;
  body_template?: string;
  success_when: WebhookSuccessWhen;
}

/**
 * Reference to an in-process validator. `name` is the registry key
 * (`file-exists`, `tsc-check`, …). The registry shipped in N1/004 is what
 * enforces the `args` shape; here it's an open record.
 */
export interface BuiltinHookConfig {
  name: string;
  args: Record<string, unknown>;
}

/**
 * One validation hook entry. Exactly ONE of `shell` / `webhook` /
 * `builtin` is set, keyed to `kind`. The Python schema enforces this with
 * a `model_validator`; the TypeScript twin uses an optional union here and
 * relies on the runtime payload being Python-validated before reaching
 * Node (the Python API is the only writer for the persisted form).
 *
 * Field invariants (mirrored from `ValidationHookSpec` in Python):
 *   - `id` is unique within `team.validation_hooks`.
 *   - `on_fail: "retry"` only makes sense if `max_retries > 0`; otherwise
 *     it is functionally identical to `blocker` (turn-level retry budget
 *     is independent of per-node retry — see `scheduler.ts:139`).
 *   - `expose_error_details: true` opts into shipping unredacted upstream
 *     node `error.message` / `error.stack` into the hook template payload;
 *     default `false` redacts them (PM decision Q12.3).
 */
export interface ValidationHookSpec {
  id: string;
  kind: HookKind;
  on_fail: HookOnFail;
  enabled: boolean;
  description: string;
  timeout_ms: number;
  /** Turn-level retry budget. 0 = disabled. See PM decision Q12.2. */
  max_retries: number;
  /** Opt-in for unredacted error details in hook payload. PM Q12.3. */
  expose_error_details: boolean;
  shell?: ShellHookConfig;
  webhook?: WebhookHookConfig;
  builtin?: BuiltinHookConfig;
}

/**
 * Type guard helpers — used by the runner (N1/003) to narrow the kind
 * union without losing type-safety. Kept here so all schema awareness
 * lives in one file.
 */
export function isShellHook(
  hook: ValidationHookSpec,
): hook is ValidationHookSpec & { shell: ShellHookConfig } {
  return hook.kind === 'shell' && hook.shell !== undefined;
}

export function isWebhookHook(
  hook: ValidationHookSpec,
): hook is ValidationHookSpec & { webhook: WebhookHookConfig } {
  return hook.kind === 'webhook' && hook.webhook !== undefined;
}

export function isBuiltinHook(
  hook: ValidationHookSpec,
): hook is ValidationHookSpec & { builtin: BuiltinHookConfig } {
  return hook.kind === 'builtin' && hook.builtin !== undefined;
}
