/**
 * AcpCallable.ts — `LlmCallable` adapter for the ACP (Agent Client Protocol)
 * spawner.
 *
 * Wraps `transport/spawners/acp.ts` `runAcpExecutor`. One instance binds a
 * specific ACP target id (e.g. `'hermes'` or `'custom?cmd=node&arg=server.js'`);
 * `turn()` spawns the agent as a stdio child, runs the JSON-RPC handshake
 * (`initialize` → `session/new` → `session/prompt`), and streams
 * `session/update` notifications back as TurnChunks via `spawner-bridge`.
 *
 * Capabilities (Phase 2 A2): ACP agents can in principle expose tool-use and
 * multi-turn sessions via their internal protocol, but the current
 * Xueban integration treats one `session/prompt` as one transport turn —
 * the higher-level orchestration (Phase 2 DAG) drives multi-step coordination
 * through artifact handoff (decision A2), so the callable surface stays
 * single-turn. Capabilities below reflect what's actually wired today.
 *
 * Cancellation (C1): `runAcpExecutor` already sends `session/cancel` then
 * tears down the child within 200ms. We forward `input.signal` verbatim.
 */

import { runAcpExecutor } from './spawners/acp';
import type { RunnerInput } from './spawners/types';
import { LlmCallError, type TurnChunk } from '../workflow/types';
import type {
  LlmCallable,
  LlmCallableCapabilities,
  LlmCallableTurnInput,
} from './LlmCallable';
import { bridgeSpawnerStream } from './spawner-bridge';

export interface AcpCallableOptions {
  sessionId?: string;
  cwd?: string;
  env?: Record<string, string>;
}

export class AcpCallable implements LlmCallable {
  readonly id: string;
  readonly capabilities: LlmCallableCapabilities = {
    // ACP protocol does expose tool semantics but Phase 2 wraps them inside
    // the SSE text-delta stream — orchestrator does not see tool_use chunks
    // from this callable today. Marking false to avoid mis-routing.
    supportsToolUse: false,
    supportsMultiTurn: true,
    supportsStreamingDelta: true,
  };

  constructor(
    private readonly target: string,
    private readonly opts: AcpCallableOptions = {},
  ) {
    this.id = `acp:${target}`;
  }

  async *turn(input: LlmCallableTurnInput): AsyncGenerator<TurnChunk> {
    const cwd = this.opts.cwd ?? input.workspace;
    if (!cwd) {
      throw new LlmCallError(
        'provider-error',
        `AcpCallable(${this.target}): no workspace / cwd provided`,
      );
    }

    const runnerInput: RunnerInput = {
      system_prompt: input.system,
      prompt: input.prompt,
      session_id: this.opts.sessionId ?? 'transport-callable',
      cwd,
      env: this.opts.env,
      signal: input.signal,
      model: input.model,
      max_tokens: input.maxTokens,
      temperature: input.temperature,
    };

    yield* bridgeSpawnerStream(runAcpExecutor(runnerInput, this.target), input.signal);
  }
}
