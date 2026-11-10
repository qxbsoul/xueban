/**
 * workflow/executor.ts — Single-node executor
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - Orchestration: composes the per-node system prompt (persona + read-this
 *     artifact slurp), invokes the Transport-layer `LlmCallable.turn()`,
 *     captures the streamed `TurnChunk`s into a `RunResult`, and writes the
 *     node's output artifact(s) into the workspace for downstream nodes.
 *   - Transport: never imported here as a value — only `import type` from
 *     `../transport/LlmCallable` so this module stays compilable while the
 *     transport package is in flight (Phase 2 parallel work).
 *
 * Phase 2 decisions touched:
 *   - A2 artifact-file handoff (this is where files actually land on disk)
 *   - A3 daemon-led DAG (no LLM tool_use; the daemon stitches each agent's
 *     prompt directly from persona + io spec)
 *   - C1 AbortSignal cascade (forwarded into callable.turn())
 *   - CL3/E3 typed errors → error chunk + RunResult.error
 */

import fs from 'fs';
import path from 'path';
import type { SkillAgentDef } from '../lib/skill-types';
import type { NodeContext, RunResult, TurnChunk } from './types';
import { LlmCallError } from './types';
import type { NodeObserver } from './observer';

// `LlmCallable` interface is being authored by a sibling agent in
// `../transport/LlmCallable.ts`. We rely on `import type` so this file
// compiles even when the module resolution fails (allowed per Phase 2 DoD).
//
// The minimal shape we depend on (Phase 2 doc §2):
//
//   interface LlmCallable {
//     turn(input: {
//       system: string;
//       prompt: string;
//       history: ConversationMessage[];
//       tools?: ToolSpec[];
//       signal?: AbortSignal;
//     }): AsyncGenerator<TurnChunk>;
//     readonly capabilities: {
//       supportsToolUse: boolean;
//       supportsMultiTurn: boolean;
//       supportsStreamingDelta: boolean;
//     };
//   }
import type { LlmCallable } from '../transport/LlmCallable';
import { ApiClientCallable } from '../transport/ApiClientCallable';
// Round 4 PR-D Lane 1 — per-agent ConversationRuntime path. When the agent
// declares a tool whitelist AND the callable wraps a direct ApiClient,
// we drive the agent through the multi-turn tool_use loop. Otherwise the
// existing single-shot `callable.turn()` path runs (CLI / ACP / MCP
// backed transports keep handling tool loops internally).
import { ConversationRuntime } from '../lib/conversation-runtime';
import { ToolRunner } from '../lib/tool-runner';
import { PermissionPolicyV2 } from '../lib/permission-policy-v2';
import { ToolRegistry } from '../lib/tool-spec';
import { toolApprovals } from '../lib/tool-approval';
import { promptDefaultsForTools } from '../lib/tools/builtin';
// 经验记忆召回（2026-11-05，整合 XUEBAN-OS PR#3 memory.py 设计）— 失败静默。
// 2026-11-07 — 开关/条数/格式统一走 storage/memory-lessons.promptBlock。
import { promptBlock } from '../storage/memory-lessons';

/** 拼该 agent 域的经验记忆块。空（无记忆/开关关）返回 ''。绝不抛错。 */
function lessonsBlockFor(agentId: string): string {
  return promptBlock(agentId, '');
}

// ─── Workspace I/O ───────────────────────────────────────────────────────────

/** Sanitise a filename component coming from `io.inputs.expects` keys. */
function safeRel(p: string): string {
  // strip leading slashes / drive letters / `..` segments
  const cleaned = p.replace(/^[A-Za-z]:[\\/]+/, '').replace(/^[\\/]+/, '');
  const segs = cleaned.split(/[\\/]+/).filter((s) => s && s !== '..' && s !== '.');
  return segs.join(path.sep);
}

/**
 * Read every file declared in `agent.io.inputs.expects` from the workspace
 * and return a single concatenated string with file-header markers.
 *
 * Missing files are tolerated (a node may run before its upstream produces
 * the expected file, or upstream may have failed); the marker still appears
 * so the agent can notice the absence.
 */
function slurpExpectedInputs(agent: SkillAgentDef, workspace: string): string {
  const expects = agent.io?.inputs?.expects ?? {};
  const entries = Object.entries(expects);
  if (entries.length === 0) return '';

  const chunks: string[] = ['', '## Inputs (read from workspace)', ''];
  for (const [name, descriptor] of entries) {
    const rel = safeRel(name);
    const abs = path.join(workspace, rel);
    let body: string;
    try {
      body = fs.readFileSync(abs, 'utf-8');
    } catch {
      body = `(missing — ${descriptor || 'no description'})`;
    }
    chunks.push(`### ${rel}`);
    chunks.push('');
    chunks.push(body);
    chunks.push('');
  }
  return chunks.join('\n');
}

/**
 * Determine the artifact path(s) this node will write, based on
 * `agent.io.outputs.produces`. If absent, default to `<node_id>.md` so every
 * node still leaves a trace for downstream condition expressions.
 */
function plannedArtifactPaths(agent: SkillAgentDef, workspace: string): string[] {
  const produces = agent.io?.outputs?.produces ?? {};
  const keys = Object.keys(produces);
  if (keys.length === 0) return [path.join(workspace, `${agent.id}.md`)];
  return keys.map((k) => path.join(workspace, safeRel(k)));
}

/** Compose the agent's full system prompt (persona + workspace inputs + 经验记忆). */
function composeSystemPrompt(agent: SkillAgentDef, workspace: string): string {
  const persona = agent.persona ?? '';
  const inputs = slurpExpectedInputs(agent, workspace);
  // Append a brief instruction about where to direct the produced output;
  // the executor itself writes the file after streaming completes.
  const outHint =
    Object.keys(agent.io?.outputs?.produces ?? {}).length > 0
      ? `\n\n## Output\n\nProduce the content for: ${Object.keys(agent.io!.outputs!.produces!).join(', ')}`
      : '';
  // 经验记忆召回（2026-11-05，整合 XUEBAN-OS PR#3 memory.py 设计）：按 agent id
  // 分域取该域沉淀的教训/偏好，拼进 persona 之后。失败静默——记忆是加分项。
  const lessons = lessonsBlockFor(agent.id);
  return [persona, lessons, inputs, outHint].filter(Boolean).join('\n');
}

// ─── Single node execution ───────────────────────────────────────────────────

/**
 * Execute a single DAG node end-to-end.
 *
 * Steps:
 *   1. Notify observer.onNodeStart(node_id)
 *   2. Compose system prompt = persona + slurped expected inputs
 *   3. callable.turn({ system, prompt: agent.id, history: [], signal })
 *   4. For each chunk: stamp node_id, forward to observer.onNodeChunk
 *      Accumulate text-delta values for artifact write
 *      Record first error chunk (still allow stream to finish)
 *   5. Write accumulated text to planned artifact path(s)
 *      (single file: one path → full text; multiple: same text into each,
 *       which is a Phase 2 simplification — multi-output agents will land
 *       in Phase 3 with a parser that splits sections)
 *   6. Build RunResult, observer.onNodeEnd
 *
 * Throws are converted to `RunResult { status: 'failed', error }`; the
 * function itself does NOT throw (caller relies on this to keep the
 * scheduler simple — failures are values, not exceptions).
 *
 * Note: `callable` may itself throw an `LlmCallError` (e.g. auth) before
 * yielding any chunk. The scheduler is expected to wrap this call in
 * `retry.withRetry()`; that's where the throw → retry happens. By the time
 * `executeNode` is called via retry, throws are still possible — we catch
 * here as a final safety net.
 */
export async function executeNode(
  node: SkillAgentDef,
  ctx: NodeContext,
  callable: LlmCallable,
  observer: NodeObserver,
  signal: AbortSignal,
): Promise<RunResult> {
  const startedAt = Date.now();
  observer.onNodeStart(node.id);

  // Make sure workspace dir exists; per-node artifacts may be written into
  // subdirectories so we create the dirs lazily before each write.
  try {
    fs.mkdirSync(ctx.workspace, { recursive: true });
  } catch { /* tolerated; later write will surface a clearer error */ }

  const system = composeSystemPrompt(node, ctx.workspace);
  const collectedText: string[] = [];
  let firstError: LlmCallError | undefined;

  try {
    // Round 4 PR-D Lane 1: per-agent ConversationRuntime when the agent
    // advertises a tool whitelist AND the transport exposes a direct
    // ApiClient. Otherwise fall back to the single-shot callable.turn()
    // path (CLI / ACP / MCP backed transports manage their own tool
    // loops; we don't try to re-host their loops here).
    const pickedTools = node.tools?.picked ?? [];
    // Branch 0 recipe path threads `ctx.goal`; when ABSENT (BMAD / legacy team
    // paths) we fall back to `node.id`, byte-for-byte preserving the pre-goal
    // behaviour. The `?? node.id` is the BMAD-isolation flag.
    const userTurn = ctx.goal ?? node.id;
    const stream =
      pickedTools.length > 0 && callable instanceof ApiClientCallable
        ? buildRuntimeStream(callable, node, system, pickedTools, signal, userTurn, ctx.session_id)
        : callable.turn({
            system,
            prompt: userTurn, // Branch 0: user goal; else node.id (BMAD). The
                            // persona + input slurp does the heavy lifting via `system`.
            history: [],     // Phase 2 decision A2: no history; artifact handoff.
            signal,
          });

    // `LlmCallable.turn()` now yields canonical `TurnChunk` (post-Lane 1
    // placeholder removal). We still re-stamp `node_id` here because a
    // callable unaware of the surrounding DAG is allowed to yield bare
    // chunks; the scheduler/executor is the authoritative source of node_id
    // on the SSE wire (parser.ts:286 contract).
    for await (const raw of stream) {
      // Cascade abort: stop forwarding chunks once cancelled. The underlying
      // callable should be honouring `signal` too and will end the stream
      // shortly after.
      if (signal.aborted) break;

      const stamped: TurnChunk = { ...raw, node_id: node.id };
      observer.onNodeChunk(node.id, stamped);

      if (stamped.type === 'text-delta') {
        collectedText.push(stamped.value);
      } else if (stamped.type === 'error' && !firstError) {
        firstError = stamped.error;
      }
    }
  } catch (err) {
    const wrapped =
      err instanceof LlmCallError
        ? err
        : new LlmCallError('provider-error', (err as Error)?.message ?? String(err), { cause: err });
    firstError = wrapped;
  }

  // Write artifacts (best-effort; failure to write becomes a failed node).
  const written: string[] = [];
  if (!firstError && !signal.aborted) {
    const fullText = collectedText.join('');
    const targets = plannedArtifactPaths(node, ctx.workspace);
    try {
      for (const t of targets) {
        fs.mkdirSync(path.dirname(t), { recursive: true });
        fs.writeFileSync(t, fullText, 'utf-8');
        written.push(t);
      }
    } catch (err) {
      firstError = new LlmCallError(
        'provider-error',
        `failed to write artifact: ${(err as Error).message}`,
        { cause: err },
      );
    }
  }

  const result: RunResult = {
    node_id: node.id,
    status: signal.aborted
      ? 'failed'
      : firstError
        ? 'failed'
        : 'done',
    artifacts: written,
    error: firstError,
    durationMs: Date.now() - startedAt,
  };

  observer.onNodeEnd(node.id, result);
  return result;
}

/**
 * Build a `ConversationRuntime`-driven chunk stream for one node. Pulled out
 * of `executeNode()` to keep the per-node hot path readable.
 *
 * The runtime here is single-use (one runTurn() call per node, matching the
 * Phase 2 artifact-handoff contract — no rolling cross-node history). The
 * tool whitelist comes from `node.tools.picked` verbatim; permission policy
 * is deny-by-default + whitelist allowed. 阶段 1: write-tier builtin defaults
 * ('prompt') become approval gates, and when `sessionId` is threaded from the
 * assembler the prompter parks on the shared `toolApprovals` registry so the
 * decision round trip frames the session's run log. Without a sessionId
 * (tests / library callers) the prompter is skipped and 'prompt' tools keep
 * the v1 fail-closed soft-deny.
 */
function buildRuntimeStream(
  callable: ApiClientCallable,
  node: SkillAgentDef,
  system: string,
  pickedTools: string[],
  signal: AbortSignal,
  userTurn: string,
  sessionId?: string,
): AsyncGenerator<TurnChunk> {
  const apiClient = callable.getApiClient();
  const registry = new ToolRegistry(
    pickedTools.map((name) => ({
      name,
      description: `agent tool ${name}`,
      input_schema: { type: 'object', properties: {} },
      source: 'base' as const,
    })),
  );
  const policy = new PermissionPolicyV2({
    allowedTools: pickedTools,
    defaults: promptDefaultsForTools(pickedTools),
  });
  const runner = new ToolRunner(registry, policy, {
    prompter: sessionId
      ? (toolUse, promptSignal) =>
          toolApprovals.request(
            sessionId,
            toolUse,
            `tool '${toolUse.name}' requires human approval`,
            promptSignal,
          )
      : undefined,
  });
  const runtime = new ConversationRuntime({
    apiClient,
    toolRunner: runner,
    // Phase 2 + PR-D: default 50. Agents that need more should override via
    // a future per-agent `max_iterations` field.
    maxIterations: 50,
  });
  return runtime.runTurn({
    system_prompt: system,
    user_message: userTurn, // ctx.goal ?? node.id — mirrors callable.turn()'s prompt: see executeNode().
    history: [],
    signal,
  });
}
