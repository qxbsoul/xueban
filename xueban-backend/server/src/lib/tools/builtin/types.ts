/**
 * types.ts — Builtin tool executor contract.
 *
 * The legacy `ToolExecutor` type from `../skill-anchors.ts` is just
 * `(input: unknown) => Promise<ToolExecutionResult>` — no workspace context.
 * Builtins need a workspace root (for sandboxing) and an AbortSignal (so the
 * runtime can cancel long-running I/O on abort). We define a parallel type
 * here and let the tool-runner (Lane 1) adapt between the two.
 *
 * Keep this file dependency-free — every builtin imports it.
 */

import type { ToolExecutionResult } from '../skill-anchors';

/**
 * Per-call context handed to every builtin executor.
 *
 *  - `workspace`     absolute path to the run's working directory. All path
 *                    arguments resolve relative to this; nothing may escape it.
 *  - `signal`        AbortSignal owned by the ConversationRuntime. Long-running
 *                    tools (shell_exec, fetch_url, grep) MUST honour it.
 *  - `allowedTools`  optional set of skill `allowed-tools` frontmatter names
 *                    (case-sensitive, exactly as authored). High-risk tools
 *                    (shell_exec) inspect this for opt-in.
 *  - `env`           optional environment override (mostly for tests). When
 *                    absent, tools should use a minimal sanitised env.
 */
export interface BuiltinToolContext {
  workspace: string;
  signal: AbortSignal;
  allowedTools?: ReadonlySet<string>;
  env?: Readonly<Record<string, string>>;
}

/**
 * Same result shape as legacy `ToolExecutionResult` (so tool-runner can pass
 * it straight through). `output` is fed back to the LLM as the
 * `tool_result.output` JSON-encoded blob. `isError` flips `is_error` on the
 * Anthropic ContentBlock.
 */
export type BuiltinToolExecutor = (
  input: unknown,
  ctx: BuiltinToolContext,
) => Promise<ToolExecutionResult>;
