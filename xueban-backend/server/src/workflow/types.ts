/**
 * workflow/types.ts — Phase 2 DAG engine type definitions
 *
 * Position in the Orchestration ⊥ Transport architecture:
 *   - This file lives in the **Orchestration** layer (`workflow/`).
 *   - It imports the canonical team/edge shapes from `lib/team-yaml.ts` so
 *     the DAG engine consumes the exact same `team.yaml v1` schema the loader
 *     produces; it deliberately re-uses those types instead of redefining nodes
 *     or edges.
 *   - The `TurnChunk` discriminated union mirrors what the Transport layer
 *     (`LlmCallable.turn()`, Phase 2 decision A1) yields. Orchestration only
 *     ever observes `TurnChunk`s; it never sees Transport-internal shapes.
 *   - `LlmCallError` formalises decision CL3/E3: typed errors thrown by callable
 *     invocations + surfaced as `error` chunks in the stream.
 *
 * Owner: backend chat-flow team. Phase 2 decisions: A1, A4, A4b, C1, CL3/E3.
 */

import type { TeamDefV1, TeamEdgeV1, EdgeKind } from '../lib/team-yaml';

// Re-export team shapes so workflow/* downstream files have a single import surface.
export type { TeamDefV1, TeamEdgeV1, EdgeKind };

// ─── Streaming chunk discriminated union (Phase 2 decision A1) ───────────────

/**
 * Tool call request emitted by a callable that supports tool_use.
 * Shape kept loose because Transport layer owns the canonical definition.
 */
export interface ToolUsePayload {
  tool_name: string;
  tool_input: unknown;
  /** Vendor-specific call id (e.g. Anthropic `toolu_*`). */
  call_id?: string;
}

/**
 * Tool execution result, paired with a prior `ToolUsePayload` by `tool_use_id`.
 *
 * P1 (sse-frame-leak root-cure plan §5): tool calls + results are first-class
 * STRUCTURED chunks, not text re-parsed out of an XML round-trip. ConversationRuntime
 * surfaces these after `toolRunner.dispatch`; `pipeChunksToSse` maps them to the
 * `tool-result` SSE frame the timeline projector already consumes.
 */
export interface ToolResultPayload {
  tool_use_id: string;
  /** Stringified tool output (already flattened by the runtime). */
  output: string;
  is_error?: boolean;
}

/**
 * Token / cost accounting emitted at the end of a callable turn.
 */
export interface UsagePayload {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  /** Optional model id for billing aggregation. */
  model?: string;
}

/**
 * Discriminated union of everything a callable.turn() can yield.
 *
 * `node_id` is optional at the type level so a callable that is unaware of
 * the surrounding DAG (e.g. a one-off `SimpleRuntime`) can yield bare chunks;
 * the scheduler is responsible for stamping `node_id` before re-yielding so
 * the front-end can route per node (parser.ts:286 contract).
 */
export type TurnChunk =
  | { type: 'text-delta'; value: string; node_id?: string }
  // P1: extended-thinking content. Bypasses the parser (it is plain prose, not
  // tagged) and maps to the `thinking-chunk` SSE frame in pipeChunksToSse.
  | { type: 'thinking-delta'; value: string; node_id?: string }
  | { type: 'tool-use'; tool: ToolUsePayload; node_id?: string }
  // P1: structured tool execution result (paired with a prior tool-use by
  // tool_use_id). Maps to the `tool-result` SSE frame in pipeChunksToSse.
  | { type: 'tool-result'; result: ToolResultPayload; node_id?: string }
  | { type: 'error'; error: LlmCallError; node_id?: string }
  | { type: 'usage'; usage: UsagePayload; node_id?: string }
  // Spawner passthrough (②, sse-frame-leak CLI-path root cure, 2026-05-31).
  // CLI/ACP/MCP spawners already parse their stream-json into STRUCTURED
  // Xueban SSE events ({event,data}) via dispatchParser. The bridge used to
  // re-flatten each one into a `text-delta` carrying the literal `event:/data:`
  // wire line — which the downstream text parser then mis-flagged as an
  // sse-frame-leak `raw` block (every <sf:node>/complete became raw → TEAM 0).
  // This chunk carries such an already-structured event through pipeChunksToSse
  // VERBATIM so it reaches the wire as itself. Only the spawner bridge emits it.
  | { type: 'sse'; event: string; data: unknown; node_id?: string }
  | { type: 'done'; node_id?: string };

// ─── Error model (Phase 2 decision CL3/E3) ───────────────────────────────────

/**
 * Coarse-grained failure mode. The Transport layer maps provider-specific
 * errors into one of these so Orchestration can decide on retry behaviour
 * without knowing about HTTP status codes / SDK exception classes.
 */
export type LlmCallErrorKind =
  | 'rate-limit'
  | 'auth'
  | 'timeout'
  | 'cli-crash'
  | 'network'
  | 'context-length'
  | 'provider-error';

/**
 * Typed exception raised by `LlmCallable.turn()` callers when a turn fails
 * before any useful output is produced. See `workflow/retry.ts` for the
 * retry classification of each `kind`.
 */
export class LlmCallError extends Error {
  readonly kind: LlmCallErrorKind;
  /**
   * Optional retry-after hint in **milliseconds** (already normalised from
   * `Retry-After` HTTP header seconds-or-date values). Honoured by
   * `retry.withRetry()` when present.
   */
  readonly retryAfter?: number;
  /** Optional underlying error for debugging / log capture. */
  readonly cause?: unknown;

  constructor(
    kind: LlmCallErrorKind,
    message: string,
    opts: { retryAfter?: number; cause?: unknown } = {},
  ) {
    super(message);
    this.name = 'LlmCallError';
    this.kind = kind;
    this.retryAfter = opts.retryAfter;
    this.cause = opts.cause;
    // Preserve prototype chain across CommonJS / target ES2020.
    Object.setPrototypeOf(this, LlmCallError.prototype);
  }
}

// ─── Per-node execution state (Phase 2 decision A4) ──────────────────────────

/**
 * Lifecycle states a DAG node passes through. Order:
 *   pending → running → (done | failed)
 *   pending → skipped (when an upstream conditional edge evaluates false,
 *                      or an upstream node failed and this one depends on it)
 */
export type NodeStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

/**
 * Result captured after a single node finishes (or is skipped).
 *
 * `artifacts` is the list of absolute file paths the node produced inside
 * the workspace, in declaration order. Downstream nodes read these via
 * `NodeContext.priorResults`.
 *
 * `error` is only set when `status === 'failed'`; it carries the typed
 * Transport-layer error after all retries have been exhausted.
 */
export interface RunResult {
  node_id: string;
  status: NodeStatus;
  artifacts: string[];
  error?: LlmCallError;
  /** Wall-clock duration in ms, including retries. 0 for skipped nodes. */
  durationMs: number;
}

/**
 * Context handed to a node executor and to the condition evaluator.
 *
 * `priorResults` is keyed by `node_id` and only contains results for nodes
 * that have *finished* (status ∈ {done, failed, skipped}) before this node
 * starts. The scheduler is responsible for maintaining the invariant.
 */
export interface NodeContext {
  team: TeamDefV1;
  node_id: string;
  /**
   * Absolute path to the per-session workspace directory; artifacts are
   * written here (and read from here by downstream nodes). The Phase 2 contract
   * (decision A2) is artifact-file handoff, not in-memory message log.
   */
  workspace: string;
  priorResults: Map<string, RunResult>;
  /**
   * Optional user goal — Branch 0 (recipe) path threads the original user goal
   * through `runDag` so the per-node executor can use it as the user-turn
   * payload (`prompt`). When ABSENT (BMAD / legacy team paths) the executor
   * falls back to `node.id`, preserving the exact pre-goal behaviour.
   */
  goal?: string;
  /**
   * 阶段 1 — run-session id, threaded from the assembler so per-node tool
   * approval (`permission_check`) frames land in the right run event log.
   * ABSENT for tests / library callers: the executor then skips the prompter
   * and 'prompt' tools keep the v1 fail-closed soft-deny.
   */
  session_id?: string;
}
