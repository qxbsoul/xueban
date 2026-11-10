/**
 * claude-code-cli-api-client.test.ts — S14.2 ClaudeCodeCliApiClient →
 * AssistantEvent translation smoke.
 *
 * Run with:
 *   npx tsx src/lib/api-clients/__tests__/claude-code-cli-api-client.test.ts
 *
 * Strategy: monkey-patch `child_process.spawn` to return an EventEmitter that
 * mimics a real subprocess. We don't actually exec the CLI — CI may not have
 * it installed and we don't want to depend on a system binary anyway. The
 * fake subprocess lets us:
 *   - script stdout NDJSON chunk-by-chunk (including mid-line splits)
 *   - assert stdin received the serialized prompt
 *   - assert SIGTERM was delivered on abort
 *   - simulate non-zero exit + stderr to verify error surfacing
 *
 * Coverage:
 *   - text_delta forwarded both via flat content_block_delta and verbose
 *     {type:stream_event, event:{...}} envelopes
 *   - tool_use input_json_delta accumulation across multiple chunks
 *   - mid-chunk line splitting (chunk boundary inside a JSON line)
 *   - message_delta stop_reason cached → emitted on message_stop
 *   - `result` flat terminator drives message_stop when verbose mode dropped it
 *   - non-zero exit code → throws with stderr text
 *   - SIGTERM delivered when AbortSignal fires
 *   - JSON.parse failure on a banner line → warns once + skips
 *   - empty stdout / no message_stop → defensive synthetic emit
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { EventEmitter } from 'node:events';
import { Readable, Writable } from 'node:stream';
import {
  ClaudeCodeCliApiClient,
  __primeClaudeCapability,
  __resetClaudeCapabilityCache,
  type SpawnFn,
} from '../claude-code-cli-api-client';
import type { AssistantEvent } from '../../../lib/conversation-runtime';
import type { ToolSpec } from '../../../lib/tool-spec';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown): void {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

function checkTruthy(label: string, actual: unknown): void {
  if (actual) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}  (expected truthy, got ${JSON.stringify(actual)})`);
  }
}

async function collect(it: AsyncIterable<AssistantEvent>): Promise<AssistantEvent[]> {
  const out: AssistantEvent[] = [];
  for await (const e of it) out.push(e);
  return out;
}

/**
 * FakeChildProcess — minimal EventEmitter that satisfies the subset of the
 * `ChildProcessWithoutNullStreams` surface our client uses:
 *   - stdout: Readable
 *   - stderr: Readable
 *   - stdin: Writable (capture writes for assertion)
 *   - kill(signal): records the signal and emits 'exit'
 *   - on('exit'|'error'): standard EventEmitter
 *
 * Tests script stdout via `pushStdoutChunks` and trigger the exit with
 * `finish(code, signal?)`. To simulate spawn ENOENT, set `simulateSpawnError`
 * which causes the patched spawn() to emit('error', enoent) on next tick.
 */
interface ScriptedSubprocess {
  child: any;
  stdinWrites: string[];
  receivedSignals: string[];
  finish: (code: number, signal?: NodeJS.Signals | null) => void;
  pushStdoutChunks: (chunks: Array<string | Buffer>) => Promise<void>;
  pushStderr: (text: string) => void;
  triggerError: (err: NodeJS.ErrnoException) => void;
}

function makeFakeChild(): ScriptedSubprocess {
  const ee = new EventEmitter() as any;
  const stdinWrites: string[] = [];
  const receivedSignals: string[] = [];

  // stdout / stderr as paused Readables we push into.
  const stdout = new Readable({ read() { /* no-op; pushed externally */ } });
  const stderr = new Readable({ read() { /* no-op */ } });
  const stdin = new Writable({
    write(chunk, _enc, cb) {
      stdinWrites.push(chunk.toString('utf8'));
      cb();
    },
    final(cb) {
      cb();
    },
  });

  ee.stdout = stdout;
  ee.stderr = stderr;
  ee.stdin = stdin;
  ee.killed = false;
  ee.kill = (signal?: NodeJS.Signals): boolean => {
    receivedSignals.push(String(signal ?? 'SIGTERM'));
    ee.killed = true;
    // Realistic: a kill triggers exit with null code + signal.
    setImmediate(() => {
      stdout.push(null);
      stderr.push(null);
      ee.emit('exit', null, signal ?? 'SIGTERM');
    });
    return true;
  };

  return {
    child: ee,
    stdinWrites,
    receivedSignals,
    finish: (code, signal = null) => {
      // EOF stdout/stderr first, then emit exit.
      stdout.push(null);
      stderr.push(null);
      ee.emit('exit', code, signal);
    },
    pushStdoutChunks: async (chunks) => {
      for (const c of chunks) {
        stdout.push(typeof c === 'string' ? Buffer.from(c, 'utf8') : c);
        // Yield to the event loop so the consumer can drain.
        await new Promise((r) => setImmediate(r));
      }
    },
    pushStderr: (text) => {
      stderr.push(Buffer.from(text, 'utf8'));
    },
    triggerError: (err) => {
      setImmediate(() => ee.emit('error', err));
    },
  };
}

/**
 * Build a SpawnFn that returns a scripted fake child each call. `factory` is
 * invoked on each spawn() to allow per-test scripting (test stashes the
 * returned ScriptedSubprocess via the closure-captured ref).
 */
function makeSpawnFn(
  factory: (cmd: string, args: readonly string[], opts: any) => ScriptedSubprocess,
): { spawnFn: SpawnFn; inspect: { spawned: Array<{ cmd: string; args: readonly string[]; opts: any }> } } {
  const inspect = { spawned: [] as Array<{ cmd: string; args: readonly string[]; opts: any }> };
  const spawnFn: SpawnFn = ((cmd, args, opts) => {
    inspect.spawned.push({ cmd, args, opts });
    return factory(cmd, args, opts).child;
  }) as SpawnFn;
  return { spawnFn, inspect };
}

const ECHO_TOOL: ToolSpec = {
  name: 'echo',
  description: 'echo a message',
  input_schema: {
    type: 'object',
    properties: { msg: { type: 'string' } },
    required: ['msg'],
  },
  source: 'base',
};

// ─── tests ─────────────────────────────────────────────────────────────────

async function testFlatTextDelta(): Promise<void> {
  console.log('\n[claude-code-cli] flat (non-verbose) text_delta envelope');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: 'SYS',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'hi' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );

    // Wait a tick for the subprocess to be wired.
    await new Promise((r) => setImmediate(r));

    await sub.pushStdoutChunks([
      JSON.stringify({ type: 'message_start', message: { usage: { input_tokens: 7 } } }) + '\n',
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'hello ' },
      }) + '\n',
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'world' },
      }) + '\n',
      JSON.stringify({
        type: 'message_delta',
        delta: { stop_reason: 'end_turn' },
        usage: { output_tokens: 3 },
      }) + '\n',
      JSON.stringify({ type: 'message_stop' }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;

    const texts = events
      .filter((e) => e.kind === 'text_delta')
      .map((e) => (e as { text: string }).text);
    check('flat text_delta sequence', ['hello ', 'world'], texts);

    const usages = events.filter((e) => e.kind === 'usage') as Array<{
      usage: { input_tokens?: number; output_tokens?: number };
    }>;
    check('flat usage events count', 2, usages.length);
    check('flat usage[0].input_tokens', 7, usages[0]?.usage.input_tokens);
    check('flat usage[1].output_tokens', 3, usages[1]?.usage.output_tokens);

    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check('flat stop_reason routed', 'end_turn', stop?.stop_reason);
  }
}

async function testNestedVerboseEnvelope(): Promise<void> {
  console.log('\n[claude-code-cli] verbose nested {type:stream_event, event:{...}} envelope');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'go' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({ type: 'system', subtype: 'init' }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_start',
          message: { usage: { input_tokens: 4 } },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: '中文 ' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: '回复' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_delta',
          delta: { stop_reason: 'end_turn' },
        },
      }) + '\n',
      JSON.stringify({ type: 'stream_event', event: { type: 'message_stop' } }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;

    check(
      'verbose text concat',
      ['中文 ', '回复'],
      events
        .filter((e) => e.kind === 'text_delta')
        .map((e) => (e as { text: string }).text),
    );
    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check('verbose stop_reason routed', 'end_turn', stop?.stop_reason);
  }
}

async function testToolUseAccumulation(): Promise<void> {
  console.log('\n[claude-code-cli] tool_use input_json_delta accumulation');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: 'use tools',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'compute' }] }],
        tools: [ECHO_TOOL],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    // Verbose envelope w/ tool_use spread across 3 input_json_delta chunks.
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_start',
          index: 0,
          content_block: { type: 'tool_use', id: 'call_xyz', name: 'echo' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: '{"msg":"' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: 'hi th' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: 'ere"}' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: { type: 'content_block_stop', index: 0 },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_delta',
          delta: { stop_reason: 'tool_use' },
        },
      }) + '\n',
      JSON.stringify({ type: 'stream_event', event: { type: 'message_stop' } }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;

    const tu = events.find((e) => e.kind === 'tool_use') as
      | { id: string; name: string; input: unknown }
      | undefined;
    checkTruthy('tool_use emitted', tu !== undefined);
    check('tool_use.id', 'call_xyz', tu?.id);
    check('tool_use.name', 'echo', tu?.name);
    check('tool_use.input glued+parsed', { msg: 'hi there' }, tu?.input);

    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check('tool_use stop_reason', 'tool_use', stop?.stop_reason);
  }
}

async function testMidChunkLineSplit(): Promise<void> {
  console.log('\n[claude-code-cli] mid-chunk line split (JSON line spans 2 stdout chunks)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    const fullLine =
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'split-test' },
      }) + '\n';
    // Cut in half — first chunk has no trailing newline.
    const cut = Math.floor(fullLine.length / 2);
    await sub.pushStdoutChunks([fullLine.slice(0, cut), fullLine.slice(cut)]);
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'message_delta',
        delta: { stop_reason: 'end_turn' },
      }) + '\n',
      JSON.stringify({ type: 'message_stop' }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    check(
      'reassembled split line',
      ['split-test'],
      events
        .filter((e) => e.kind === 'text_delta')
        .map((e) => (e as { text: string }).text),
    );
  }
}

async function testNonZeroExit(): Promise<void> {
  console.log('\n[claude-code-cli] non-zero exit → throws with stderr text');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    sub.pushStderr('Error: not authenticated. Run `claude login`.\n');
    sub.finish(1);

    let threw = false;
    let msg = '';
    try {
      await eventsP;
    } catch (err) {
      threw = true;
      msg = (err as Error).message;
    }
    checkTruthy('throws on non-zero exit', threw);
    checkTruthy(
      `error mentions exit code 1 (got '${msg.slice(0, 80)}…')`,
      msg.includes('code 1'),
    );
    checkTruthy(
      `error includes stderr tail (got '${msg.slice(0, 200)}…')`,
      msg.includes('not authenticated'),
    );
  }
}

async function testAbortSendsSigterm(): Promise<void> {
  console.log('\n[claude-code-cli] abort signal → SIGTERM');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const ac = new AbortController();
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: ac.signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    // Push a partial event then abort before message_stop.
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'partial' },
      }) + '\n',
    ]);
    ac.abort();
    // pile some bytes after abort to make sure consumer notices abort fast
    await new Promise((r) => setImmediate(r));
    // The fake's kill() drives the exit emit automatically.
    try {
      await eventsP;
    } catch {
      /* may throw or not depending on timing; we only assert SIGTERM was sent */
    }
    checkTruthy(
      `SIGTERM delivered (signals=${JSON.stringify(sub.receivedSignals)})`,
      sub.receivedSignals.includes('SIGTERM'),
    );
  }
}

async function testParseFailureWarnOnce(): Promise<void> {
  console.log('\n[claude-code-cli] JSON.parse failure → warn once + skip');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const origWarn = console.warn;
  let warnCount = 0;
  let warnMsg = '';
  console.warn = (...a: unknown[]): void => {
    warnCount++;
    warnMsg = String(a[0] ?? '');
  };
  try {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      'BANNER: claude-code v1.2.3 starting\n',
      'another non-json log line\n',
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'real text' },
      }) + '\n',
      JSON.stringify({ type: 'message_stop' }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    check(
      'real text still extracted past banner',
      ['real text'],
      events.filter((e) => e.kind === 'text_delta').map((e) => (e as any).text),
    );
    check('console.warn fired exactly once across N bad lines', 1, warnCount);
    checkTruthy(
      `warn mentions JSON.parse (got '${warnMsg.slice(0, 80)}')`,
      warnMsg.includes('JSON.parse'),
    );
  } finally {
    console.warn = origWarn;
  }
}

async function testResultTerminator(): Promise<void> {
  console.log('\n[claude-code-cli] `result` flat terminator drives final message_stop');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'done.' },
      }) + '\n',
      // No message_stop — verbose mode dropped it, instead a flat `result`.
      JSON.stringify({
        type: 'result',
        stop_reason: 'end_turn',
        usage: { input_tokens: 1, output_tokens: 2 },
      }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    const stops = events.filter((e) => e.kind === 'message_stop');
    check('exactly one message_stop emitted', 1, stops.length);
    check(
      'result-driven stop_reason',
      'end_turn',
      (stops[0] as { stop_reason: string })?.stop_reason,
    );
    const usages = events.filter((e) => e.kind === 'usage') as Array<{ usage: any }>;
    checkTruthy('usage from result terminator', usages.some((u) => u.usage.output_tokens === 2));
  }
}

async function testEmptyStdoutDefensiveStop(): Promise<void> {
  console.log('\n[claude-code-cli] empty stdout → defensive synthetic message_stop');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    // No chunks, just clean exit.
    sub.finish(0);
    const events = await eventsP;
    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    checkTruthy('defensive message_stop emitted', stop !== undefined);
    check('defensive stop_reason defaults to unknown', 'unknown', stop?.stop_reason);
  }
}

async function testStdinReceivesPrompt(): Promise<void> {
  console.log('\n[claude-code-cli] stdin receives serialized prompt with role labels');
  let sub!: ScriptedSubprocess;
  const { spawnFn, inspect } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', model: 'claude-sonnet-4-6', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: 'You are a helpful agent.',
        messages: [
          { role: 'user', blocks: [{ kind: 'text', text: 'Compute 1+1' }] },
          {
            role: 'assistant',
            blocks: [
              { kind: 'text', text: 'I will use echo.' },
              { kind: 'tool_use', id: 'c1', name: 'echo', input: { msg: 'two' } },
            ],
          },
          {
            role: 'tool',
            blocks: [
              {
                kind: 'tool_result',
                tool_use_id: 'c1',
                tool_name: 'echo',
                output: 'two',
                is_error: false,
              },
            ],
          },
        ],
        tools: [ECHO_TOOL],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    sub.finish(0);
    await eventsP.catch(() => {/* defensive stop emit fine */});

    const allStdin = sub.stdinWrites.join('');
    checkTruthy('prompt contains USER label', allStdin.includes('## USER'));
    checkTruthy('prompt contains ASSISTANT label', allStdin.includes('## ASSISTANT'));
    checkTruthy('prompt contains TOOL label', allStdin.includes('## TOOL'));
    checkTruthy(
      'prompt contains tool_use envelope',
      allStdin.includes('<tool_use name="echo" id="c1">'),
    );
    checkTruthy(
      'prompt contains tool_result envelope',
      allStdin.includes('<tool_result tool_use_id="c1"'),
    );
    checkTruthy('prompt lists available tool', allStdin.includes('echo: echo a message'));

    const spawnCall = inspect.spawned[0];
    checkTruthy(
      `--model forwarded (args=${JSON.stringify(spawnCall.args)})`,
      spawnCall.args.includes('--model') &&
        spawnCall.args[spawnCall.args.indexOf('--model') + 1] === 'claude-sonnet-4-6',
    );
    checkTruthy(
      '--append-system forwarded with system_prompt',
      spawnCall.args.includes('--append-system') &&
        spawnCall.args[spawnCall.args.indexOf('--append-system') + 1] ===
          'You are a helpful agent.',
    );
    checkTruthy(
      '--output-format stream-json forwarded',
      spawnCall.args.includes('stream-json'),
    );
    checkTruthy('--verbose forwarded', spawnCall.args.includes('--verbose'));
  }
}

async function testStreamJsonInputWhenSupported(): Promise<void> {
  console.log('\n[claude-code-cli] --input-format stream-json: native blocks on stdin (no XML)');
  // Force the structured path by priming the capability (skips the help probe).
  __primeClaudeCapability('claude-sj', {
    partialMessages: false,
    addDir: false,
    streamJsonInput: true,
  });
  let sub!: ScriptedSubprocess;
  const { spawnFn, inspect } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude-sj', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [
          { role: 'user', blocks: [{ kind: 'text', text: 'Compute 1+1' }] },
          {
            role: 'assistant',
            blocks: [{ kind: 'tool_use', id: 'c1', name: 'echo', input: { msg: 'two' } }],
          },
          {
            role: 'tool',
            blocks: [
              { kind: 'tool_result', tool_use_id: 'c1', tool_name: 'echo', output: 'two', is_error: false },
            ],
          },
        ],
        tools: [ECHO_TOOL],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    sub.finish(0);
    await eventsP.catch(() => {/* defensive */});

    const spawnCall = inspect.spawned[0];
    checkTruthy(
      `--input-format stream-json forwarded (args=${JSON.stringify(spawnCall.args)})`,
      spawnCall.args.includes('--input-format') &&
        spawnCall.args[spawnCall.args.indexOf('--input-format') + 1] === 'stream-json',
    );
    const allStdin = sub.stdinWrites.join('');
    checkTruthy('stdin has NO <tool_use> XML', !allStdin.includes('<tool_use'));
    checkTruthy('stdin has NO <tool_result> XML', !allStdin.includes('<tool_result'));
    checkTruthy('stdin carries native tool_use block', allStdin.includes('"type":"tool_use"'));
    checkTruthy('stdin carries native tool_result block', allStdin.includes('"type":"tool_result"'));
    checkTruthy('stdin tool_use id preserved', allStdin.includes('"id":"c1"'));
    // Each non-empty stdin line must be valid JSON (proper JSONL).
    const lines = allStdin.split('\n').filter((l) => l.trim().length > 0);
    let allJson = true;
    for (const l of lines) {
      try {
        JSON.parse(l);
      } catch {
        allJson = false;
      }
    }
    checkTruthy('every stdin line is valid JSON (JSONL)', allJson);
  }
}

async function testSpawnEnoent(): Promise<void> {
  console.log('\n[claude-code-cli] spawn ENOENT → install-hint error');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    const enoent: NodeJS.ErrnoException = new Error('spawn claude ENOENT');
    enoent.code = 'ENOENT';
    sub.triggerError(enoent);
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    // Even with ENOENT the child still emits its own exit via the EE — fire
    // it to unblock the await exitPromise inside the client.
    sub.finish(127);

    let threw = false;
    let msg = '';
    try {
      await eventsP;
    } catch (err) {
      threw = true;
      msg = (err as Error).message;
    }
    checkTruthy('throws on ENOENT', threw);
    checkTruthy(
      `error mentions install hint (got '${msg.slice(0, 200)}…')`,
      msg.includes('npm i') || msg.includes('not found') || msg.includes('claude'),
    );
  }
}

// ─── S14.2 follow-up: 6 new fix coverage tests ─────────────────────────────

/**
 * fix 1: extended-thinking chunks arrive as content_block_delta thinking_delta
 * inside a `thinking` block. We must buffer + emit ONE text_delta at
 * content_block_stop wrapped in <sf:thinking step="extended" origin="cli">.
 */
async function testThinkingDeltaTyped(): Promise<void> {
  console.log('\n[claude-code-cli] thinking → typed thinking_delta kind (NOT wrapped text)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'think' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_start',
          index: 0,
          content_block: { type: 'thinking' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'thinking_delta', thinking: 'Step 1: parse the request. ' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'thinking_delta', thinking: 'Step 2: form a plan.' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: { type: 'content_block_stop', index: 0 },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_delta',
          delta: { stop_reason: 'end_turn' },
        },
      }) + '\n',
      JSON.stringify({ type: 'stream_event', event: { type: 'message_stop' } }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    // Gap-close (2026-05-31): thinking is a typed channel now — it must NOT be
    // wrapped as <sf:thinking> text_delta. Exactly one thinking_delta carrying
    // the concatenated buffer, and zero text_delta.
    const texts = events
      .filter((e) => e.kind === 'text_delta')
      .map((e) => (e as { text: string }).text);
    check('no text_delta (thinking is not text)', 0, texts.length);
    const thinks = events
      .filter((e) => e.kind === 'thinking_delta')
      .map((e) => (e as { text: string }).text);
    check('exactly one thinking_delta', 1, thinks.length);
    check(
      'thinking_delta carries concatenated buffer (no <sf:thinking> wrapper)',
      'Step 1: parse the request. Step 2: form a plan.',
      thinks[0],
    );
  }
}

/**
 * fix 2 (no prior streaming): assistant wrapper fallback when the CLI didn't
 * emit `stream_event` lines. text/thinking/tool_use should all surface.
 */
async function testAssistantWrapperFallback(): Promise<void> {
  console.log('\n[claude-code-cli] assistant wrapper fallback (no stream_event)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'go' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    // ONLY an assistant wrapper line + result terminator — older Claude Code.
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'assistant',
        message: {
          id: 'msg_legacy_1',
          stop_reason: 'tool_use',
          content: [
            { type: 'text', text: 'Here is the plan.' },
            { type: 'thinking', thinking: 'reasoning bits' },
            { type: 'tool_use', id: 'call_legacy_a', name: 'echo', input: { msg: 'hi' } },
          ],
        },
      }) + '\n',
      JSON.stringify({
        type: 'result',
        stop_reason: 'tool_use',
        usage: { input_tokens: 5, output_tokens: 10 },
      }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;

    const texts = events
      .filter((e) => e.kind === 'text_delta')
      .map((e) => (e as { text: string }).text);
    // Gap-close (2026-05-31): only the plain text is a text_delta now; thinking
    // surfaces on its own typed channel (not wrapped as <sf:thinking> text).
    check('one text_delta (plain text only)', 1, texts.length);
    checkTruthy(
      `plain text surfaced (got ${JSON.stringify(texts)})`,
      texts.some((t) => t === 'Here is the plan.'),
    );
    const thinks = events
      .filter((e) => e.kind === 'thinking_delta')
      .map((e) => (e as { text: string }).text);
    check('one thinking_delta (typed, not wrapped text)', 1, thinks.length);
    checkTruthy(
      `thinking surfaced on its own channel (got ${JSON.stringify(thinks)})`,
      thinks.some((t) => t === 'reasoning bits'),
    );

    const tools = events.filter((e) => e.kind === 'tool_use') as Array<{
      id: string;
      name: string;
      input: unknown;
    }>;
    check('one tool_use from assistant wrapper', 1, tools.length);
    check('tool_use.id', 'call_legacy_a', tools[0]?.id);
    check('tool_use.name', 'echo', tools[0]?.name);
    check('tool_use.input', { msg: 'hi' }, tools[0]?.input);
  }
}

/**
 * fix 2 (dedup): when both stream_event AND assistant wrapper carry the same
 * tool_use id, emit ONCE — the wrapper's repeat is suppressed.
 */
async function testAssistantWrapperDedupToolUse(): Promise<void> {
  console.log('\n[claude-code-cli] assistant wrapper dedups already-streamed tool_use');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_start',
          message: { id: 'msg_dup_1', usage: { input_tokens: 1 } },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_start',
          index: 0,
          content_block: { type: 'tool_use', id: 't1', name: 'echo' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: '{"msg":"once"}' },
        },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: { type: 'content_block_stop', index: 0 },
      }) + '\n',
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_delta',
          delta: { stop_reason: 'tool_use' },
        },
      }) + '\n',
      JSON.stringify({ type: 'stream_event', event: { type: 'message_stop' } }) + '\n',
      // Wrapper re-emits the same id — must be suppressed.
      JSON.stringify({
        type: 'assistant',
        message: {
          id: 'msg_dup_1',
          stop_reason: 'tool_use',
          content: [
            { type: 'tool_use', id: 't1', name: 'echo', input: { msg: 'once' } },
          ],
        },
      }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    const tools = events.filter((e) => e.kind === 'tool_use');
    check('tool_use emitted exactly once across stream_event + wrapper', 1, tools.length);
  }
}

/**
 * fix 3: when the first spawn synchronously throws ENOENT, the client walks
 * `fallbackBins` and uses the first one that spawns OK.
 */
async function testFallbackBinsTriggered(): Promise<void> {
  console.log('\n[claude-code-cli] fallbackBins picks up `openclaude` when claude ENOENTs');
  let sub!: ScriptedSubprocess;
  let attemptedCommands: string[] = [];
  const spawnFn: SpawnFn = ((cmd, _args, _opts) => {
    attemptedCommands.push(cmd);
    if (cmd === 'claude') {
      const enoent: NodeJS.ErrnoException = new Error('spawn claude ENOENT');
      enoent.code = 'ENOENT';
      throw enoent;
    }
    // openclaude succeeds.
    sub = makeFakeChild();
    return sub.child;
  }) as SpawnFn;
  {
    const client = new ClaudeCodeCliApiClient({
      binPath: 'claude',
      fallbackBins: ['openclaude'],
      spawnFn,
      // Skip probe (probe would also throw and complicate the test).
      capabilities: { partialMessages: false, addDir: false },
    });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'from openclaude' },
      }) + '\n',
      JSON.stringify({ type: 'message_stop' }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    check('attempted [claude, openclaude] in order', ['claude', 'openclaude'], attemptedCommands);
    const texts = events
      .filter((e) => e.kind === 'text_delta')
      .map((e) => (e as { text: string }).text);
    check('text streamed from fallback', ['from openclaude'], texts);
  }
}

/**
 * fix 4: capability probe is cached per binPath. Two stream() calls trigger
 * only ONE probe spawn (+ N main spawns).
 */
async function testCapabilityProbeCached(): Promise<void> {
  console.log('\n[claude-code-cli] capability probe is cached across stream() calls');
  __resetClaudeCapabilityCache();

  // Track spawn calls. The probe spawns with args [-p, --help]; the main
  // stream spawn carries -p, --output-format, stream-json, ...
  let probeCount = 0;
  let mainCount = 0;
  const subs: ScriptedSubprocess[] = [];
  const spawnFn: SpawnFn = ((_cmd, args, _opts) => {
    const isProbe =
      args.length === 2 && args[0] === '-p' && args[1] === '--help';
    if (isProbe) {
      probeCount++;
      const sub = makeFakeChild();
      // Probe child must emit `exit` so the probe Promise resolves; we don't
      // push any stdout (so partialMessages stays false), then finish.
      setImmediate(() => sub.finish(0));
      return sub.child;
    }
    mainCount++;
    const sub = makeFakeChild();
    subs.push(sub);
    return sub.child;
  }) as SpawnFn;

  const client = new ClaudeCodeCliApiClient({ binPath: 'claude-probe-test', spawnFn });

  // Stream #1.
  const p1 = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  // Yield enough for probe → main spawn → stdin write to resolve.
  await new Promise((r) => setTimeout(r, 10));
  subs[0]?.finish(0);
  await p1;

  // Stream #2 — should NOT trigger a second probe spawn.
  const p2 = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setTimeout(r, 10));
  subs[1]?.finish(0);
  await p2;

  check('probe spawned exactly once (cached on stream #2)', 1, probeCount);
  check('main spawned twice (one per stream() call)', 2, mainCount);
}

/**
 * Checker P1-1: abort listener removed in `finally` even on throw paths.
 * Verify by spy-overriding addEventListener / removeEventListener on a fresh
 * AbortSignal — counts must balance after a non-zero-exit throw.
 */
async function testAbortListenerCleanedOnThrow(): Promise<void> {
  console.log('\n[claude-code-cli] abort listener cleanup on throw (Checker P1-1)');
  // Re-prime: testCapabilityProbeCached reset the cache, so without this the
  // `claude` probe would fire and block waiting for an exit on the fake.
  __primeClaudeCapability('claude', { partialMessages: false, addDir: false });
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });

  const ac = new AbortController();
  const signal = ac.signal;
  const origAdd = signal.addEventListener.bind(signal);
  const origRem = signal.removeEventListener.bind(signal);
  let addCount = 0;
  let removeCount = 0;
  (signal as any).addEventListener = (
    type: string,
    listener: any,
    opts?: any,
  ): void => {
    if (type === 'abort') addCount++;
    origAdd(type, listener, opts);
  };
  (signal as any).removeEventListener = (
    type: string,
    listener: any,
    opts?: any,
  ): void => {
    if (type === 'abort') removeCount++;
    origRem(type, listener, opts);
  };

  const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  // Trigger a throw path: non-zero exit.
  sub.pushStderr('boom\n');
  sub.finish(1);
  try {
    await eventsP;
  } catch {
    /* expected */
  }
  checkTruthy(`abort listener was added (count=${addCount})`, addCount >= 1);
  check(
    'abort listener cleanup count matches add count after throw',
    addCount,
    removeCount,
  );
}

// ─── S14.3: CLI-only telemetry (cost_usd / duration_ms / ttft_ms) ──────────

/**
 * The `result` terminator carries `total_cost_usd` + `duration_ms` alongside
 * the usual usage payload. They must be folded into the TokenUsage we yield
 * (not silently dropped) so the runtime can accumulate them per session.
 */
async function testCostDurationFromResult(): Promise<void> {
  console.log('\n[claude-code-cli] result.total_cost_usd + duration_ms folded into usage');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'result',
        stop_reason: 'end_turn',
        total_cost_usd: 0.0042,
        duration_ms: 1234,
        usage: { input_tokens: 100, output_tokens: 50 },
      }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    const usages = events.filter((e) => e.kind === 'usage') as Array<{ usage: any }>;
    checkTruthy('result-driven usage emitted', usages.length > 0);
    const u = usages[usages.length - 1].usage;
    check('result.total_cost_usd folded as cost_usd', 0.0042, u.cost_usd);
    check('result.duration_ms folded as duration_ms', 1234, u.duration_ms);
    check('result usage.input_tokens preserved', 100, u.input_tokens);
    check('result usage.output_tokens preserved', 50, u.output_tokens);
  }
}

/**
 * Verbose `stream_event.message_start` carries `ttft_ms` on the inner event
 * envelope itself (not inside `message.usage`). We must lift it into the
 * TokenUsage payload so it survives the usage channel.
 */
async function testTtftFromMessageStart(): Promise<void> {
  console.log('\n[claude-code-cli] stream_event.message_start.ttft_ms folded into usage');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  {
    const client = new ClaudeCodeCliApiClient({ binPath: 'claude', spawnFn });
    const eventsP = collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    await new Promise((r) => setImmediate(r));
    await sub.pushStdoutChunks([
      JSON.stringify({
        type: 'stream_event',
        event: {
          type: 'message_start',
          ttft_ms: 87,
          message: { id: 'msg_1', usage: { input_tokens: 100, output_tokens: 1 } },
        },
      }) + '\n',
      JSON.stringify({ type: 'stream_event', event: { type: 'message_stop' } }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    const usages = events.filter((e) => e.kind === 'usage') as Array<{ usage: any }>;
    checkTruthy('message_start usage emitted', usages.length > 0);
    const u = usages[0].usage;
    check('message_start.ttft_ms folded as ttft_ms', 87, u.ttft_ms);
    check('message_start usage.input_tokens preserved', 100, u.input_tokens);
    check('message_start usage.output_tokens preserved', 1, u.output_tokens);
  }
}

async function main(): Promise<void> {
  // Pre-seed the capability cache so the existing tests don't have to script
  // a `<bin> -p --help` probe child. The probe-itself test below resets +
  // re-tests via `probeClaudeCapabilities` directly.
  __primeClaudeCapability('claude', { partialMessages: false, addDir: false });

  await testFlatTextDelta();
  await testNestedVerboseEnvelope();
  await testToolUseAccumulation();
  await testMidChunkLineSplit();
  await testNonZeroExit();
  await testAbortSendsSigterm();
  await testParseFailureWarnOnce();
  await testResultTerminator();
  await testEmptyStdoutDefensiveStop();
  await testStdinReceivesPrompt();
  await testStreamJsonInputWhenSupported();
  await testSpawnEnoent();

  // S14.2 follow-up — 6 new fixes
  await testThinkingDeltaTyped();
  await testAssistantWrapperFallback();
  await testAssistantWrapperDedupToolUse();
  await testFallbackBinsTriggered();
  await testCapabilityProbeCached();
  await testAbortListenerCleanedOnThrow();

  // S14.3 — CLI-only telemetry surfacing
  await testCostDurationFromResult();
  await testTtftFromMessageStart();

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

void main();
