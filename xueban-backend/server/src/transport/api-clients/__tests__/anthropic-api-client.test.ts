/**
 * anthropic-api-client.test.ts — S6 AnthropicApiClient → AssistantEvent
 * translation smoke.
 *
 * Run with:  npx tsx src/lib/api-clients/__tests__/anthropic-api-client.test.ts
 *
 * Standalone tsx pattern matching llm-providers/anthropic.test.ts. We
 * monkey-patch `Anthropic.prototype.messages` to intercept the SDK call and
 * feed back scripted stream events.
 *
 * Coverage:
 *   - text_delta event → kind: 'text_delta' yielded with text
 *   - tool_use streaming (content_block_start → input_json_delta × N → content_block_stop)
 *     → kind: 'tool_use' yielded with parsed input
 *   - usage on message_start → kind: 'usage' yielded
 *   - usage on message_delta → kind: 'usage' yielded
 *   - message_stop → kind: 'message_stop' yielded with stop_reason
 *   - Missing API key → throws (not yields)
 *   - tools array passed to SDK with correct shape
 *   - System prompt + messages forwarded to SDK
 *   - Malformed tool_use input JSON → falls back to __parse_error wrapper
 *   - Empty tool_use input JSON → empty object
 *   - signal forwarded to SDK request options
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import Anthropic from '@anthropic-ai/sdk';
import { AnthropicApiClient } from '../anthropic-api-client';
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

function clearEnv(): void {
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.XUEBAN_DEFAULT_MODEL;
}

/**
 * Install a monkey-patched Anthropic SDK stream that yields the scripted
 * events. Returns a restore function and an `inspect` ref capturing the
 * SDK call arguments for assertion.
 *
 * S6-review P1 #1 (2026-05-20): the client no longer calls
 * `stream.finalMessage()` — stop_reason is now sourced from the scripted
 * `message_delta.delta.stop_reason` field directly. The `finalMessage`
 * shim is left in place defensively so any future code path that DOES
 * await it gets the same value; today it's unused.
 */
function patchSdk(scriptedEvents: any[], finalMessageStopReason: string = 'end_turn'): {
  restore: () => void;
  inspect: { streamArgs?: any; streamOptions?: any };
} {
  const probe = new Anthropic({ apiKey: 'probe' });
  const MessagesProto = Object.getPrototypeOf(probe.messages);
  const origStreamFn = MessagesProto.stream;
  const inspect: { streamArgs?: any; streamOptions?: any } = {};

  MessagesProto.stream = function (args: any, options?: any) {
    inspect.streamArgs = args;
    inspect.streamOptions = options;
    const it = (async function* () {
      for (const ev of scriptedEvents) yield ev;
    })();
    (it as any).abort = () => {};
    // Vestigial — client no longer reads this. Kept for defensive parity
    // with the SDK helper interface; safe to delete once we're confident
    // no future caller resurrects finalMessage().
    (it as any).finalMessage = async () => ({ stop_reason: finalMessageStopReason });
    return it;
  };

  return {
    inspect,
    restore: () => {
      MessagesProto.stream = origStreamFn;
    },
  };
}

const PERSONA_TOOL: ToolSpec = {
  name: 'get_skill_anchor',
  description: 'fetch persona',
  input_schema: {
    type: 'object',
    properties: { skill_id: { type: 'string' } },
    required: ['skill_id'],
  },
  source: 'base',
};

async function testNoApiKey(): Promise<void> {
  console.log('\n[anthropic-api-client] NO_API_KEY throws');
  clearEnv();
  const client = new AnthropicApiClient({});
  let threw = false;
  try {
    await collect(
      client.stream({
        system_prompt: 'sys',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
  } catch (err) {
    threw = true;
    checkTruthy(
      'NO_API_KEY: error message mentions API key',
      (err as Error).message.includes('API key'),
    );
  }
  check('NO_API_KEY: throws (does not yield)', true, threw);
}

async function testTextDelta(): Promise<void> {
  console.log('\n[anthropic-api-client] text_delta translation');
  const { restore } = patchSdk([
    { type: 'message_start', message: { usage: { input_tokens: 10 } } },
    { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'hello ' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'world' } },
    // S6-review P1 #1: stop_reason now sourced from message_delta.delta
    { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 5 } },
    { type: 'message_stop' },
  ]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: 'sys',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'hi' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );

    const textDeltas = events.filter((e) => e.kind === 'text_delta');
    check('text_delta: count', 2, textDeltas.length);
    check(
      'text_delta: texts in order',
      ['hello ', 'world'],
      textDeltas.map((e) => (e as { text: string }).text),
    );

    const usages = events.filter((e) => e.kind === 'usage');
    checkTruthy('usage events emitted at least once', usages.length >= 1);

    const stop = events.find((e) => e.kind === 'message_stop');
    checkTruthy('message_stop emitted', stop !== undefined);
    check(
      'message_stop carries stop_reason from message_delta.delta.stop_reason',
      'end_turn',
      (stop as { stop_reason: string }).stop_reason,
    );
  } finally {
    restore();
  }
}

async function testToolUseStream(): Promise<void> {
  console.log('\n[anthropic-api-client] tool_use stream assembly');
  // SDK breaks tool_use input across multiple input_json_delta events.
  const { restore } = patchSdk([
    {
      type: 'content_block_start',
      index: 0,
      content_block: { type: 'tool_use', id: 'toolu_abc', name: 'get_skill_anchor' },
    },
    {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'input_json_delta', partial_json: '{"skill_id":"' },
    },
    {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'input_json_delta', partial_json: 'paper-review"}' },
    },
    { type: 'content_block_stop', index: 0 },
    // S6-review P1 #1: stop_reason on message_delta.delta
    { type: 'message_delta', delta: { stop_reason: 'tool_use' } },
    { type: 'message_stop' },
  ], 'tool_use');
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: 'sys',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'go' }] }],
        tools: [PERSONA_TOOL],
        signal: new AbortController().signal,
      }),
    );

    const toolUse = events.find((e) => e.kind === 'tool_use');
    checkTruthy('tool_use event yielded', toolUse !== undefined);
    if (toolUse && toolUse.kind === 'tool_use') {
      check('tool_use: id', 'toolu_abc', toolUse.id);
      check('tool_use: name', 'get_skill_anchor', toolUse.name);
      check(
        'tool_use: parsed input',
        { skill_id: 'paper-review' },
        toolUse.input,
      );
    }

    const stop = events.find((e) => e.kind === 'message_stop');
    check(
      'message_stop stop_reason=tool_use',
      'tool_use',
      (stop as { stop_reason: string }).stop_reason,
    );
  } finally {
    restore();
  }
}

async function testToolUseEmptyInput(): Promise<void> {
  console.log('\n[anthropic-api-client] tool_use with empty input');
  const { restore } = patchSdk([
    {
      type: 'content_block_start',
      index: 0,
      content_block: { type: 'tool_use', id: 't1', name: 'list_team_agents' },
    },
    // No input_json_delta — tool takes no args.
    { type: 'content_block_stop', index: 0 },
    { type: 'message_stop' },
  ]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    const tu = events.find((e) => e.kind === 'tool_use');
    if (tu && tu.kind === 'tool_use') {
      check('tool_use empty input → {}', {}, tu.input);
    } else {
      check('tool_use empty input → {} (event missing)', true, false);
    }
  } finally {
    restore();
  }
}

async function testToolUseMalformedJson(): Promise<void> {
  console.log('\n[anthropic-api-client] tool_use with malformed JSON');
  const { restore } = patchSdk([
    {
      type: 'content_block_start',
      index: 0,
      content_block: { type: 'tool_use', id: 't2', name: 'foo' },
    },
    {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'input_json_delta', partial_json: 'not valid json' },
    },
    { type: 'content_block_stop', index: 0 },
    { type: 'message_stop' },
  ]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    const tu = events.find((e) => e.kind === 'tool_use');
    if (tu && tu.kind === 'tool_use') {
      check(
        'malformed JSON wrapped in __parse_error',
        { __parse_error: true, raw: 'not valid json' },
        tu.input,
      );
    } else {
      check('tool_use event for malformed input still emitted', true, false);
    }
  } finally {
    restore();
  }
}

async function testSdkCallShape(): Promise<void> {
  console.log('\n[anthropic-api-client] SDK call shape');
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    const ac = new AbortController();
    const client = new AnthropicApiClient({
      apiKey: 'sk-ant-test',
      model: 'claude-custom-1',
      max_tokens: 1234,
      temperature: 0.5,
      // P1: thinking defaults ON and suppresses temperature. This test asserts
      // the general non-thinking call shape (incl. temperature forwarding), so
      // opt out explicitly here. Thinking behavior is covered separately below.
      thinking: false,
    });
    await collect(
      client.stream({
        system_prompt: 'MY-SYS',
        messages: [
          { role: 'user', blocks: [{ kind: 'text', text: 'goal' }] },
          { role: 'assistant', blocks: [{ kind: 'text', text: 'ok' }] },
        ],
        tools: [PERSONA_TOOL],
        signal: ac.signal,
      }),
    );

    const args = inspect.streamArgs!;
    check('SDK call: model', 'claude-custom-1', args.model);
    check('SDK call: max_tokens', 1234, args.max_tokens);
    check('SDK call: temperature', 0.5, args.temperature);
    check('SDK call: system prompt', 'MY-SYS', args.system);
    check('SDK call: messages length', 2, args.messages.length);
    check('SDK call: messages[0].role', 'user', args.messages[0].role);
    check('SDK call: messages[1].role', 'assistant', args.messages[1].role);
    checkTruthy('SDK call: tools array passed', Array.isArray(args.tools) && args.tools.length === 1);
    check('SDK call: tools[0].name', 'get_skill_anchor', args.tools[0].name);
    checkTruthy(
      'SDK call: tools[0].input_schema.type === object',
      args.tools[0].input_schema.type === 'object',
    );
    // Signal forwarded as request option
    checkTruthy(
      'SDK call: signal forwarded to options',
      inspect.streamOptions?.signal === ac.signal,
    );
  } finally {
    restore();
  }
}

async function testNoToolsWhenEmpty(): Promise<void> {
  console.log('\n[anthropic-api-client] tools omitted when empty');
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    // tools should be omitted entirely when empty (SDK rejects empty tools[])
    check(
      'tools omitted when empty',
      true,
      inspect.streamArgs!.tools === undefined,
    );
  } finally {
    restore();
  }
}

async function testEnvKey(): Promise<void> {
  console.log('\n[anthropic-api-client] env ANTHROPIC_API_KEY fallback');
  clearEnv();
  process.env.ANTHROPIC_API_KEY = 'sk-ant-from-env';
  const { restore } = patchSdk([{ type: 'message_stop' }]);
  try {
    const client = new AnthropicApiClient({});
    // Should not throw
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    checkTruthy('env key path: stream completes', events.length >= 1);
  } finally {
    restore();
    clearEnv();
  }
}

async function testStopReasonFromMessageDelta(): Promise<void> {
  console.log('\n[anthropic-api-client] S6-review P1 #1: stop_reason sourced from message_delta.delta');
  // Critical regression test: prior implementation awaited
  // `stream.finalMessage()` to obtain stop_reason, which was both an extra
  // round-trip and brittle on aborted streams. The new code reads it from
  // `message_delta.delta.stop_reason` directly (per Anthropic Messages API
  // canonical source).
  const { restore } = patchSdk(
    [
      { type: 'message_start', message: { usage: { input_tokens: 1 } } },
      { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'done' } },
      {
        type: 'message_delta',
        delta: { stop_reason: 'max_tokens' },
        usage: { output_tokens: 100 },
      },
      { type: 'message_stop' },
    ],
    // finalMessage stub returns 'end_turn' to PROVE the client is NOT
    // reading from finalMessage anymore. If it were, this test would
    // fail with stop_reason='end_turn' instead of 'max_tokens'.
    'end_turn',
  );
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    const stop = events.find((e) => e.kind === 'message_stop');
    check(
      'stop_reason captured from message_delta.delta (NOT finalMessage)',
      'max_tokens',
      (stop as { stop_reason: string }).stop_reason,
    );
  } finally {
    restore();
  }
}

async function testStopReasonFallbackWhenAbsent(): Promise<void> {
  console.log('\n[anthropic-api-client] S6-review P1 #1: stop_reason falls back to "unknown" when delta missing');
  // SDK might emit message_stop without a preceding message_delta carrying
  // stop_reason (e.g. mid-aborted streams). Client should default to
  // 'unknown' rather than throw.
  const { restore } = patchSdk(
    [
      { type: 'message_start', message: { usage: { input_tokens: 1 } } },
      { type: 'message_stop' },
    ],
    'IGNORED-finalMessage-stub',
  );
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    const stop = events.find((e) => e.kind === 'message_stop');
    check(
      'stop_reason defaults to "unknown" when message_delta absent',
      'unknown',
      (stop as { stop_reason: string }).stop_reason,
    );
  } finally {
    restore();
  }
}

async function testThinkingDeltaTranslation(): Promise<void> {
  console.log('\n[anthropic-api-client] P1: thinking_delta translation');
  // Anthropic streams a thinking block (content_block_start type=thinking,
  // then thinking_delta chunks, then a signature_delta) BEFORE the answer.
  const { restore } = patchSdk([
    { type: 'message_start', message: { usage: { input_tokens: 10 } } },
    { type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'Let me ' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'reason...' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature: 'sig-abc' } },
    { type: 'content_block_stop', index: 0 },
    { type: 'content_block_start', index: 1, content_block: { type: 'text', text: '' } },
    { type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: 'answer' } },
    { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 5 } },
    { type: 'message_stop' },
  ]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test' });
    const events = await collect(
      client.stream({
        system_prompt: 'sys',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'hi' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );

    const thinking = events.filter((e) => e.kind === 'thinking_delta');
    check('thinking_delta: count (signature_delta ignored)', 2, thinking.length);
    check(
      'thinking_delta: texts in order',
      ['Let me ', 'reason...'],
      thinking.map((e) => (e as { text: string }).text),
    );
    // Thinking precedes the answer text in event order.
    const firstThinkingIdx = events.findIndex((e) => e.kind === 'thinking_delta');
    const firstTextIdx = events.findIndex((e) => e.kind === 'text_delta');
    checkTruthy('thinking_delta emitted before text_delta', firstThinkingIdx < firstTextIdx);

    const texts = events.filter((e) => e.kind === 'text_delta');
    check('text_delta still translated alongside thinking', ['answer'], texts.map((e) => (e as { text: string }).text));
  } finally {
    restore();
  }
}

async function testThinkingEnabledByDefault(): Promise<void> {
  console.log('\n[anthropic-api-client] P1: thinking enabled by default + temperature suppressed');
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    // temperature provided, but thinking (default ON) must suppress it.
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test', max_tokens: 8192, temperature: 0.2 });
    await collect(
      client.stream({ system_prompt: '', messages: [], tools: [], signal: new AbortController().signal }),
    );
    const args = inspect.streamArgs!;
    checkTruthy('thinking present in request', args.thinking !== undefined);
    check('thinking.type === enabled', 'enabled', args.thinking?.type);
    check('thinking.budget_tokens default 4096', 4096, args.thinking?.budget_tokens);
    check('temperature SUPPRESSED when thinking enabled', undefined, args.temperature);
  } finally {
    restore();
  }
}

async function testThinkingDisabledOptOut(): Promise<void> {
  console.log('\n[anthropic-api-client] P1: thinking:false opts out, temperature returns');
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test', temperature: 0.2, thinking: false });
    await collect(
      client.stream({ system_prompt: '', messages: [], tools: [], signal: new AbortController().signal }),
    );
    const args = inspect.streamArgs!;
    check('thinking omitted when thinking:false', undefined, args.thinking);
    check('temperature forwarded when thinking disabled', 0.2, args.temperature);
  } finally {
    restore();
  }
}

async function testThinkingEnvKillSwitch(): Promise<void> {
  console.log('\n[anthropic-api-client] P1: XUEBAN_ANTHROPIC_THINKING=off disables');
  const prev = process.env.XUEBAN_ANTHROPIC_THINKING;
  process.env.XUEBAN_ANTHROPIC_THINKING = 'off';
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    const client = new AnthropicApiClient({ apiKey: 'sk-ant-test', temperature: 0.2 });
    await collect(
      client.stream({ system_prompt: '', messages: [], tools: [], signal: new AbortController().signal }),
    );
    const args = inspect.streamArgs!;
    check('thinking disabled by env kill-switch', undefined, args.thinking);
    check('temperature forwarded when env-disabled', 0.2, args.temperature);
  } finally {
    restore();
    if (prev === undefined) delete process.env.XUEBAN_ANTHROPIC_THINKING;
    else process.env.XUEBAN_ANTHROPIC_THINKING = prev;
  }
}

async function testThinkingBudgetClamp(): Promise<void> {
  console.log('\n[anthropic-api-client] P1: budget clamped below max_tokens (API: max_tokens > budget)');
  const { restore, inspect } = patchSdk([{ type: 'message_stop' }]);
  try {
    // Request a budget that exceeds max_tokens; must clamp to max_tokens - 1.
    const client = new AnthropicApiClient({
      apiKey: 'sk-ant-test',
      max_tokens: 2000,
      thinking: { budget_tokens: 9000 },
    });
    await collect(
      client.stream({ system_prompt: '', messages: [], tools: [], signal: new AbortController().signal }),
    );
    const args = inspect.streamArgs!;
    check('budget clamped to max_tokens - 1', 1999, args.thinking?.budget_tokens);
    checkTruthy('max_tokens strictly greater than budget', args.max_tokens > args.thinking.budget_tokens);
  } finally {
    restore();
  }
}

async function main(): Promise<void> {
  await testNoApiKey();
  await testTextDelta();
  await testToolUseStream();
  await testToolUseEmptyInput();
  await testToolUseMalformedJson();
  await testSdkCallShape();
  await testNoToolsWhenEmpty();
  await testEnvKey();
  await testStopReasonFromMessageDelta();
  await testStopReasonFallbackWhenAbsent();
  await testThinkingDeltaTranslation();
  await testThinkingEnabledByDefault();
  await testThinkingDisabledOptOut();
  await testThinkingEnvKillSwitch();
  await testThinkingBudgetClamp();

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

void main();
