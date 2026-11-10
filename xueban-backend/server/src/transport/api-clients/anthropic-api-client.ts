/**
 * anthropic-api-client.ts — ApiClient (S5) implementation backed by the
 * Anthropic Messages API.
 *
 * S6 (skill-team-conversion-design-v1.md §5 line 806-815) — thin adapter that
 * wires the ConversationRuntime ApiClient interface to the Anthropic SDK.
 * Crucially this is NOT a re-implementation of llm-providers/anthropic.ts —
 * that file is single-turn text-only and strips tool_use blocks. For the
 * ConversationRuntime path we need:
 *
 *   1. Full multi-message history (not just one user prompt).
 *   2. `tools` parameter on every turn (from ToolExecutor.toolSpecs()).
 *   3. tool_use blocks surfaced as AssistantEvent (the single-turn provider
 *      filters them out).
 *   4. Usage accounting (message_delta.usage).
 *
 * Why a separate adapter (not extend the existing provider):
 *  - The existing `streamCompletion` shape is provider-agnostic and used by
 *    16 non-team-backed skill flows. Adding tools / multi-turn / usage to
 *    that shape would break the 12-provider parity contract.
 *  - The S6-only adapter has a much narrower public surface (one method,
 *    one interface) that maps 1:1 to the Anthropic SDK without
 *    accumulating cross-provider concessions.
 *
 * Provider scope (Coordinator note in S6 brief): S6 only wires the
 * Anthropic provider. GLM / OpenAI multi-turn variants land in later
 * stories — they require the same kind of tool_use stream support which
 * neither SDK uniformly exposes today.
 */

import Anthropic from '@anthropic-ai/sdk';
import type { MessageStreamEvent } from '@anthropic-ai/sdk/resources/messages';
import type {
  ApiClient,
  AssistantEvent,
} from '../../lib/conversation-runtime';
import type { ConversationMessage, TokenUsage } from '../../lib/conversation-types';
import { toAnthropicMessages } from '../../lib/anthropic-block-adapter';
import type { ToolSpec } from '../../lib/tool-spec';
import { DEFAULT_MODELS } from './types';
import { SYSTEM_PROMPT_DYNAMIC_BOUNDARY } from '../../prompt-assembly';

/**
 * Options for constructing the client. The runtime calls `stream()` per turn;
 * the same client instance is reused across the whole runTurn() loop so it
 * makes sense to bind apiKey / model / max_tokens at construction.
 */
export interface AnthropicApiClientOptions {
  /** BYOK key. If absent and env ANTHROPIC_API_KEY also empty, stream() throws. */
  apiKey?: string;
  /** Model id (e.g. 'claude-sonnet-4-6'). Falls back to DEFAULT_MODELS.anthropic. */
  model?: string;
  /** Per-turn output cap. Falls back to 8192 to match llm-providers/anthropic.ts. */
  max_tokens?: number;
  /** 0..1 sampling temp; omitted from request when undefined. */
  temperature?: number;
  /**
   * Extended thinking (P1, audit/thinking-transport-fix-plan-2026-05-27.md).
   *   - `{ budget_tokens }` → enable; API streams `thinking` blocks before the
   *      answer, which we surface as `thinking_delta` AssistantEvents (→ the
   *      design-spec collapsible thinking card).
   *   - `false`            → explicitly disable.
   *   - `undefined`        → enabled by default (Sonnet 4.x family supports it;
   *      the dispatcher only builds this client for Anthropic models). Budget
   *      from XUEBAN_ANTHROPIC_THINKING_BUDGET env, else 4096.
   *
   * Hard API constraints handled below: when enabled, `temperature` MUST be
   * omitted (API rejects custom temp) and `max_tokens` MUST exceed
   * `budget_tokens`.
   */
  thinking?: { budget_tokens: number } | false;
}

/**
 * Resolve the effective thinking config from the option + env, applying the
 * two hard API constraints. Returns the SDK request fragment plus whether
 * temperature should be suppressed.
 *
 * - `thinking === false` → disabled (caller opted out).
 * - `thinking === { budget_tokens }` → that budget.
 * - `thinking === undefined` → default-on with env/4096 budget.
 *
 * budget is clamped to [1024, max_tokens - 1] so `max_tokens > budget_tokens`
 * always holds (API requirement) and the API's own 1024 floor is respected.
 */
function resolveThinking(
  thinking: { budget_tokens: number } | false | undefined,
  max_tokens: number,
): { enabled: boolean; budget_tokens: number } {
  if (thinking === false) return { enabled: false, budget_tokens: 0 };
  // Global ops kill-switch (§1.5 #5 — "按开关分流"): lets an operator disable
  // extended thinking fleet-wide without a code change, e.g. to cut cost /
  // latency. Default remains on.
  const sw = (process.env.XUEBAN_ANTHROPIC_THINKING ?? '').trim().toLowerCase();
  if (sw === 'off' || sw === '0' || sw === 'false' || sw === 'no') {
    return { enabled: false, budget_tokens: 0 };
  }
  let budget: number;
  if (thinking && typeof thinking.budget_tokens === 'number') {
    budget = thinking.budget_tokens;
  } else {
    const envBudget = Number(process.env.XUEBAN_ANTHROPIC_THINKING_BUDGET);
    budget = Number.isFinite(envBudget) && envBudget > 0 ? envBudget : 4096;
  }
  // API floor 1024; must stay strictly below max_tokens.
  const upper = Math.max(1024, max_tokens - 1);
  budget = Math.min(Math.max(budget, 1024), upper);
  // Degenerate: max_tokens too small to fit any thinking budget → disable.
  if (max_tokens <= 1024) return { enabled: false, budget_tokens: 0 };
  return { enabled: true, budget_tokens: budget };
}

/**
 * Convert our ToolSpec to the Anthropic `tools` array entry shape. Drops
 * `source` (internal-only). Description + input_schema pass through.
 *
 * The SDK types `input_schema` as `{ type: 'object'; properties?; ... }`. All
 * S4 SkillAnchorTool specs already use that exact shape, so this is a
 * runtime no-op — we just cast to the SDK type to satisfy the compiler.
 */
function toAnthropicTool(t: ToolSpec): {
  name: string;
  description: string;
  input_schema: { type: 'object'; [k: string]: unknown };
} {
  return {
    name: t.name,
    description: t.description,
    input_schema: t.input_schema as { type: 'object'; [k: string]: unknown },
  };
}

/**
 * Anthropic stream usage block → our TokenUsage. The SDK reports
 * input_tokens on message_start and output_tokens on message_delta.usage.
 * We extract both as TokenUsage and emit them as a single 'usage'
 * AssistantEvent each time they appear.
 */
function extractUsage(raw: unknown): TokenUsage | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const u = raw as Record<string, unknown>;
  const out: TokenUsage = {};
  if (typeof u.input_tokens === 'number') out.input_tokens = u.input_tokens;
  if (typeof u.output_tokens === 'number') out.output_tokens = u.output_tokens;
  if (typeof u.cache_creation_input_tokens === 'number')
    out.cache_creation_input_tokens = u.cache_creation_input_tokens;
  if (typeof u.cache_read_input_tokens === 'number')
    out.cache_read_input_tokens = u.cache_read_input_tokens;
  return Object.keys(out).length > 0 ? out : undefined;
}

export class AnthropicApiClient implements ApiClient {
  constructor(private readonly opts: AnthropicApiClientOptions = {}) {}

  /**
   * Stream one LLM turn. ConversationRuntime calls this once per iteration
   * of its agentic loop with the full rolling message history.
   *
   * Translation contract (Anthropic stream events → AssistantEvent):
   *   content_block_start (tool_use)            → buffer name+id, start input accumulator
   *   content_block_delta (input_json_delta)    → accumulate partial input JSON
   *   content_block_stop  (tool_use)            → emit 'tool_use' with parsed input
   *   content_block_delta (text_delta)          → emit 'text_delta'
   *   message_start.message.usage               → emit 'usage' (input tokens primarily)
   *   message_delta.usage                       → emit 'usage' (output tokens)
   *   message_stop                              → emit 'message_stop' with stop_reason
   *
   * Errors / signal: re-thrown to runtime (which converts to SSE + re-throws
   * upstream per its own error policy). Signal aborts are also re-thrown via
   * the SDK's AbortError, which the runtime treats as a normal throw — it
   * still folds the partial assistant message into history. Runtime's
   * top-of-loop signal check then short-circuits subsequent iterations.
   */
  async *stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: ToolSpec[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent> {
    const apiKey = this.opts.apiKey ?? process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      // The runtime's error policy expects ApiClient throws to surface as
      // 'error' SSE + re-throw. We honor that by throwing here — no special
      // NO_API_KEY chunk shape (this adapter is internal-only; the upstream
      // provider/skill-runner already gates on key absence before we run).
      throw new Error('Anthropic API key not configured');
    }

    const model =
      this.opts.model ?? process.env.XUEBAN_DEFAULT_MODEL ?? DEFAULT_MODELS.anthropic;
    const max_tokens = this.opts.max_tokens ?? 8192;
    const think = resolveThinking(this.opts.thinking, max_tokens);

    const client = new Anthropic({ apiKey });
    const wireMessages = toAnthropicMessages(args.messages);

    // Per-stream state for tool_use input accumulation. The SDK emits the
    // tool_use input in pieces via input_json_delta; we have to glue them
    // back together and parse before yielding the 'tool_use' event.
    interface PendingToolUse {
      id: string;
      name: string;
      jsonBuf: string;
    }
    const pending = new Map<number, PendingToolUse>();

    // S6-review P1 #1 (2026-05-20): cache stop_reason from message_delta.
    // Per Anthropic SDK + Messages API contract `RawMessageDeltaEvent.delta.
    // stop_reason` is the canonical source — message_stop carries no field of
    // its own and the prior `await stream.finalMessage()` round-trip was both
    // unnecessary and prone to swallowing errors on aborted streams.
    let cachedStopReason: string = 'unknown';

    // Bridge AbortSignal → SDK. The SDK accepts a signal in the request
    // options. Once aborted, the for-await on stream() throws AbortError.
    let stream: ReturnType<Anthropic['messages']['stream']>;
    try {
      stream = client.messages.stream(
        {
          model,
          max_tokens,
          // P1: extended thinking. When enabled, Anthropic streams `thinking`
          // blocks before the answer AND forbids a custom temperature (the
          // request 400s if temperature is sent), so temperature is suppressed
          // here — only forwarded in the non-thinking path.
          ...(think.enabled
            ? { thinking: { type: 'enabled' as const, budget_tokens: think.budget_tokens } }
            : this.opts.temperature !== undefined
              ? { temperature: this.opts.temperature }
              : {}),
          system: args.system_prompt,
          // toAnthropicMessages can return system-role envelopes in pathological
          // inputs; the type below narrows away `system`. We never produce
          // system-role internal messages today (system_prompt is separate), so
          // a runtime cast is safe and keeps the SDK type happy.
          messages: wireMessages as Array<{
            role: 'user' | 'assistant';
            content: typeof wireMessages[number]['content'];
          }>,
          ...(args.tools.length > 0
            ? { tools: args.tools.map(toAnthropicTool) }
            : {}),
        },
        { signal: args.signal },
      );
    } catch (err) {
      // Init failure (auth / network / SDK validation). Rethrow — runtime
      // packs it into 'error' SSE.
      throw err instanceof Error ? err : new Error(String(err));
    }

    for await (const ev of stream as AsyncIterable<MessageStreamEvent>) {
      if (args.signal.aborted) {
        // Defensive — the SDK should have already thrown. Returning here
        // means the generator completes cleanly without an extra event;
        // runtime's top-of-loop abort check handles the SSE.
        return;
      }

      if (ev.type === 'message_start') {
        const u = extractUsage((ev.message as { usage?: unknown }).usage);
        if (u) yield { kind: 'usage', usage: u };
      } else if (ev.type === 'content_block_start') {
        const block = ev.content_block as { type: string; id?: string; name?: string };
        if (block.type === 'tool_use' && typeof block.id === 'string' && typeof block.name === 'string') {
          pending.set(ev.index, { id: block.id, name: block.name, jsonBuf: '' });
        }
      } else if (ev.type === 'content_block_delta') {
        const delta = ev.delta as {
          type: string;
          text?: string;
          partial_json?: string;
          thinking?: string;
        };
        if (delta.type === 'text_delta' && typeof delta.text === 'string') {
          yield { kind: 'text_delta', text: delta.text };
        } else if (delta.type === 'thinking_delta' && typeof delta.thinking === 'string') {
          // P1: extended-thinking content. `signature_delta` (the block's
          // cryptographic signature) is intentionally ignored — it carries no
          // displayable text and the runtime has no use for it today.
          yield { kind: 'thinking_delta', text: delta.thinking };
        } else if (delta.type === 'input_json_delta' && typeof delta.partial_json === 'string') {
          const p = pending.get(ev.index);
          if (p) p.jsonBuf += delta.partial_json;
        }
      } else if (ev.type === 'content_block_stop') {
        const p = pending.get(ev.index);
        if (p) {
          // Empty input is valid (some tools take no args) — SDK emits
          // jsonBuf='' or '{}'. Parse defensively: malformed JSON becomes
          // an empty object so the runtime can still dispatch the call (the
          // tool executor will then error out with a structured response,
          // letting the LLM see the failure and retry).
          let input: unknown = {};
          if (p.jsonBuf.length > 0) {
            try {
              input = JSON.parse(p.jsonBuf);
            } catch {
              input = { __parse_error: true, raw: p.jsonBuf };
            }
          }
          yield { kind: 'tool_use', id: p.id, name: p.name, input };
          pending.delete(ev.index);
        }
      } else if (ev.type === 'message_delta') {
        const u = extractUsage((ev as { usage?: unknown }).usage);
        if (u) yield { kind: 'usage', usage: u };
        // S6-review P1 #1: cache stop_reason here — this is the SDK's
        // canonical carrier per Messages API spec. message_stop downstream
        // emits the cached value.
        const delta = (ev as { delta?: { stop_reason?: unknown } }).delta;
        if (delta && typeof delta.stop_reason === 'string') {
          cachedStopReason = delta.stop_reason;
        }
      } else if (ev.type === 'message_stop') {
        // S6-review P1 #1 (2026-05-20): emit cached stop_reason from
        // message_delta. Avoids the prior `await stream.finalMessage()`
        // round-trip (extra await on a generator iter that may have been
        // aborted; the SDK helper sometimes throws on aborted streams,
        // forcing us into a catch that lost the real signal).
        yield { kind: 'message_stop', stop_reason: cachedStopReason };
      }
    }
  }
}
