/**
 * conversation-runtime-impl.ts — PR-D Lane 1 multi-turn tool-use driver.
 *
 * Restores the `ConversationRuntime` class that Phase 2 (commit 6905478)
 * removed when daemon-led DAG + artifact handoff became the primary
 * orchestration path. PR-C compiled `agentConfig.tools[]` re-introduces
 * single-agent tool-use as a first-class flow, so we need the loop back.
 *
 * The class lives in this file (and is re-exported from
 * `conversation-runtime.ts`) so the original type-shim module can stay
 * untouched for Transport-layer adapters that only need the interface
 * declarations.
 *
 * What this driver does
 * ─────────────────────
 *
 *   user message
 *     → push as user content block
 *     → loop ≤ maxIterations:
 *         · call ApiClient.stream(system + history + tools)
 *         · for each AssistantEvent:
 *             text_delta  → yield TurnChunk text-delta, buffer for history
 *             tool_use    → flush text into history, queue tool call
 *             usage       → fold into running TokenUsage, yield 'usage' chunk
 *             message_stop → cache stop_reason
 *         · push assistant turn into history
 *         · if no pending tool_use → break (assistant ended turn)
 *         · for each pending tool: ToolRunner.dispatch
 *             - yield any tool-emitted SSE as TurnChunk (channelled via
 *               a `tool-use` chunk with the executor result attached)
 *             - push tool_result into history
 *         · loop next iteration
 *     → yield 'done' TurnChunk
 *
 * Output shape: `AsyncGenerator<TurnChunk>` (not `AssistantEvent`)
 * ────────────────────────────────────────────────────────────────
 * The Round 4 plan spec described the legacy `AssistantEvent` output, but
 * the assembler's `pipeChunksToSse` and the workflow executor both consume
 * `TurnChunk` (Phase 2 decision A1). Yielding `TurnChunk` lets the runtime
 * drop into the existing orchestration plumbing without a second adapter.
 * Internal AssistantEvents from the ApiClient are still observed inside
 * runTurn(); we translate them to TurnChunks at the yield boundary.
 *
 * Error policy (mirrors Phase 2 CL3/E3)
 * ────────────────────────────────────
 *
 *   - ApiClient throws        → yield 'error' TurnChunk, return
 *   - ApiClient error mid-stream → yield 'error' TurnChunk, return
 *   - Tool throws / returns isError → packed as tool_result is_error=true,
 *                                     fed back to LLM, loop continues
 *   - Permission deny / prompt → tool_result is_error=true with reason,
 *                                permission_check SSE for 'prompt' mode,
 *                                loop continues
 *   - max_iterations hit       → yield 'error' TurnChunk(MAX_ITERATIONS),
 *                                then 'done'
 *   - signal.aborted           → yield 'error' TurnChunk('timeout' kind →
 *                                actually 'aborted' semantics packaged
 *                                as LlmCallError), return
 *
 * The runtime never persists session state itself — `history` is a
 * by-value array passed in; the caller decides whether to thread the
 * mutated history through to a follow-up runTurn() call. (Matches
 * `LlmCallable.turn()`'s artifact-handoff philosophy: orchestration owns
 * cross-turn state, transport owns one turn.)
 */

import type { ApiClient } from './conversation-runtime';
import type {
  ContentBlock,
  ConversationMessage,
  TokenUsage,
} from './conversation-types';
import { LlmCallError, type TurnChunk } from '../workflow/types';
import type { ToolRunner } from './tool-runner';

/** Initial zeroed usage. Stored cumulatively across turns within one runTurn. */
function zeroUsage(): TokenUsage {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
  };
}

/** Sum two TokenUsage snapshots (cf. addUsage() in conversation-runtime.ts). */
function addUsage(a: TokenUsage, b: TokenUsage): TokenUsage {
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

/** Stringify arbitrary tool output for the tool_result block. */
function outputToString(out: unknown): string {
  return typeof out === 'string' ? out : JSON.stringify(out);
}

/**
 * Constructor options. The runtime is intentionally created per-skill-
 * activation (cheap), so all collaborators are owned externally and passed
 * in. `maxIterations` defaults to 50 (Phase 1 D2 decision: 16 was too low
 * for 4-agent skill runs, 50 gives headroom while still surfacing runaways).
 */
export interface ConversationRuntimeOptions {
  apiClient: ApiClient;
  toolRunner: ToolRunner;
  maxIterations?: number;
}

/** Input to runTurn. `history` is mutated in place when the caller passes one. */
export interface RunTurnArgs {
  system_prompt: string;
  user_message: string;
  /**
   * Optional rolling history. Defaults to `[]`. The runtime appends the new
   * user/assistant/tool messages produced by this turn. If the caller wants
   * isolation, pass a fresh array; if continuation, pass the same array
   * across runTurn calls.
   */
  history?: ConversationMessage[];
  signal: AbortSignal;
  /** Optional per-call max_tokens forwarded to the ApiClient. Unused for now. */
  max_tokens?: number;
}

export class ConversationRuntime {
  private readonly apiClient: ApiClient;
  private readonly toolRunner: ToolRunner;
  private readonly maxIterations: number;

  constructor(opts: ConversationRuntimeOptions) {
    this.apiClient = opts.apiClient;
    this.toolRunner = opts.toolRunner;
    this.maxIterations = opts.maxIterations ?? 50;
  }

  /**
   * Drive one user-initiated turn end-to-end. Yields `TurnChunk` events that
   * plug straight into `pipeChunksToSse` (assembler.ts) or the workflow
   * executor's chunk-forwarding loop.
   *
   * Generator ALWAYS terminates by yielding a `done` TurnChunk (success) or
   * an `error` chunk + return (call-phase / mid-stream failure / abort).
   * Callers must consume the generator to completion or abort via `signal`.
   */
  async *runTurn(args: RunTurnArgs): AsyncGenerator<TurnChunk> {
    const history = args.history ?? [];

    // 1) Push the user message that opens the turn.
    history.push({
      role: 'user',
      blocks: [{ kind: 'text', text: args.user_message }],
    });

    let iterations = 0;
    let totalUsage = zeroUsage();
    let stopReason = 'unknown';
    let cleanBreak = false;

    while (iterations < this.maxIterations) {
      // Top-of-loop abort check.
      if (args.signal.aborted) {
        yield {
          type: 'error',
          error: new LlmCallError('timeout', 'aborted by caller signal'),
        };
        return;
      }
      iterations += 1;

      const assistantBlocks: ContentBlock[] = [];
      const pendingTools: Array<{ id: string; name: string; input: unknown }> = [];
      let textBuf = '';
      let thisTurnStopReason: string | null = null;

      // 2) Run one LLM stream. We translate AssistantEvent → TurnChunk
      //    inline (rather than via the helper) so we can do the
      //    text-flush-before-tool_use ordering correctly: buffered text
      //    must land in `assistantBlocks` AS A TEXT BLOCK before the
      //    tool_use block that follows it. Flushing only at stream end
      //    would silently re-order text after tool_use in history.
      const tools = this.toolRunner.toolSpecs();
      try {
        for await (const ev of this.apiClient.stream({
          system_prompt: args.system_prompt,
          messages: history,
          tools,
          signal: args.signal,
        })) {
          if (ev.kind === 'text_delta') {
            textBuf += ev.text;
            yield { type: 'text-delta', value: ev.text };
          } else if (ev.kind === 'thinking_delta') {
            // P1: stream extended-thinking content out as its own TurnChunk.
            // Deliberately NOT appended to assistantBlocks — thinking is
            // display-only and must not pollute the replayed text history
            // (it would otherwise be fed back as a user-visible answer block).
            yield { type: 'thinking-delta', value: ev.text };
          } else if (ev.kind === 'tool_use') {
            // Flush any accumulated text into a text block BEFORE the
            // tool_use block. Order matters: when we replay this assistant
            // turn to the LLM, it should see text → tool_use in the same
            // sequence the model emitted them.
            if (textBuf.length > 0) {
              assistantBlocks.push({ kind: 'text', text: textBuf });
              textBuf = '';
            }
            assistantBlocks.push({
              kind: 'tool_use',
              id: ev.id,
              name: ev.name,
              input: ev.input,
            });
            pendingTools.push({ id: ev.id, name: ev.name, input: ev.input });
            // P1 (root-cure plan §5): surface the model's tool call as a
            // STRUCTURED chunk so the UI renders a tool_call chip — instead of
            // the call staying invisible inside the loop (the old behavior) or
            // being recovered by re-parsing <tool_use> XML out of text.
            yield {
              type: 'tool-use',
              tool: { tool_name: ev.name, tool_input: ev.input, call_id: ev.id },
            };
          } else if (ev.kind === 'usage') {
            totalUsage = addUsage(totalUsage, ev.usage);
            yield {
              type: 'usage',
              usage: {
                input_tokens: ev.usage.input_tokens,
                output_tokens: ev.usage.output_tokens,
                cache_creation_input_tokens: ev.usage.cache_creation_input_tokens,
                cache_read_input_tokens: ev.usage.cache_read_input_tokens,
              },
            };
          } else if (ev.kind === 'message_stop') {
            // Final text flush at the end of the turn — covers the case
            // where the model ended with prose and no trailing tool_use.
            if (textBuf.length > 0) {
              assistantBlocks.push({ kind: 'text', text: textBuf });
              textBuf = '';
            }
            thisTurnStopReason = ev.stop_reason;
          }
        }

        // Safety net: stream ended without a message_stop event. Still
        // flush any pending text so we don't lose the assistant's prose.
        if (textBuf.length > 0) {
          assistantBlocks.push({ kind: 'text', text: textBuf });
          textBuf = '';
        }
      } catch (err) {
        // ApiClient throw — call-phase failure for this iteration. Surface
        // as 'error' chunk and return; the upstream caller (assembler or
        // workflow executor) decides whether to retry the whole skill.
        const wrapped =
          err instanceof LlmCallError
            ? err
            : new LlmCallError(
                'provider-error',
                err instanceof Error ? err.message : String(err),
                { cause: err },
              );
        yield { type: 'error', error: wrapped };
        return;
      }

      // 3) Persist the assistant turn (text-only OR text + tool_use blocks).
      history.push({
        role: 'assistant',
        blocks: assistantBlocks,
        usage: totalUsage,
      });
      if (thisTurnStopReason !== null) {
        stopReason = thisTurnStopReason;
      }

      // 4) No tool calls → clean break.
      if (pendingTools.length === 0) {
        cleanBreak = true;
        break;
      }

      // 5) Dispatch each pending tool in order. Each becomes one
      //    role: 'tool' message with a single tool_result block.
      for (const t of pendingTools) {
        if (args.signal.aborted) {
          yield {
            type: 'error',
            error: new LlmCallError('timeout', 'aborted by caller signal'),
          };
          return;
        }

        const dispatch = await this.toolRunner.dispatch(
          { name: t.name, input: t.input },
          args.signal,
        );

        // Tool-emitted SSE (e.g. permission_check) is surfaced as a
        // `tool-use` chunk so the consumer can route it onto the wire.
        // We piggy-back on the tool-use TurnChunk shape — assembler's
        // `pipeChunksToSse` drops these today, but the workflow observer
        // forwards them. Downstream (Phase 3) we can grow a dedicated
        // chunk kind if a richer wire is needed.
        for (const sse of dispatch.sseEvents ?? []) {
          yield {
            type: 'tool-use',
            tool: {
              tool_name: sse.event,
              tool_input: sse.data,
              call_id: t.id,
            },
          };
        }

        const toolOutput = outputToString(dispatch.output);
        history.push({
          role: 'tool',
          blocks: [
            {
              kind: 'tool_result',
              tool_use_id: t.id,
              tool_name: t.name,
              output: toolOutput,
              is_error: dispatch.isError ?? false,
            },
          ],
        });
        // P1: surface the result as a STRUCTURED chunk (pairs with the tool-use
        // chunk above by tool_use_id) so the UI's tool-group card shows the
        // tool_echo continuation line. Previously results stayed loop-internal.
        yield {
          type: 'tool-result',
          result: {
            tool_use_id: t.id,
            output: toolOutput,
            is_error: dispatch.isError ?? false,
          },
        };
      }
      // Loop continues with the new tool_result(s) appended to history.
    }

    // Out of the loop. Either clean break (assistant ended turn) or we hit
    // maxIterations with pending tool_use — that's a runaway, surface it.
    if (!cleanBreak && iterations >= this.maxIterations) {
      yield {
        type: 'error',
        error: new LlmCallError(
          'provider-error',
          `reached max_iterations=${this.maxIterations} with pending tool_use`,
        ),
      };
      stopReason = 'max_iterations';
    }

    // Final usage snapshot + done. The 'done' chunk is the orchestration
    // signal; assembler's pipeChunksToSse uses it as a flush boundary and
    // the workflow executor uses it to know when to write artifacts.
    yield {
      type: 'usage',
      usage: {
        input_tokens: totalUsage.input_tokens,
        output_tokens: totalUsage.output_tokens,
        cache_creation_input_tokens: totalUsage.cache_creation_input_tokens,
        cache_read_input_tokens: totalUsage.cache_read_input_tokens,
      },
    };
    // `stopReason` is informational; we could surface it via a dedicated
    // chunk in the future but for now the cumulative usage above + `done`
    // is enough for orchestration. Reference the variable so noUnusedLocals
    // doesn't flag it.
    void stopReason;
    yield { type: 'done' };
  }

}
