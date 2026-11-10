/**
 * conversation-runtime.ts — Transport-layer types + re-exports.
 *
 * Houses the Transport-layer contract types (ApiClient, AssistantEvent,
 * ToolExecutor, RuntimeSession, SseEvent, addUsage helper) consumed by
 * `transport/api-clients/*-api-client.ts`. The `ConversationRuntime` class
 * itself lives in `conversation-runtime-impl.ts` and is re-exported below
 * so existing import sites (`./conversation-runtime`) keep working.
 *
 * History:
 *   - Phase 2 (2026-05-22): the multi-turn tool_use driver was removed when
 *     daemon-led DAG + artifact handoff became the primary orchestration
 *     path. Only types survived.
 *   - Round 4 PR-D (2026-05-26): single-agent tool-use is back as a
 *     first-class flow (compiled `agentConfig.tools[]` from PR-C). The
 *     ConversationRuntime class was restored in a sibling module.
 *
 * If you came here looking for *multi-agent* orchestration, that still
 * lives in `workflow/scheduler.ts` + `workflow/executor.ts`. PR-D's runtime
 * is for the single-agent branch and per-node LLM calls.
 */

import type { ConversationMessage, TokenUsage } from './conversation-types';
import type { ToolSpec } from './tool-spec';

// ─── Public interfaces ─────────────────────────────────────────────────────

/**
 * AssistantEvent — what the ApiClient yields per LLM stream chunk. Aligned
 * with Anthropic Messages API stream events but normalized to our internal
 * kinds (matching the Rust port's `AssistantEvent` enum).
 */
export type AssistantEvent =
  | { kind: 'text_delta'; text: string }
  // P1 (audit/thinking-transport-fix-plan-2026-05-27.md): extended-thinking
  // content, streamed before the answer. Display-only — not folded into the
  // assistant message history (see conversation-runtime-impl.ts).
  | { kind: 'thinking_delta'; text: string }
  | { kind: 'tool_use'; id: string; name: string; input: unknown }
  | { kind: 'usage'; usage: TokenUsage }
  | {
      kind: 'message_stop';
      /**
       * Anthropic stop_reason. 'end_turn' | 'tool_use' | 'max_tokens' |
       * 'stop_sequence' | other provider strings. We keep it as a free string
       * to stay forward-compatible with new reasons.
       */
      stop_reason: string;
    };

/**
 * ApiClient — abstract LLM streaming contract. One LLM turn = one call to
 * `stream()`. Implementations:
 *
 *   - real Anthropic Messages API streaming (S6)
 *   - GLM / OpenAI provider adapters (S6 follow-ons)
 *   - FakeApiClient (tests, this file's __tests__/)
 *
 * The async iterable MUST terminate eventually (either by yielding a
 * `message_stop` event then returning, or by throwing). Implementations are
 * expected to honor `signal` and abort mid-stream when it fires.
 */
export interface ApiClient {
  stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: ToolSpec[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent>;
}

/**
 * ToolExecutionResult — what one tool returns to the runtime. Identical
 * shape to S4 SkillAnchorTool's ToolExecutionResult; re-declared here to
 * keep this module free of cross-imports from lib/tools/.
 *
 *   - `output` is fed back to the LLM verbatim as the tool_result content.
 *     Non-strings get JSON.stringify'd before push.
 *   - `sseEvents` is the side-channel (D4) — runtime yields each entry
 *     downstream so the wire SSE keeps existing event names (node / edge /
 *     ...). [S6 contract: parser.ts maps <sf:node>→'node' and <sf:edge>→
 *     'edge'; tool side-effects must use the same SSE event names — see
 *     skill-anchors.ts register_agent / register_edge.]
 *   - `isError` flips the tool_result's `is_error` flag so the LLM can
 *     distinguish success from failure.
 */
export interface ToolExecutionResult {
  output: unknown;
  sseEvents?: Array<{ event: string; data: unknown }>;
  isError?: boolean;
}

/**
 * ToolExecutor — abstract tool dispatcher. S6 will wrap the S4 skill-anchor
 * executors and any future per-skill conditional tools into one of these.
 *
 *   - `toolSpecs()` is called once per LLM turn to populate the `tools` arg
 *     to `ApiClient.stream`. Returning a fresh array per turn allows the
 *     executor to add/remove conditional tools mid-conversation.
 *   - `execute(name, input)` is called once per pending tool_use block from
 *     the assistant. May throw — runtime catches and packs into tool_result.
 */
export interface ToolExecutor {
  toolSpecs(): ToolSpec[];
  execute(name: string, input: unknown): Promise<ToolExecutionResult>;
}

/**
 * SseEvent — what runTurn yields. Upstream (S6 SSE handler) writes each one
 * as a single `event: <name>\ndata: <json>\n\n` chunk on the HTTP response.
 */
export interface SseEvent {
  event: string;
  data: unknown;
}

/**
 * Session shape ConversationRuntime owns. Just enough to mutate the rolling
 * message stack. The full server/src/lib/session-store SessionRecord has more
 * fields but the runtime only needs `id` (for the complete event) and
 * `messages` (mutable history). Keeping the contract narrow lets tests use
 * `{id, messages: []}` literals without faking the whole record.
 */
export interface RuntimeSession {
  id: string;
  messages: ConversationMessage[];
}

// ─── Implementation ────────────────────────────────────────────────────────

/**
 * Sum two TokenUsage snapshots, treating `undefined` fields as 0 so partial
 * provider payloads (e.g. GLM only reports output_tokens) compose cleanly.
 * Returns a fresh object — does not mutate args.
 *
 * CLI-only fields (cost_usd / duration_ms / ttft_ms) have special semantics:
 *   - cost_usd / duration_ms: SUM (total cost / total wall-clock across turns
 *     is meaningful). Result stays `undefined` if both sides are `undefined`
 *     so providers that never emit these don't accumulate spurious 0s.
 *   - ttft_ms: LAST-WRITE (b.ttft_ms ?? a.ttft_ms). Time-to-first-token is a
 *     per-turn latency — summing across turns makes no sense. We keep the
 *     most recent non-undefined value so the UI can show "this turn's TTFT".
 */
// ─── PR-D re-export ────────────────────────────────────────────────────────
//
// The class implementation lives in `conversation-runtime-impl.ts`; we
// re-export it here so existing `import { ConversationRuntime } from
// './conversation-runtime'` call sites work after PR-D's restoration.
export {
  ConversationRuntime,
  type ConversationRuntimeOptions,
  type RunTurnArgs,
} from './conversation-runtime-impl';

export function addUsage(a: TokenUsage, b: TokenUsage): TokenUsage {
  const sumOrUndef = (x?: number, y?: number): number | undefined =>
    x === undefined && y === undefined ? undefined : (x ?? 0) + (y ?? 0);
  return {
    input_tokens: (a.input_tokens ?? 0) + (b.input_tokens ?? 0),
    output_tokens: (a.output_tokens ?? 0) + (b.output_tokens ?? 0),
    cache_creation_input_tokens:
      (a.cache_creation_input_tokens ?? 0) + (b.cache_creation_input_tokens ?? 0),
    cache_read_input_tokens:
      (a.cache_read_input_tokens ?? 0) + (b.cache_read_input_tokens ?? 0),
    cost_usd: sumOrUndef(a.cost_usd, b.cost_usd),
    duration_ms: sumOrUndef(a.duration_ms, b.duration_ms),
    ttft_ms: b.ttft_ms ?? a.ttft_ms,
  };
}
