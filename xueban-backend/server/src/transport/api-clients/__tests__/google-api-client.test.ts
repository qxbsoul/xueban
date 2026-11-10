/**
 * google-api-client.test.ts — S14.1 GoogleApiClient → AssistantEvent
 * translation smoke.
 *
 * Run with:  npx tsx src/lib/api-clients/__tests__/google-api-client.test.ts
 *
 * Monkey-patch `GenerativeModel.prototype.generateContentStream` to return a
 * scripted async-iterable of chunk objects mirroring the Gemini SDK shape.
 *
 * Coverage:
 *   - text streaming → text_delta events in order
 *   - functionCall part → tool_use event with synthesized id + parsed args
 *   - functionResponse round-trip: tool_result block converted to user/
 *     functionResponse part in toGeminiContents
 *   - finishReason normalization (STOP → end_turn; STOP + functionCall →
 *     tool_use; MAX_TOKENS → max_tokens)
 *   - usageMetadata extraction
 *   - missing api_key throws
 *   - safetySettings NOT injected by default (regression guard)
 *   - SDK ModelParams shape: systemInstruction + tools.functionDeclarations
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { GoogleGenerativeAI } from '@google/generative-ai';
import {
  GoogleApiClient,
  toGeminiContents,
} from '../google-api-client';
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
 * Patch the GenerativeModel prototype. The SDK exposes `getGenerativeModel`
 * which returns an instance whose `generateContentStream` we hijack to return
 * a scripted iterable. We also capture the ModelParams passed at
 * `getGenerativeModel` call time.
 */
function patchSdk(scriptedChunks: any[]): {
  restore: () => void;
  inspect: { modelParams?: any; streamParams?: any; streamOptions?: any };
} {
  const inspect: {
    modelParams?: any;
    streamParams?: any;
    streamOptions?: any;
  } = {};

  // Reach prototype via a throwaway instance.
  const probeAI = new GoogleGenerativeAI('probe');
  const origGetModel = probeAI.getGenerativeModel.bind(probeAI);
  const probeModel = origGetModel({ model: 'probe-model' });
  const ModelProto = Object.getPrototypeOf(probeModel);
  const origStream = ModelProto.generateContentStream;

  // Patch getGenerativeModel on the AI prototype too — capture params.
  const AiProto = Object.getPrototypeOf(probeAI);
  const origAiGet = AiProto.getGenerativeModel;
  AiProto.getGenerativeModel = function (params: any) {
    inspect.modelParams = params;
    return origAiGet.call(this, params);
  };

  ModelProto.generateContentStream = async function (params: any, options?: any) {
    inspect.streamParams = params;
    inspect.streamOptions = options;
    const stream = (async function* () {
      for (const c of scriptedChunks) yield c;
    })();
    return { stream };
  };

  return {
    inspect,
    restore: () => {
      ModelProto.generateContentStream = origStream;
      AiProto.getGenerativeModel = origAiGet;
    },
  };
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

// ─── tests ──────────────────────────────────────────────────────────────────

async function testTextStreaming(): Promise<void> {
  console.log('\n[google-api-client] text streaming → text_delta in order');
  const { restore, inspect } = patchSdk([
    {
      candidates: [
        {
          content: { parts: [{ text: '你好' }] },
        },
      ],
    },
    {
      candidates: [
        {
          content: { parts: [{ text: '，世界' }] },
          finishReason: 'STOP',
        },
      ],
      usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 7 },
    },
  ]);
  try {
    const client = new GoogleApiClient({
      apiKey: 'g-key',
      model: 'gemini-2.5-flash',
    });
    const events = await collect(
      client.stream({
        system_prompt: 'sys prompt',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'hi' }] }],
        tools: [],
        signal: new AbortController().signal,
      }),
    );

    const texts = events
      .filter((e) => e.kind === 'text_delta')
      .map((e) => (e as { text: string }).text);
    check('text deltas in order', ['你好', '，世界'], texts);

    const usage = events.find((e) => e.kind === 'usage') as
      | { usage: { input_tokens?: number; output_tokens?: number } }
      | undefined;
    check('usage.input_tokens (from promptTokenCount)', 5, usage?.usage.input_tokens);
    check(
      'usage.output_tokens (from candidatesTokenCount)',
      7,
      usage?.usage.output_tokens,
    );

    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check('STOP → end_turn', 'end_turn', stop?.stop_reason);

    // ModelParams checks
    check('systemInstruction forwarded', 'sys prompt', inspect.modelParams?.systemInstruction);
    check(
      'safetySettings NOT injected by default',
      undefined,
      inspect.modelParams?.safetySettings,
    );
    check('model id forwarded', 'gemini-2.5-flash', inspect.modelParams?.model);
  } finally {
    restore();
  }
}

async function testFunctionCall(): Promise<void> {
  console.log('\n[google-api-client] functionCall part → tool_use with synthesized id');
  const { restore, inspect } = patchSdk([
    {
      candidates: [
        {
          content: {
            parts: [
              { text: 'calling echo' },
              { functionCall: { name: 'echo', args: { msg: 'hi' } } },
            ],
          },
          finishReason: 'STOP',
        },
      ],
    },
  ]);
  try {
    const client = new GoogleApiClient({ apiKey: 'g-key' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'go' }] }],
        tools: [ECHO_TOOL],
        signal: new AbortController().signal,
      }),
    );

    const tu = events.find((e) => e.kind === 'tool_use') as
      | { id: string; name: string; input: unknown }
      | undefined;
    checkTruthy('tool_use emitted', tu !== undefined);
    check('tool_use.name', 'echo', tu?.name);
    check('tool_use.input', { msg: 'hi' }, tu?.input);
    checkTruthy(
      'tool_use.id synthesized (g_…)',
      typeof tu?.id === 'string' && tu.id.startsWith('g_'),
    );

    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check(
      'STOP + functionCall → tool_use (overrides end_turn)',
      'tool_use',
      stop?.stop_reason,
    );

    // tools wired through
    const decls = inspect.modelParams?.tools?.[0]?.functionDeclarations;
    checkTruthy('functionDeclarations array forwarded', Array.isArray(decls));
    check('functionDeclarations[0].name', 'echo', decls?.[0]?.name);
  } finally {
    restore();
  }
}

async function testFunctionResponseRoundTrip(): Promise<void> {
  console.log('\n[google-api-client] toGeminiContents → functionResponse part round-trip');
  const contents = toGeminiContents([
    { role: 'user', blocks: [{ kind: 'text', text: 'use echo' }] },
    {
      role: 'assistant',
      blocks: [
        { kind: 'text', text: 'sure' },
        { kind: 'tool_use', id: 'g_1_0', name: 'echo', input: { msg: 'hi' } },
      ],
    },
    {
      role: 'tool',
      blocks: [
        {
          kind: 'tool_result',
          tool_use_id: 'g_1_0',
          tool_name: 'echo',
          output: '{"ok":true}',
          is_error: false,
        },
      ],
    },
  ]);

  check('contents length (user + model + user/functionResponse)', 3, contents.length);
  check('contents[1].role (assistant → model)', 'model', contents[1]?.role);
  const modelParts = contents[1]?.parts as Array<any>;
  check('model parts count (text + functionCall)', 2, modelParts.length);
  check('model functionCall name', 'echo', modelParts[1]?.functionCall?.name);
  check('model functionCall args', { msg: 'hi' }, modelParts[1]?.functionCall?.args);

  // tool_result fold
  check('contents[2].role (tool → user)', 'user', contents[2]?.role);
  const fnRespPart = (contents[2]?.parts as Array<any>)?.[0]?.functionResponse;
  check('functionResponse.name (from tool_name)', 'echo', fnRespPart?.name);
  check('functionResponse.response parsed', { ok: true }, fnRespPart?.response);
}

async function testFunctionResponseFold(): Promise<void> {
  console.log('\n[google-api-client] consecutive tool messages fold into one user envelope');
  const contents = toGeminiContents([
    {
      role: 'tool',
      blocks: [
        {
          kind: 'tool_result',
          tool_use_id: 'g_1_0',
          tool_name: 'echo',
          output: 'a',
          is_error: false,
        },
      ],
    },
    {
      role: 'tool',
      blocks: [
        {
          kind: 'tool_result',
          tool_use_id: 'g_1_1',
          tool_name: 'add',
          output: '3',
          is_error: false,
        },
      ],
    },
  ]);
  check('two tool msgs folded into single user content', 1, contents.length);
  const parts = contents[0]?.parts as Array<any>;
  check('folded parts count', 2, parts.length);
  check('parts[0].functionResponse.name', 'echo', parts[0]?.functionResponse?.name);
  check('parts[1].functionResponse.name', 'add', parts[1]?.functionResponse?.name);
}

async function testFinishReasonMaxTokens(): Promise<void> {
  console.log('\n[google-api-client] finishReason MAX_TOKENS → max_tokens');
  const { restore } = patchSdk([
    {
      candidates: [
        {
          content: { parts: [{ text: 'truncated' }] },
          finishReason: 'MAX_TOKENS',
        },
      ],
    },
  ]);
  try {
    const client = new GoogleApiClient({ apiKey: 'g-key' });
    const events = await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
    const stop = events.find((e) => e.kind === 'message_stop') as
      | { stop_reason: string }
      | undefined;
    check('MAX_TOKENS → max_tokens', 'max_tokens', stop?.stop_reason);
  } finally {
    restore();
  }
}

async function testNoApiKey(): Promise<void> {
  console.log('\n[google-api-client] missing api_key throws');
  delete process.env.GOOGLE_API_KEY;
  const client = new GoogleApiClient({});
  let threw = false;
  try {
    await collect(
      client.stream({
        system_prompt: '',
        messages: [],
        tools: [],
        signal: new AbortController().signal,
      }),
    );
  } catch (err) {
    threw = true;
    checkTruthy('error mentions API key', /API key/.test((err as Error).message));
  }
  check('throws for missing key', true, threw);
}

async function main(): Promise<void> {
  await testTextStreaming();
  await testFunctionCall();
  await testFunctionResponseRoundTrip();
  await testFunctionResponseFold();
  await testFinishReasonMaxTokens();
  await testNoApiKey();

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

void main();
