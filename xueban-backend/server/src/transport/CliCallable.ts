/**
 * CliCallable.ts — `LlmCallable` adapter for the local-CLI spawner.
 *
 * Wraps `transport/spawners/cli.ts` `runCliSpawn`. One instance binds a
 * specific CLI id (e.g. `'claude'`, `'codex'`); `turn()` spawns the binary
 * per call, streams its stdout through the per-CLI parser
 * (`parsers/cli-streams/<format>`), and forwards SSE events as TurnChunks
 * via `spawner-bridge`.
 *
 * Capabilities (Phase 2 A2): CLI executors don't expose tool-use over their
 * stdio interface and don't support a server-side multi-turn history — each
 * turn spawns a fresh process. Streaming delta IS supported for the Claude
 * Code CLI (stream-json) and Codex CLI (best-effort plain-line); the
 * front-end accepts both at the SSE layer.
 *
 * Cancellation (C1): the underlying spawner already wires `RunnerInput.signal`
 * to SIGTERM → 5s grace → SIGKILL. We forward `input.signal` verbatim.
 */

import { runCliSpawn } from './spawners/cli';
import type { RunnerInput } from './spawners/types';
import { LlmCallError, type TurnChunk } from '../workflow/types';
import type {
  LlmCallable,
  LlmCallableCapabilities,
  LlmCallableTurnInput,
} from './LlmCallable';
import { bridgeSpawnerStream } from './spawner-bridge';

export interface CliCallableOptions {
  /** Session id used by parser callbacks for artifact paths. */
  sessionId?: string;
  /** Working directory; falls back to LlmCallableTurnInput.workspace at turn time. */
  cwd?: string;
  /** Extra env vars merged on top of process.env when spawning. */
  env?: Record<string, string>;
  /** Optional model pin forwarded into `RunnerInput.model`. */
  model?: string;
  /** Per-turn output cap; not all CLIs honour this. */
  maxTokens?: number;
}

export class CliCallable implements LlmCallable {
  readonly id: string;
  readonly capabilities: LlmCallableCapabilities = {
    // `supportsToolUse` = "can Xueban inject tool specs and DRIVE a
    // tool_use→tool_result loop through this callable" — still false: the CLI
    // self-hosts and runs its own tools; we don't pass it tool specs.
    //
    // NB (② 2026-05-31): we DO now SURFACE the CLI's internal tool calls — the
    // spawner's stream-json parser emits structured `tool-use`/`tool-result`
    // events and `spawner-bridge` maps them to typed chunks (no longer flattened
    // to text). So the CLI path is multi-channel for display even though we
    // don't drive the loop. Surfacing ≠ driving; this flag governs driving.
    supportsToolUse: false,
    supportsMultiTurn: false,
    supportsStreamingDelta: true,
  };

  constructor(
    private readonly cliId: string,
    private readonly opts: CliCallableOptions = {},
  ) {
    this.id = `cli:${cliId}`;
  }

  async *turn(input: LlmCallableTurnInput): AsyncGenerator<TurnChunk> {
    const cwd = this.opts.cwd ?? input.workspace;
    if (!cwd) {
      throw new LlmCallError(
        'provider-error',
        `CliCallable(${this.cliId}): no workspace / cwd provided (input.workspace and opts.cwd both absent)`,
      );
    }

    const runnerInput: RunnerInput = {
      system_prompt: input.system,
      prompt: input.prompt,
      session_id: this.opts.sessionId ?? 'transport-callable',
      cwd,
      env: this.opts.env,
      signal: input.signal,
      model: input.model ?? this.opts.model,
      max_tokens: input.maxTokens ?? this.opts.maxTokens,
      temperature: input.temperature,
    };

    yield* bridgeSpawnerStream(runCliSpawn(runnerInput, this.cliId), input.signal);
  }
}
