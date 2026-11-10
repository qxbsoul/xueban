/**
 * conversation-runtime.test.ts — PR-D Lane 1 ConversationRuntime smoke tests.
 *
 * Run with:  npx tsx src/lib/__tests__/conversation-runtime.test.ts   (from server/)
 *
 * No framework — mirrors the surrounding lib/__tests__ pattern (compare to
 * permission-policy.test.ts and tool-spec.test.ts). Each test uses a fresh
 * FakeApiClient + FakeToolRunner so state doesn't leak between cases.
 *
 * The whole file runs inside a single async IIFE because the project tsconfig
 * uses CommonJS module + ES2020 target — top-level await isn't permitted.
 *
 * Coverage:
 *   1. Happy path: one turn, no tool_use → done
 *   2. With tool: tool_use → allowed → tool_result → next turn → done
 *   3. Deny:  tool_use → denied → is_error tool_result → loop continues
 *   4. Max iterations: 5 (test override) → error chunk + done
 *   5. Abort: signal aborted before start → error chunk, return early
 *   6. ApiClient throw mid-stream → error chunk + return
 */

import { ConversationRuntime } from '../conversation-runtime-impl';
import {
  ToolRunner,
  type ToolExecutor,
  _resetToolExecutorsForTests,
  registerToolExecutor,
} from '../tool-runner';
import { PermissionPolicyV2 } from '../permission-policy-v2';
import { ToolRegistry, type ToolSpec } from '../tool-spec';
import type { ApiClient, AssistantEvent } from '../conversation-runtime';
import type { ConversationMessage } from '../conversation-types';
import type { TurnChunk } from '../../workflow/types';

let pass = 0;
let fail = 0;

function check(label: string, cond: boolean, detail?: string): void {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}${detail ? `\n        ${detail}` : ''}`);
  }
}

// ─── Helpers ────────────────────────────────────────────────────────────────

/**
 * FakeApiClient yields a pre-scripted sequence of AssistantEvents per turn.
 * Each call to `stream()` consumes one script. Throws if scripts run out so
 * a test that drives more turns than expected fails loudly.
 */
class FakeApiClient implements ApiClient {
  private scripts: AssistantEvent[][];
  private turn = 0;
  public callLog: Array<{
    system_prompt: string;
    messages: ConversationMessage[];
    toolNames: string[];
  }> = [];

  constructor(scripts: AssistantEvent[][]) {
    this.scripts = scripts;
  }

  async *stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: { name: string }[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent> {
    const idx = this.turn++;
    this.callLog.push({
      system_prompt: args.system_prompt,
      // Snapshot history at call time so mutations downstream don't pollute.
      messages: args.messages.map((m) => ({ ...m, blocks: [...m.blocks] })),
      toolNames: args.tools.map((t) => t.name),
    });
    if (idx >= this.scripts.length) {
      throw new Error(`FakeApiClient: no script for turn ${idx}`);
    }
    for (const ev of this.scripts[idx]) {
      if (args.signal.aborted) return;
      yield ev;
    }
  }
}

async function drain(gen: AsyncGenerator<TurnChunk>): Promise<TurnChunk[]> {
  const out: TurnChunk[] = [];
  for await (const c of gen) out.push(c);
  return out;
}

function spec(name: string): ToolSpec {
  return {
    name,
    description: `synthetic ${name}`,
    input_schema: { type: 'object', properties: {} },
    source: 'base',
  };
}

function buildRuntime(opts: {
  scripts: AssistantEvent[][];
  policy: PermissionPolicyV2;
  registry: ToolRegistry;
  maxIterations?: number;
}): { runtime: ConversationRuntime; api: FakeApiClient } {
  const api = new FakeApiClient(opts.scripts);
  const runner = new ToolRunner(opts.registry, opts.policy);
  const runtime = new ConversationRuntime({
    apiClient: api,
    toolRunner: runner,
    maxIterations: opts.maxIterations,
  });
  return { runtime, api };
}

// ─── All tests run sequentially inside a single async IIFE ─────────────────

(async (): Promise<void> => {
  // ── 1. Happy path ────────────────────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    const { runtime } = buildRuntime({
      scripts: [
        [
          { kind: 'text_delta', text: 'Hello ' },
          { kind: 'text_delta', text: 'world' },
          { kind: 'usage', usage: { input_tokens: 10, output_tokens: 2 } },
          { kind: 'message_stop', stop_reason: 'end_turn' },
        ],
      ],
      policy: PermissionPolicyV2.fromAllowedTools([]),
      registry: new ToolRegistry(),
    });
    const history: ConversationMessage[] = [];
    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'You are a helper.',
        user_message: 'hi',
        history,
        signal: new AbortController().signal,
      }),
    );
    const textValues = chunks
      .filter((c): c is Extract<TurnChunk, { type: 'text-delta' }> => c.type === 'text-delta')
      .map((c) => c.value)
      .join('');
    check('1. happy path: text concatenates to "Hello world"', textValues === 'Hello world');
    check('1. happy path: ends with done', chunks[chunks.length - 1]?.type === 'done');
    check('1. happy path: contains usage chunk', chunks.some((c) => c.type === 'usage'));
    check('1. happy path: no error chunk', !chunks.some((c) => c.type === 'error'));
    check(
      '1. happy path: history grew by user + assistant',
      history.length === 2 && history[0].role === 'user' && history[1].role === 'assistant',
    );
    const assistantBlock = history[1].blocks[0];
    check(
      '1. happy path: assistant block carries the text',
      assistantBlock?.kind === 'text' && assistantBlock.text === 'Hello world',
    );
  }

  // ── 2. Tool use happy path ───────────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    const exec: ToolExecutor = {
      async execute(input) {
        return { output: { echoed: input } };
      },
    };
    registerToolExecutor('echo_tool', exec);
    const registry = new ToolRegistry([spec('echo_tool')]);
    const policy = PermissionPolicyV2.fromAllowedTools(['echo_tool']);

    const { runtime, api } = buildRuntime({
      scripts: [
        [
          { kind: 'text_delta', text: 'calling tool...' },
          {
            kind: 'tool_use',
            id: 'toolu_01',
            name: 'echo_tool',
            input: { msg: 'hi' },
          },
          { kind: 'message_stop', stop_reason: 'tool_use' },
        ],
        [
          { kind: 'text_delta', text: 'done.' },
          { kind: 'message_stop', stop_reason: 'end_turn' },
        ],
      ],
      policy,
      registry,
    });

    const history: ConversationMessage[] = [];
    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'helper',
        user_message: 'echo hi',
        history,
        signal: new AbortController().signal,
      }),
    );
    check('2. tool use: two LLM turns called', api.callLog.length === 2);
    check(
      '2. tool use: 2nd turn saw tool_result in history',
      api.callLog[1].messages.some(
        (m) =>
          m.role === 'tool' &&
          m.blocks.some((b) => b.kind === 'tool_result' && !b.is_error),
      ),
    );
    check('2. tool use: ends with done', chunks[chunks.length - 1]?.type === 'done');
    check('2. tool use: no error chunk', !chunks.some((c) => c.type === 'error'));
    check(
      '2. tool use: history shape user→assistant→tool→assistant',
      history.length === 4 &&
        history[0].role === 'user' &&
        history[1].role === 'assistant' &&
        history[2].role === 'tool' &&
        history[3].role === 'assistant',
    );
    const toolBlock = history[2].blocks[0];
    check(
      '2. tool use: tool_result not flagged as error',
      toolBlock?.kind === 'tool_result' && toolBlock.is_error === false,
    );
    // Assistant turn 0 should contain text BEFORE tool_use block.
    const a0 = history[1].blocks;
    check(
      '2. tool use: assistant block order is text → tool_use',
      a0[0]?.kind === 'text' && a0[1]?.kind === 'tool_use',
    );
    // P1 (root-cure plan §5): the runtime now SURFACES the model's tool call
    // and the dispatch result as structured TurnChunks (previously they stayed
    // loop-internal and the UI never saw them). pipeChunksToSse maps these to
    // tool-use / tool-result SSE frames → timeline tool-group card.
    const tuChunk = chunks.find((c) => c.type === 'tool-use');
    check('2. P1: yields a tool-use chunk', !!tuChunk);
    check(
      '2. P1: tool-use chunk carries name + call_id',
      tuChunk?.type === 'tool-use' &&
        tuChunk.tool.tool_name === 'echo_tool' &&
        tuChunk.tool.call_id === 'toolu_01',
    );
    const trChunk = chunks.find((c) => c.type === 'tool-result');
    check('2. P1: yields a tool-result chunk', !!trChunk);
    check(
      '2. P1: tool-result pairs by tool_use_id, not flagged error',
      trChunk?.type === 'tool-result' &&
        trChunk.result.tool_use_id === 'toolu_01' &&
        trChunk.result.is_error === false,
    );
    // Ordering: tool-use chunk must precede its tool-result chunk on the wire.
    check(
      '2. P1: tool-use chunk precedes tool-result chunk',
      chunks.findIndex((c) => c.type === 'tool-use') <
        chunks.findIndex((c) => c.type === 'tool-result'),
    );
  }

  // ── 3. Deny → loop continues ─────────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    const registry = new ToolRegistry([spec('forbidden')]);
    const policy = PermissionPolicyV2.fromAllowedTools([]); // deny-all
    let executorCalled = false;
    registerToolExecutor('forbidden', {
      async execute() {
        executorCalled = true;
        return { output: 'should-not-run' };
      },
    });

    const { runtime, api } = buildRuntime({
      scripts: [
        [
          {
            kind: 'tool_use',
            id: 'toolu_02',
            name: 'forbidden',
            input: {},
          },
          { kind: 'message_stop', stop_reason: 'tool_use' },
        ],
        [
          { kind: 'text_delta', text: 'cannot proceed' },
          { kind: 'message_stop', stop_reason: 'end_turn' },
        ],
      ],
      policy,
      registry,
    });

    const history: ConversationMessage[] = [];
    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'helper',
        user_message: 'do the thing',
        history,
        signal: new AbortController().signal,
      }),
    );
    check('3. deny: executor was NOT called', executorCalled === false);
    check(
      '3. deny: ran two LLM turns (loop continued after denial)',
      api.callLog.length === 2,
    );
    const toolMsg = history.find((m) => m.role === 'tool');
    const tr = toolMsg?.blocks[0];
    check(
      '3. deny: tool_result has is_error=true',
      !!tr && tr.kind === 'tool_result' && tr.is_error === true,
    );
    check(
      '3. deny: ends with done (loop did not abort)',
      chunks[chunks.length - 1]?.type === 'done',
    );
  }

  // ── 4. Max iterations ────────────────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    const registry = new ToolRegistry([spec('looper')]);
    const policy = PermissionPolicyV2.fromAllowedTools(['looper']);
    registerToolExecutor('looper', {
      async execute() {
        return { output: 'again' };
      },
    });

    // Every script asks for tool_use → max_iterations hits before resolution.
    const infiniteScripts: AssistantEvent[][] = [];
    for (let i = 0; i < 60; i++) {
      infiniteScripts.push([
        {
          kind: 'tool_use',
          id: `toolu_loop_${i}`,
          name: 'looper',
          input: {},
        },
        { kind: 'message_stop', stop_reason: 'tool_use' },
      ]);
    }

    const { runtime } = buildRuntime({
      scripts: infiniteScripts,
      policy,
      registry,
      maxIterations: 5,
    });

    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'helper',
        user_message: 'loop',
        signal: new AbortController().signal,
      }),
    );
    const errs = chunks.filter(
      (c): c is Extract<TurnChunk, { type: 'error' }> => c.type === 'error',
    );
    check('4. max_iter: exactly one error chunk', errs.length === 1);
    check(
      '4. max_iter: error message mentions max_iterations',
      typeof errs[0]?.error?.message === 'string' &&
        errs[0].error.message.includes('max_iterations'),
    );
    check('4. max_iter: still ends with done', chunks[chunks.length - 1]?.type === 'done');
  }

  // ── 5. Abort before start ────────────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    const registry = new ToolRegistry();
    const policy = PermissionPolicyV2.fromAllowedTools([]);

    const { runtime } = buildRuntime({
      scripts: [
        [
          { kind: 'text_delta', text: 'thinking' },
          { kind: 'message_stop', stop_reason: 'end_turn' },
        ],
      ],
      policy,
      registry,
    });

    const controller = new AbortController();
    controller.abort();

    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'helper',
        user_message: 'go',
        signal: controller.signal,
      }),
    );
    const errs = chunks.filter(
      (c): c is Extract<TurnChunk, { type: 'error' }> => c.type === 'error',
    );
    check('5. abort: exactly one error chunk', errs.length === 1);
    check('5. abort: error kind is timeout', errs[0]?.error?.kind === 'timeout');
    check('5. abort: no done chunk after abort', !chunks.some((c) => c.type === 'done'));
  }

  // ── 6. ApiClient throw mid-stream ────────────────────────────────────────
  {
    _resetToolExecutorsForTests();
    class ThrowingClient implements ApiClient {
      async *stream(): AsyncIterable<AssistantEvent> {
        yield { kind: 'text_delta', text: 'partial...' };
        throw new Error('synthetic provider 500');
      }
    }
    const runner = new ToolRunner(
      new ToolRegistry(),
      PermissionPolicyV2.fromAllowedTools([]),
    );
    const runtime = new ConversationRuntime({
      apiClient: new ThrowingClient(),
      toolRunner: runner,
    });

    const chunks = await drain(
      runtime.runTurn({
        system_prompt: 'helper',
        user_message: 'go',
        signal: new AbortController().signal,
      }),
    );
    const errs = chunks.filter(
      (c): c is Extract<TurnChunk, { type: 'error' }> => c.type === 'error',
    );
    check('6. throw: exactly one error chunk', errs.length === 1);
    check(
      '6. throw: error message mentions synthetic',
      typeof errs[0]?.error?.message === 'string' &&
        errs[0].error.message.includes('synthetic'),
    );
    check('6. throw: no done after throw', !chunks.some((c) => c.type === 'done'));
    check(
      '6. throw: partial text-delta still surfaced',
      chunks.some((c) => c.type === 'text-delta' && c.value === 'partial...'),
    );
  }

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
})().catch((err) => {
  console.error('UNCAUGHT in async IIFE:', err);
  process.exit(1);
});
