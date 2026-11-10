/**
 * spawner-bridge.ts — Translation helpers shared by CliCallable / AcpCallable
 * / McpCallable.
 *
 * The legacy spawner runners (`transport/spawners/{cli,acp,mcp}.ts`) yield
 * `SseEvent = { event, data }` shapes — the same wire-format the existing
 * `routes/run-sessions.ts` SSE handler already speaks. The Phase 2 Transport
 * contract is stricter: `LlmCallable.turn()` yields `TurnChunk`, a
 * discriminated union the orchestrator/scheduler can `switch` on without
 * parsing the SSE event-name string.
 *
 * This file does NOT attempt to invert parser.ts. Instead it forwards each
 * SSE event verbatim as a single `text-delta` chunk whose `value` is the
 * canonical SSE wire line ("event: ...\ndata: <json>\n\n"). The scheduler /
 * Assembler (Lane 3) is responsible for re-parsing where it needs to (the
 * existing front-end already does this on `useRunSession`'s side).
 *
 * The two exceptions:
 *   - `event: 'error'` → mapped to a typed `{type: 'error', error}` chunk so
 *     the retry layer (`workflow/retry.ts`) can classify by `LlmCallErrorKind`.
 *   - End-of-stream → after the spawner generator returns, the callable
 *     yields a single `{type: 'done'}` chunk. The spawner never emits one
 *     itself (its consumer relied on iterator return) so we synthesise it.
 *
 * This is intentionally lossy on the chunk-taxonomy axis but lossless on the
 * SSE wire — Lane 3 can choose to either teach the scheduler to forward
 * text-delta chunks directly to SSE, or to add a finer-grained translation
 * layer later. Either path is forward-compatible with this bridge.
 */

import type { SseEvent } from '../parser';
import { LlmCallError, type LlmCallErrorKind, type TurnChunk } from '../workflow/types';

/**
 * Best-effort mapping from a spawner error event's `code` field to one of
 * `LlmCallErrorKind`. Unknown codes degrade to `provider-error` so the front-
 * end still gets a readable message.
 */
function spawnerErrorKind(code: unknown): LlmCallErrorKind {
  if (typeof code !== 'string') return 'provider-error';
  if (code === 'CLI_NOT_INSTALLED' || code === 'CLI_NOT_REGISTERED') return 'provider-error';
  if (code === 'CLI_SPAWN_FAILED') return 'cli-crash';
  if (code === 'CLI_EXIT_NONZERO') return 'cli-crash';
  if (code === 'EXECUTOR_UNKNOWN') return 'provider-error';
  if (code === 'ACP_UNREACHABLE' || code === 'EXECUTOR_NOT_INSTALLED') return 'provider-error';
  if (code === 'ACP_TIMEOUT' || code === 'MCP_TIMEOUT') return 'timeout';
  if (code === 'MCP_SERVER_NOT_FOUND' || code === 'MCP_TOOL_NOT_FOUND') return 'provider-error';
  if (code === 'PROVIDER_ERROR' || code === 'NO_API_KEY') return 'auth';
  if (code === 'RATE_LIMITED') return 'rate-limit';
  if (code === 'AUTH_FAILED') return 'auth';
  if (code === 'NETWORK_ERROR') return 'network';
  return 'provider-error';
}

/**
 * Translate one already-structured spawner SSE event into a TurnChunk.
 *
 * ② sse-frame-leak CLI-path root cure (2026-05-31): the spawner's stream-json
 * parser (dispatchParser) ALREADY produced structured Xueban events. We map
 * the core channels (text / thinking / tool-use / tool-result) onto typed
 * TurnChunks — so the CLI path now has the same independent channels as the API
 * path — and pass every other already-structured business event (node /
 * assemble / blueprint / classify / edge / yaml-line / usage / raw / complete /
 * discovery / question-form / …) through VERBATIM as an `sse` chunk.
 *
 * The OLD behavior re-encoded EVERY event as a `text-delta` carrying the literal
 * `event:/data:` wire line; the downstream text parser then mis-flagged those
 * frames as sse-frame-leak `raw` blocks (so <sf:node>/complete never rendered →
 * TEAM 0 + a screen full of raw). Mapping/passthrough keeps the structure.
 */
export function sseEventToChunk(ev: SseEvent): TurnChunk {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  const nodeId = typeof data.node_id === 'string' ? data.node_id : undefined;

  switch (ev.event) {
    case 'error': {
      const code = data.code;
      const message = typeof data.message === 'string' ? data.message : 'spawner error';
      return {
        type: 'error',
        error: new LlmCallError(spawnerErrorKind(code), message, { cause: ev.data }),
        node_id: nodeId,
      };
    }
    case 'text': {
      const value = typeof data.text === 'string' ? data.text : '';
      return { type: 'text-delta', value, node_id: nodeId };
    }
    case 'thinking-chunk': {
      const value = typeof data.text === 'string' ? data.text : '';
      return { type: 'thinking-delta', value, node_id: nodeId };
    }
    case 'tool-use': {
      return {
        type: 'tool-use',
        tool: {
          tool_name: typeof data.name === 'string' ? data.name : 'unknown',
          tool_input: data.input,
          call_id: typeof data.id === 'string' ? data.id : undefined,
        },
        node_id: nodeId,
      };
    }
    case 'tool-result': {
      const output =
        typeof data.output === 'string'
          ? data.output
          : data.output != null
            ? JSON.stringify(data.output)
            : '';
      const forId =
        typeof data.for === 'string' ? data.for : typeof data.id === 'string' ? data.id : '';
      return {
        type: 'tool-result',
        result: { tool_use_id: forId, output, is_error: data.is_error === true },
        node_id: nodeId,
      };
    }
    default:
      // Already-structured Xueban business event — forward verbatim.
      return { type: 'sse', event: ev.event, data: ev.data, node_id: nodeId };
  }
}

/**
 * Drain a spawner SSE generator and re-yield as TurnChunks. Honours
 * `signal.aborted` (stops forwarding on cancel; the spawner is expected to
 * have its own signal-driven cleanup via the `RunnerInput.signal` field).
 *
 * Synthesises a single trailing `{type: 'done'}` chunk after the spawner
 * generator finishes normally. On error chunks the bridge does NOT inject
 * `done` — the scheduler treats an error chunk as terminal.
 */
export async function* bridgeSpawnerStream(
  source: AsyncGenerator<SseEvent> | AsyncIterable<SseEvent>,
  signal: AbortSignal,
): AsyncGenerator<TurnChunk> {
  let sawError = false;
  for await (const ev of source) {
    if (signal.aborted) return;
    const chunk = sseEventToChunk(ev);
    if (chunk.type === 'error') sawError = true;
    yield chunk;
  }
  if (!sawError) yield { type: 'done' };
}
