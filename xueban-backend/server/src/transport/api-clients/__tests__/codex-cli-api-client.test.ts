/**
 * codex-cli-api-client.test.ts — S14.2 CodexCliApiClient → AssistantEvent
 * translation smoke.
 *
 * Run with:
 *   npx tsx src/lib/api-clients/__tests__/codex-cli-api-client.test.ts
 *
 * Same DI-via-spawnFn pattern as claude-code-cli-api-client.test.ts. We script
 * a fake subprocess and assert the AssistantEvent stream. NB: the codex CLI's
 * actual JSONL shape is partially documented (Responses API mirror) — these
 * tests pin the EXPECTED shape per our parser; the parser is marked
 * EXPERIMENTAL in source until verified against a real binary.
 *
 * Coverage:
 *   - text_delta (string form + nested {text: ...} form)
 *   - tool-call lifecycle: added → arguments.delta×N → done → tool_use event
 *   - tool-call args-on-done fallback (no streaming deltas)
 *   - response.completed.usage → 'usage' + 'message_stop' end_turn
 *   - hadToolUse → stop_reason 'tool_use'
 *   - response.failed → throws with error message
 *   - non-zero exit → throws with stderr
 *   - abort signal → SIGTERM
 *   - JSON.parse failure → warn once, skip
 *   - mid-chunk line split
 *   - stdin receives prompt + system + tools
 *   - spawn ENOENT → install hint
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { EventEmitter } from 'node:events';
import { Readable, Writable } from 'node:stream';
import { CodexCliApiClient, type SpawnFn } from '../codex-cli-api-client';
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

interface ScriptedSubprocess {
  child: any;
  stdinWrites: string[];
  receivedSignals: string[];
  stdoutEncodings: string[];
  stderrEncodings: string[];
  finish: (code: number, signal?: NodeJS.Signals | null) => void;
  pushStdoutChunks: (chunks: Array<string | Buffer>) => Promise<void>;
  pushStderr: (text: string) => void;
  triggerError: (err: NodeJS.ErrnoException) => void;
}

function makeFakeChild(): ScriptedSubprocess & {
  stdoutEncodings: string[];
  stderrEncodings: string[];
} {
  const ee = new EventEmitter() as any;
  const stdinWrites: string[] = [];
  const receivedSignals: string[] = [];
  const stdoutEncodings: string[] = [];
  const stderrEncodings: string[] = [];
  const stdout = new Readable({ read() {} });
  const stderr = new Readable({ read() {} });
  // Spy on setEncoding so tests can assert the P1-2 fix (utf8 boundary safety).
  const origStdoutSetEncoding = stdout.setEncoding.bind(stdout);
  stdout.setEncoding = ((enc: BufferEncoding) => {
    stdoutEncodings.push(String(enc));
    return origStdoutSetEncoding(enc);
  }) as typeof stdout.setEncoding;
  const origStderrSetEncoding = stderr.setEncoding.bind(stderr);
  stderr.setEncoding = ((enc: BufferEncoding) => {
    stderrEncodings.push(String(enc));
    return origStderrSetEncoding(enc);
  }) as typeof stderr.setEncoding;
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
    stdoutEncodings,
    stderrEncodings,
    finish: (code, signal = null) => {
      stdout.push(null);
      stderr.push(null);
      ee.emit('exit', code, signal);
    },
    pushStdoutChunks: async (chunks) => {
      for (const c of chunks) {
        stdout.push(typeof c === 'string' ? Buffer.from(c, 'utf8') : c);
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

async function testTextDeltaStringForm(): Promise<void> {
  console.log('\n[codex-cli] text_delta (delta: string form)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: 'sys',
      messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'hi' }] }],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  await sub.pushStdoutChunks([
    JSON.stringify({ type: 'response.created' }) + '\n',
    JSON.stringify({ type: 'response.output_text.delta', delta: 'hello ' }) + '\n',
    JSON.stringify({ type: 'response.output_text.delta', delta: 'world' }) + '\n',
    JSON.stringify({
      type: 'response.completed',
      response: { status: 'completed', usage: { input_tokens: 5, output_tokens: 2 } },
    }) + '\n',
  ]);
  sub.finish(0);
  const events = await eventsP;

  check(
    'text deltas in order',
    ['hello ', 'world'],
    events.filter((e) => e.kind === 'text_delta').map((e) => (e as any).text),
  );
  const usage = events.find((e) => e.kind === 'usage') as
    | { usage: { input_tokens?: number; output_tokens?: number } }
    | undefined;
  check('usage.input_tokens', 5, usage?.usage.input_tokens);
  check('usage.output_tokens', 2, usage?.usage.output_tokens);
  const stop = events.find((e) => e.kind === 'message_stop') as
    | { stop_reason: string }
    | undefined;
  check('end_turn stop_reason', 'end_turn', stop?.stop_reason);
}

async function testTextDeltaNestedForm(): Promise<void> {
  console.log('\n[codex-cli] text_delta (delta: {text} form, older builds)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
      type: 'response.output_text.delta',
      delta: { text: '嵌套' },
    }) + '\n',
    JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
  ]);
  sub.finish(0);
  const events = await eventsP;
  check(
    'nested .delta.text extracted',
    ['嵌套'],
    events.filter((e) => e.kind === 'text_delta').map((e) => (e as any).text),
  );
}

async function testToolCallLifecycle(): Promise<void> {
  console.log('\n[codex-cli] function_call lifecycle (added → args.delta×3 → done)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'echo two' }] }],
      tools: [ECHO_TOOL],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  await sub.pushStdoutChunks([
    JSON.stringify({
      type: 'response.output_item.added',
      item: { type: 'function_call', id: 'fc_001', name: 'echo' },
    }) + '\n',
    JSON.stringify({
      type: 'response.function_call_arguments.delta',
      item_id: 'fc_001',
      delta: '{"msg":"',
    }) + '\n',
    JSON.stringify({
      type: 'response.function_call_arguments.delta',
      item_id: 'fc_001',
      delta: 'two"',
    }) + '\n',
    JSON.stringify({
      type: 'response.function_call_arguments.delta',
      item_id: 'fc_001',
      delta: '}',
    }) + '\n',
    JSON.stringify({
      type: 'response.output_item.done',
      item: { type: 'function_call', id: 'fc_001', name: 'echo' },
    }) + '\n',
    JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
  ]);
  sub.finish(0);
  const events = await eventsP;

  const tu = events.find((e) => e.kind === 'tool_use') as
    | { id: string; name: string; input: any }
    | undefined;
  checkTruthy('tool_use emitted', tu !== undefined);
  check('tool_use.id', 'fc_001', tu?.id);
  check('tool_use.name', 'echo', tu?.name);
  check('tool_use.input glued+parsed', { msg: 'two' }, tu?.input);

  const stop = events.find((e) => e.kind === 'message_stop') as
    | { stop_reason: string }
    | undefined;
  check('hadToolUse → stop_reason=tool_use', 'tool_use', stop?.stop_reason);
}

async function testToolCallArgsOnDoneFallback(): Promise<void> {
  console.log('\n[codex-cli] function_call: args attached to `done` event (no deltas)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
      type: 'response.output_item.added',
      item: { type: 'function_call', id: 'fc_x', name: 'echo' },
    }) + '\n',
    // Skip arguments.delta entirely — args land on `done.item.arguments`.
    JSON.stringify({
      type: 'response.output_item.done',
      item: {
        type: 'function_call',
        id: 'fc_x',
        name: 'echo',
        arguments: '{"msg":"done-fallback"}',
      },
    }) + '\n',
    JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
  ]);
  sub.finish(0);
  const events = await eventsP;
  const tu = events.find((e) => e.kind === 'tool_use') as
    | { input: { msg: string } }
    | undefined;
  check('args-on-done fallback parsed', { msg: 'done-fallback' }, tu?.input);
}

async function testResponseFailedThrows(): Promise<void> {
  console.log('\n[codex-cli] response.failed → throws with reason');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
      type: 'response.failed',
      response: { status: 'failed', incomplete_details: { reason: 'content_filter' } },
    }) + '\n',
  ]);
  sub.finish(0);

  let threw = false;
  let msg = '';
  try {
    await eventsP;
  } catch (err) {
    threw = true;
    msg = (err as Error).message;
  }
  checkTruthy('throws on response.failed', threw);
  checkTruthy(
    `error mentions reason (got '${msg.slice(0, 80)}')`,
    msg.includes('content_filter'),
  );
}

async function testNonZeroExit(): Promise<void> {
  console.log('\n[codex-cli] non-zero exit → throws with stderr');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  sub.pushStderr('Error: OPENAI_API_KEY not set\n');
  sub.finish(2);

  let threw = false;
  let msg = '';
  try {
    await eventsP;
  } catch (err) {
    threw = true;
    msg = (err as Error).message;
  }
  checkTruthy('throws on non-zero exit', threw);
  checkTruthy(`error mentions code 2 (got '${msg.slice(0, 80)}')`, msg.includes('code 2'));
  checkTruthy(
    'error includes stderr tail',
    msg.includes('OPENAI_API_KEY'),
  );
}

async function testAbortSendsSigterm(): Promise<void> {
  console.log('\n[codex-cli] abort signal → SIGTERM');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
  await sub.pushStdoutChunks([
    JSON.stringify({ type: 'response.output_text.delta', delta: 'partial' }) + '\n',
  ]);
  ac.abort();
  await new Promise((r) => setImmediate(r));
  try {
    await eventsP;
  } catch {
    /* may throw or not — assert signal */
  }
  checkTruthy(
    `SIGTERM delivered (signals=${JSON.stringify(sub.receivedSignals)})`,
    sub.receivedSignals.includes('SIGTERM'),
  );
}

async function testParseFailureWarnOnce(): Promise<void> {
  console.log('\n[codex-cli] JSON.parse failure → warn once + skip');
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
    const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
      'banner v0.42 starting\n',
      'second log line\n',
      JSON.stringify({ type: 'response.output_text.delta', delta: 'real' }) + '\n',
      JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
    ]);
    sub.finish(0);
    const events = await eventsP;
    check(
      'real text extracted past banner',
      ['real'],
      events.filter((e) => e.kind === 'text_delta').map((e) => (e as any).text),
    );
    check('console.warn fired exactly once', 1, warnCount);
    checkTruthy(
      `warn mentions JSON.parse (got '${warnMsg.slice(0, 80)}')`,
      warnMsg.includes('JSON.parse'),
    );
  } finally {
    console.warn = origWarn;
  }
}

async function testMidChunkLineSplit(): Promise<void> {
  console.log('\n[codex-cli] mid-chunk line split');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  const full = JSON.stringify({ type: 'response.output_text.delta', delta: 'split' }) + '\n';
  const cut = Math.floor(full.length / 2);
  await sub.pushStdoutChunks([full.slice(0, cut), full.slice(cut)]);
  await sub.pushStdoutChunks([
    JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
  ]);
  sub.finish(0);
  const events = await eventsP;
  check(
    'reassembled split line',
    ['split'],
    events.filter((e) => e.kind === 'text_delta').map((e) => (e as any).text),
  );
}

async function testStdinReceivesPrompt(): Promise<void> {
  console.log('\n[codex-cli] stdin receives serialized prompt + system + tools');
  let sub!: ScriptedSubprocess;
  const { spawnFn, inspect } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({
    binPath: 'codex',
    model: 'o4-mini',
    spawnFn,
  });
  const eventsP = collect(
    client.stream({
      system_prompt: 'You are codex.',
      messages: [
        { role: 'user', blocks: [{ kind: 'text', text: 'Compute' }] },
        {
          role: 'assistant',
          blocks: [
            { kind: 'text', text: 'Plan' },
            { kind: 'tool_use', id: 'fc_p', name: 'echo', input: { msg: 'x' } },
          ],
        },
        {
          role: 'tool',
          blocks: [
            {
              kind: 'tool_result',
              tool_use_id: 'fc_p',
              tool_name: 'echo',
              output: 'x',
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
  await eventsP.catch(() => {/* defensive stop is fine */});

  const allStdin = sub.stdinWrites.join('');
  checkTruthy('prompt contains SYSTEM section', allStdin.includes('## SYSTEM'));
  checkTruthy('prompt contains system text', allStdin.includes('You are codex.'));
  checkTruthy('prompt contains USER label', allStdin.includes('## USER'));
  checkTruthy('prompt contains ASSISTANT label', allStdin.includes('## ASSISTANT'));
  checkTruthy('prompt contains TOOL label', allStdin.includes('## TOOL'));
  checkTruthy(
    'prompt contains tool_use envelope',
    allStdin.includes('<tool_use name="echo" id="fc_p">'),
  );
  checkTruthy(
    'prompt contains tool_result envelope',
    allStdin.includes('<tool_result tool_use_id="fc_p"'),
  );
  checkTruthy('prompt lists tools', allStdin.includes('echo: echo a message'));

  const spawned = inspect.spawned[0];
  checkTruthy(
    `--stream forwarded (args=${JSON.stringify(spawned.args)})`,
    spawned.args.includes('--stream'),
  );
  checkTruthy(
    '--model o4-mini forwarded',
    spawned.args.includes('--model') &&
      spawned.args[spawned.args.indexOf('--model') + 1] === 'o4-mini',
  );
}

async function testSpawnEnoent(): Promise<void> {
  console.log('\n[codex-cli] spawn ENOENT → install-hint error');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    const enoent: NodeJS.ErrnoException = new Error('spawn codex ENOENT');
    enoent.code = 'ENOENT';
    sub.triggerError(enoent);
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: new AbortController().signal,
    }),
  );
  await new Promise((r) => setImmediate(r));
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
    `error mentions install hint (got '${msg.slice(0, 200)}')`,
    msg.includes('@openai/codex') || msg.includes('not found'),
  );
}

// ─── S14.2 follow-up P1 regression tests ──────────────────────────────────

async function testAbortListenerCleanedOnThrow(): Promise<void> {
  console.log('\n[codex-cli] P1-1: abort listener removed on throw path (non-zero exit)');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  // Spy AbortSignal: track add/removeEventListener calls for 'abort'.
  const ac = new AbortController();
  const realSignal = ac.signal;
  const added: Array<(...a: unknown[]) => void> = [];
  const removed: Array<(...a: unknown[]) => void> = [];
  const spySignal = new Proxy(realSignal, {
    get(target, prop, receiver) {
      if (prop === 'addEventListener') {
        return (type: string, listener: (...a: unknown[]) => void, opts?: unknown) => {
          if (type === 'abort') added.push(listener);
          return (target.addEventListener as any).call(target, type, listener, opts);
        };
      }
      if (prop === 'removeEventListener') {
        return (type: string, listener: (...a: unknown[]) => void, opts?: unknown) => {
          if (type === 'abort') removed.push(listener);
          return (target.removeEventListener as any).call(target, type, listener, opts);
        };
      }
      const v = Reflect.get(target, prop, receiver);
      return typeof v === 'function' ? v.bind(target) : v;
    },
  }) as AbortSignal;
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
  const eventsP = collect(
    client.stream({
      system_prompt: '',
      messages: [],
      tools: [],
      signal: spySignal,
    }),
  );
  await new Promise((r) => setImmediate(r));
  sub.pushStderr('boom\n');
  sub.finish(1);
  let threw = false;
  try {
    await eventsP;
  } catch {
    threw = true;
  }
  checkTruthy('non-zero exit threw', threw);
  check('addEventListener(abort) called once', 1, added.length);
  checkTruthy(
    `removeEventListener(abort) called for same fn (added=${added.length}, removed=${removed.length})`,
    removed.length >= 1 && added[0] === removed[0],
  );
}

async function testStdoutEncodingUtf8(): Promise<void> {
  console.log('\n[codex-cli] P1-2: child.stdout/stderr.setEncoding("utf8") called');
  let sub!: ScriptedSubprocess;
  const { spawnFn } = makeSpawnFn(() => {
    sub = makeFakeChild();
    return sub;
  });
  const client = new CodexCliApiClient({ binPath: 'codex', spawnFn });
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
    JSON.stringify({ type: 'response.output_text.delta', delta: '你好' }) + '\n',
    JSON.stringify({ type: 'response.completed', response: { status: 'completed' } }) + '\n',
  ]);
  sub.finish(0);
  await eventsP.catch(() => {});

  checkTruthy(
    `stdout.setEncoding('utf8') called (encs=${JSON.stringify(sub.stdoutEncodings)})`,
    sub.stdoutEncodings.includes('utf8'),
  );
  checkTruthy(
    `stderr.setEncoding('utf8') called (encs=${JSON.stringify(sub.stderrEncodings)})`,
    sub.stderrEncodings.includes('utf8'),
  );
}

async function main(): Promise<void> {
  await testTextDeltaStringForm();
  await testTextDeltaNestedForm();
  await testToolCallLifecycle();
  await testToolCallArgsOnDoneFallback();
  await testResponseFailedThrows();
  await testNonZeroExit();
  await testAbortSendsSigterm();
  await testParseFailureWarnOnce();
  await testMidChunkLineSplit();
  await testStdinReceivesPrompt();
  await testSpawnEnoent();
  await testAbortListenerCleanedOnThrow();
  await testStdoutEncodingUtf8();

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

void main();
