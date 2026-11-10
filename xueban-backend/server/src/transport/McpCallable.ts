/**
 * McpCallable.ts — `LlmCallable` adapter for the MCP (Model Context Protocol)
 * spawner.
 *
 * Wraps `transport/spawners/mcp.ts` `runMcpExecutor`. The MCP runner spec
 * format is `<server>/<tool>` (e.g. `'fs/edit_file'`). One instance binds a
 * specific spec; `turn()` spawns the MCP server, performs `initialize` +
 * `tools/list`, then `tools/call` with `{goal, system}` as arguments. The
 * runner yields a single text-delta chunk built from `result.content[]`
 * text items (MCP `tools/call` is request/response, not notifications).
 *
 * Capabilities (Phase 2 A2): MCP is not streaming — a single chunk arrives
 * after the tool completes. From Xueban's transport perspective the call
 * looks like a non-streaming text oracle; the UI "typewriter" effect (A1)
 * still works because the chunk is emitted as a single text-delta TurnChunk
 * the front-end appends to its panel.
 *
 * Cancellation (C1): `runMcpExecutor` honours `RunnerInput.signal` by
 * tearing down the child + JSON-RPC transport. We forward verbatim.
 */

import { runMcpExecutor } from './spawners/mcp';
import type { RunnerInput } from './spawners/types';
import { LlmCallError, type TurnChunk } from '../workflow/types';
import type {
  LlmCallable,
  LlmCallableCapabilities,
  LlmCallableTurnInput,
} from './LlmCallable';
import { bridgeSpawnerStream } from './spawner-bridge';

export interface McpCallableOptions {
  sessionId?: string;
  cwd?: string;
  env?: Record<string, string>;
}

export class McpCallable implements LlmCallable {
  readonly id: string;
  readonly capabilities: LlmCallableCapabilities = {
    // MCP exposes tool semantics — but at the Transport contract level, a
    // single `tools/call` IS the turn. We don't surface separate tool_use
    // chunks; the call result is the text-delta.
    supportsToolUse: true,
    supportsMultiTurn: false,
    supportsStreamingDelta: false,
  };

  constructor(
    private readonly spec: string,
    private readonly opts: McpCallableOptions = {},
  ) {
    this.id = `mcp:${spec}`;
  }

  async *turn(input: LlmCallableTurnInput): AsyncGenerator<TurnChunk> {
    const cwd = this.opts.cwd ?? input.workspace;
    if (!cwd) {
      throw new LlmCallError(
        'provider-error',
        `McpCallable(${this.spec}): no workspace / cwd provided`,
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

    yield* bridgeSpawnerStream(runMcpExecutor(runnerInput, this.spec), input.signal);
  }
}
