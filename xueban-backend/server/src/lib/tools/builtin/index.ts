/**
 * builtin/index.ts — Default registry for the 17 ConversationRuntime builtins.
 *
 * 2026-11-07 — WorkBuddy 平台能力补齐: +8 provider-backed platform tools
 * (send_email / im_read / im_send / github_request / github_write /
 * image_generate / video_generate / generate_3d), all following web-search's
 * env-provider + stub contract (see provider-http.ts).
 *
 * Exported pieces:
 *   - `builtinTools`                 ToolSpec[]    insertion-ordered list
 *   - `builtinExecutors`             Record<name, BuiltinToolExecutor>
 *   - `builtinDefaultModes`          Record<name, PermissionMode>
 *   - `createBuiltinRegistry()`      factory returning a fresh ToolRegistry
 *                                    seeded with all 9 ToolSpec entries.
 *   - `registerBuiltinExecutors()`   register the executors against Lane 1's
 *                                    module-level executor registry. The
 *                                    workspace + allowedTools context is
 *                                    threaded via AsyncLocalStorage — the
 *                                    runtime owns setting that scope per
 *                                    conversation turn (`runWithBuiltinContext`).
 *
 * Bridge to Lane 1
 * ────────────────
 * Lane 1's `tool-runner.ts` defines a simpler `ToolExecutor` shape:
 *   `{ execute(input: unknown, signal: AbortSignal): Promise<ToolExecResult> }`
 * It has no workspace argument. Our builtin executors NEED a workspace (they
 * sandbox every path against it). We bridge by:
 *
 *   1. Storing the per-turn context (`workspace`, `allowedTools`) in an
 *      AsyncLocalStorage that the ConversationRuntime sets before calling
 *      `ToolRunner.dispatch()`.
 *   2. Registering an adapter shim for each builtin that pulls context from
 *      ALS, then calls the BuiltinToolExecutor with `(input, { workspace,
 *      signal, allowedTools })`.
 *   3. Falling back to a clearly-labelled isError result if ALS is empty —
 *      this is the "Lane 1 forgot to wrap dispatch in runWithBuiltinContext"
 *      diagnostic.
 *
 * Ordering rationale (matches Claude Code's manifest):
 *   read-tier (safe-by-default), then write-tier, then shell_exec last.
 */

import { AsyncLocalStorage } from 'async_hooks';
import type { ToolSpec } from '../../tool-spec';
import { ToolRegistry } from '../../tool-spec';
import type { PermissionMode } from '../../permission-policy';
import type { BuiltinToolExecutor } from './types';

import { readFileTool, readFileExecutor } from './read-file';
import { listDirTool, listDirExecutor } from './list-dir';
import { globFilesTool, globFilesExecutor } from './glob-files';
import { grepTool, grepExecutor } from './grep';
import { webSearchTool, webSearchExecutor } from './web-search';
import { fetchUrlTool, fetchUrlExecutor } from './fetch-url';
import { imReadTool, imReadExecutor } from './im-read';
import { githubRequestTool, githubRequestExecutor, githubWriteTool, githubWriteExecutor } from './github';
import { writeFileTool, writeFileExecutor } from './write-file';
import { editFileTool, editFileExecutor } from './edit-file';
import { sendEmailTool, sendEmailExecutor } from './send-email';
import { imSendTool, imSendExecutor } from './im-send';
import { imageGenerateTool, imageGenerateExecutor } from './image-generate';
import { videoGenerateTool, videoGenerateExecutor } from './video-generate';
import { generate3dTool, generate3dExecutor } from './three-d-generate';
import { shellExecTool, shellExecExecutor } from './shell-exec';

export const builtinTools: ToolSpec[] = [
  // read-tier (safe-by-default)
  readFileTool,
  listDirTool,
  globFilesTool,
  grepTool,
  webSearchTool,
  fetchUrlTool,
  imReadTool,
  githubRequestTool,
  // write-tier
  writeFileTool,
  editFileTool,
  sendEmailTool,
  imSendTool,
  githubWriteTool,
  // generation-tier (costly providers)
  imageGenerateTool,
  videoGenerateTool,
  generate3dTool,
  // shell last
  shellExecTool,
];

export const builtinExecutors: Record<string, BuiltinToolExecutor> = {
  read_file: readFileExecutor,
  list_dir: listDirExecutor,
  glob_files: globFilesExecutor,
  grep: grepExecutor,
  web_search: webSearchExecutor,
  fetch_url: fetchUrlExecutor,
  im_read: imReadExecutor,
  github_request: githubRequestExecutor,
  write_file: writeFileExecutor,
  edit_file: editFileExecutor,
  send_email: sendEmailExecutor,
  im_send: imSendExecutor,
  github_write: githubWriteExecutor,
  image_generate: imageGenerateExecutor,
  video_generate: videoGenerateExecutor,
  generate_3d: generate3dExecutor,
  shell_exec: shellExecExecutor,
};

/**
 * Canonical defaults. `'prompt'` means tool-runner holds the call, emits a
 * `permission_check` frame (pending → decision via the tool-approval registry,
 * 阶段 1), and only runs on explicit user approval; timeout/abort settle as
 * deny (fail-closed). When no prompter is wired (legacy call sites / unit
 * tests), tool-runner downgrades `'prompt'` → deny + notify SSE (v1 behaviour).
 */
export const builtinDefaultModes: Record<string, PermissionMode> = {
  read_file: 'allow',
  list_dir: 'allow',
  glob_files: 'allow',
  grep: 'allow',
  web_search: 'allow',
  fetch_url: 'allow',
  im_read: 'allow',
  github_request: 'allow',
  write_file: 'prompt',
  edit_file: 'prompt',
  send_email: 'prompt',
  im_send: 'prompt',
  github_write: 'prompt',
  image_generate: 'allow',
  video_generate: 'prompt',
  generate_3d: 'prompt',
  shell_exec: 'deny',
};

/**
 * 阶段 1 — lift ONLY the `'prompt'` entries of `builtinDefaultModes` for tools
 * present in a skill's whitelist. Deliberately ignores the `'deny'` default
 * (shell_exec): a tool the skill author explicitly listed keeps today's
 * listed→allow semantics — this helper adds approval gates, never new denials.
 * The deny-by-default floor for UNLISTED tools already comes from
 * `PermissionPolicyV2` itself.
 */
export function promptDefaultsForTools(
  names: readonly string[],
): Record<string, PermissionMode> {
  const out: Record<string, PermissionMode> = {};
  for (const n of names) {
    if (builtinDefaultModes[n] === 'prompt') out[n] = 'prompt';
  }
  return out;
}

/** Fresh ToolRegistry seeded with the canonical builtin order. */
export function createBuiltinRegistry(): ToolRegistry {
  return new ToolRegistry(builtinTools);
}

// ─── Bridge to Lane 1's module-level executor registry ──────────────────────

/**
 * Per-call context that the runtime sets via `runWithBuiltinContext` before
 * dispatching a tool. Pulled out of ALS by each executor shim.
 */
export interface BuiltinExecutorScope {
  workspace: string;
  allowedTools?: ReadonlySet<string>;
}

const _builtinAls = new AsyncLocalStorage<BuiltinExecutorScope>();

/**
 * Wrap a runtime turn so all builtin executors see the right workspace.
 *
 *   await runWithBuiltinContext({ workspace, allowedTools }, () =>
 *     runner.dispatch(toolUse, signal)
 *   );
 *
 * The ConversationRuntime owns this — call it once per `tool_use` block.
 */
export function runWithBuiltinContext<T>(
  scope: BuiltinExecutorScope,
  fn: () => Promise<T> | T,
): Promise<T> | T {
  return _builtinAls.run(scope, fn);
}

/**
 * Register all builtin executors against Lane 1's tool-runner module
 * registry. Idempotent — safe to call multiple times (last write wins per
 * tool name).
 *
 * `registerFn` matches the signature of `tool-runner.ts`'s
 * `registerToolExecutor(name, executor)`. We accept it by injection instead
 * of importing to avoid a hard module dependency in case Lane 1's file path
 * shifts during the merge — the runtime just hands us the function.
 */
export function registerBuiltinExecutors(
  registerFn: (
    name: string,
    executor: { execute(input: unknown, signal: AbortSignal): Promise<{ output: unknown; isError?: boolean; sseEvents?: Array<{ event: string; data: unknown }> }> },
  ) => void,
): void {
  for (const [name, exec] of Object.entries(builtinExecutors)) {
    registerFn(name, {
      async execute(input: unknown, signal: AbortSignal) {
        const scope = _builtinAls.getStore();
        if (!scope) {
          return {
            output: {
              error: `builtin '${name}' invoked outside runWithBuiltinContext scope; ConversationRuntime must wrap each turn`,
            },
            isError: true,
          };
        }
        return exec(input, {
          workspace: scope.workspace,
          signal,
          allowedTools: scope.allowedTools,
        });
      },
    });
  }
}

export type { BuiltinToolExecutor, BuiltinToolContext } from './types';
