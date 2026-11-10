/**
 * additive-normalization.test.ts — CLI stream additive归一 regression (2026-05-27)
 *
 * Run from server/:  npx tsx src/parsers/cli-streams/additive-normalization.test.ts
 *
 * Standalone tsx test (mirrors cli-streams.test.ts / api-clients __tests__ style).
 * Verifies the OpenDesign-style ADDITIVE归一 contract: every stdout line is
 * accounted for — text → text, typed structures → their typed SSE event, and
 * anything unrecognised / malformed → `raw` (NEVER silently dropped, NEVER
 * leaked into `text`).
 */

import { Readable } from 'node:stream';
import type { SseEvent } from '../../parser';
import { parseClaudeStreamJson } from './claude-stream-json';
import { parseCodexStreamJson } from './codex-stream-json';

let passCount = 0;
let failCount = 0;

function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    passCount++;
    console.log(`  PASS  ${label}`);
  } else {
    failCount++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

const artifactCb = () => {};

function streamFromChunks(chunks: string[]): Readable {
  return Readable.from(chunks.map((c) => Buffer.from(c, 'utf8')));
}

async function collect(gen: AsyncGenerator<SseEvent>): Promise<SseEvent[]> {
  const out: SseEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

function find(events: SseEvent[], name: string): SseEvent | undefined {
  return events.find((e) => e.event === name);
}
function findAll(events: SseEvent[], name: string): SseEvent[] {
  return events.filter((e) => e.event === name);
}
function countEvent(events: SseEvent[], name: string): number {
  return events.filter((e) => e.event === name).length;
}

async function main(): Promise<void> {

// ─── claude: plain text line → text (not raw) ──────────────────────────────
console.log('\n── claude: normal text line → text ──');
{
  const lines = [
    JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: 'Hello world' } }) + '\n',
    JSON.stringify({ type: 'result', usage: { input_tokens: 5, output_tokens: 2 } }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-text', artifactCb));
  const text = find(events, 'text');
  check('claude: text_delta surfaces as `text`', !!text);
  check('claude: text payload correct', !!text && (text.data as { text?: string }).text === 'Hello world', text?.data);
  check('claude: text line did NOT leak to raw', countEvent(events, 'raw') === 0);
  check('claude: result.usage → usage event', !!find(events, 'usage'));
}

// ─── claude: tool_use block in assistant wrapper → tool-use (deduped) ───────
console.log('\n── claude: tool_use block → tool-use (deduped by id) ──');
{
  const assistant = {
    type: 'assistant',
    message: {
      id: 'msg_1',
      content: [
        { type: 'tool_use', id: 'tool_abc', name: 'Bash', input: { command: 'ls' } },
      ],
    },
  };
  const lines = [
    JSON.stringify(assistant) + '\n',
    // Same wrapper re-arrives (final flush double-send) — must NOT duplicate.
    JSON.stringify(assistant) + '\n',
    JSON.stringify({ type: 'result' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-tool', artifactCb));
  const tu = findAll(events, 'tool-use');
  check('claude: tool_use → exactly 1 tool-use (deduped by id)', tu.length === 1, tu);
  check('claude: tool-use id/name correct',
    tu.length === 1 && (tu[0].data as { id?: string; name?: string }).id === 'tool_abc'
      && (tu[0].data as { name?: string }).name === 'Bash', tu[0]?.data);
  check('claude: tool_use did NOT leak to text', countEvent(events, 'text') === 0);
}

// ─── claude: tool_result in user wrapper → tool-result ──────────────────────
console.log('\n── claude: tool_result → tool-result ──');
{
  const lines = [
    JSON.stringify({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'tool_abc', content: 'file1\nfile2', is_error: false }] },
    }) + '\n',
    JSON.stringify({ type: 'result' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-tr', artifactCb));
  const tr = find(events, 'tool-result');
  check('claude: tool_result → tool-result', !!tr);
  check('claude: tool-result for/output correct',
    !!tr && (tr.data as { for?: string }).for === 'tool_abc'
      && (tr.data as { output?: string }).output === 'file1\nfile2', tr?.data);
}

// ─── claude: thinking delta → thinking-chunk ────────────────────────────────
console.log('\n── claude: thinking_delta → thinking-chunk ──');
{
  const lines = [
    JSON.stringify({ type: 'content_block_delta', delta: { type: 'thinking_delta', thinking: 'let me reason...' } }) + '\n',
    JSON.stringify({ type: 'result' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-think', artifactCb));
  const th = find(events, 'thinking-chunk');
  check('claude: thinking_delta → thinking-chunk', !!th);
  check('claude: thinking text correct', !!th && (th.data as { text?: string }).text === 'let me reason...', th?.data);
  check('claude: thinking did NOT leak to text', countEvent(events, 'text') === 0);
}

// ─── claude: UNRECOGNIZED line → raw (the core fix; not dropped/text) ───────
console.log('\n── claude: unknown line → raw (not dropped, not text) ──');
{
  const lines = [
    JSON.stringify({ type: 'some_future_event', payload: { foo: 'bar' } }) + '\n',
    JSON.stringify({ type: 'result' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-unk', artifactCb));
  const raw = find(events, 'raw');
  check('claude: unrecognised line → raw', !!raw);
  check('claude: raw source tagged with type', !!raw && /^claude:some_future_event$/.test(String((raw.data as { source?: string }).source)), raw?.data);
  check('claude: unrecognised line did NOT become text', countEvent(events, 'text') === 0);
}

// ─── claude: malformed JSON line → raw (not silently dropped) ───────────────
console.log('\n── claude: malformed JSON → raw ──');
{
  const lines = [
    'this is not json at all\n',
    JSON.stringify({ type: 'result' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-c-bad', artifactCb));
  const raw = find(events, 'raw');
  check('claude: malformed line → raw', !!raw);
  check('claude: malformed raw source = claude:non-json', !!raw && (raw.data as { source?: string }).source === 'claude:non-json', raw?.data);
  check('claude: malformed raw preserves text', !!raw && (raw.data as { text?: string }).text === 'this is not json at all', raw?.data);
}

// ─── codex: normal delta → text; unknown → raw; bad → raw ───────────────────
console.log('\n── codex: text vs raw split ──');
{
  const lines = [
    JSON.stringify({ type: 'response.output_text.delta', delta: 'hi there' }) + '\n',
    JSON.stringify({ type: 'response.weird_frame', stuff: 1 }) + '\n',
    'BROKEN}{' + '\n',
    JSON.stringify({ type: 'response.completed', usage: { input_tokens: 3 } }) + '\n',
  ];
  const events = await collect(parseCodexStreamJson(streamFromChunks(lines), 'sess-x-mix', artifactCb));
  const text = find(events, 'text');
  check('codex: delta → text', !!text && (text.data as { text?: string }).text === 'hi there', text?.data);
  const raws = findAll(events, 'raw');
  check('codex: unknown frame → raw', raws.some((r) => (r.data as { source?: string }).source === 'codex:response.weird_frame'), raws.map((r) => r.data));
  check('codex: malformed line → raw (non-json)', raws.some((r) => (r.data as { source?: string }).source === 'codex:non-json'), raws.map((r) => r.data));
  check('codex: usage on completed → usage', !!find(events, 'usage'));
  check('codex: synthetic complete present', countEvent(events, 'complete') === 1);
}

// ─── codex: structural noise frame swallowed (NOT raw) ──────────────────────
console.log('\n── codex: structural noise swallowed ──');
{
  const lines = [
    JSON.stringify({ type: 'response.created' }) + '\n',
    JSON.stringify({ type: 'response.output_text.delta', delta: 'x' }) + '\n',
    JSON.stringify({ type: 'response.completed' }) + '\n',
  ];
  const events = await collect(parseCodexStreamJson(streamFromChunks(lines), 'sess-x-noise', artifactCb));
  check('codex: response.created produced no raw', countEvent(events, 'raw') === 0, events.map((e) => e.event));
}

// ─── summary ────────────────────────────────────────────────────────────────
console.log(`\n────────────────────────────────────────────`);
console.log(`Total: ${passCount + failCount} | PASS: ${passCount} | FAIL: ${failCount}`);
if (failCount > 0) process.exit(1);

}

main().catch((err) => {
  console.error('test crashed:', err);
  process.exit(1);
});
