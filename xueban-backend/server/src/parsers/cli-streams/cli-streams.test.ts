/**
 * cli-streams.test.ts — Parser fixtures (Story 15.19 v2)
 *
 * Run from server/:  npx tsx src/parsers/cli-streams/cli-streams.test.ts
 *
 * Standalone tsx test (mirrors parser.test.ts pattern). Each parser is fed a
 * synthetic Readable stream of bytes and we assert the AsyncGenerator yields
 * the expected normalized SseEvents.
 */

import { Readable } from 'node:stream';
import type { SseEvent } from '../../parser';
import { parseClaudeStreamJson } from './claude-stream-json';
import { parseCodexStreamJson } from './codex-stream-json';
import { parseGhCopilot } from './gh-copilot';
import { parsePlainLine } from './plain-line';
import { getStreamParser } from './index';

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

const captured: Array<{ filename: string; content: string; type: string }> = [];
const artifactCb = (filename: string, content: string, type: string) => {
  captured.push({ filename, content, type });
};

function streamFromChunks(chunks: string[]): Readable {
  return Readable.from(chunks.map((c) => Buffer.from(c, 'utf8')));
}

async function collect(gen: AsyncGenerator<SseEvent>): Promise<SseEvent[]> {
  const out: SseEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

function findEvent(events: SseEvent[], name: string): SseEvent | undefined {
  return events.find((e) => e.event === name);
}

function countEvent(events: SseEvent[], name: string): number {
  return events.filter((e) => e.event === name).length;
}

async function main(): Promise<void> {

// ─── claude-stream-json ───────────────────────────────────────────────────────

console.log('\n── claude-stream-json ──');
{
  const lines = [
    JSON.stringify({ type: 'message_start' }) + '\n',
    JSON.stringify({
      type: 'content_block_delta',
      delta: { type: 'text_delta', text: '<sf:classify output_type="answer" mode="single" confidence="0.9" complexity="1"/>' },
    }) + '\n',
    JSON.stringify({
      type: 'content_block_delta',
      delta: { type: 'text_delta', text: '<sf:step name="生成" status="running"/>' },
    }) + '\n',
    JSON.stringify({ type: 'message_stop' }) + '\n',
  ];
  const events = await collect(parseClaudeStreamJson(streamFromChunks(lines), 'sess-claude-1', artifactCb));
  check('claude: classify event extracted', !!findEvent(events, 'classify'));
  check('claude: assemble event extracted', !!findEvent(events, 'assemble'));
  check('claude: synthetic complete added', countEvent(events, 'complete') === 1);
}

{
  // Split JSON across chunk boundary to test buffer handling
  const json = JSON.stringify({
    type: 'content_block_delta',
    delta: { type: 'text_delta', text: '<sf:node id="a" type="agent" title="测" sub="x" chips="x,y" avatar_char="A"/>' },
  });
  const half = Math.floor(json.length / 2);
  const events = await collect(
    parseClaudeStreamJson(
      streamFromChunks([json.slice(0, half), json.slice(half) + '\n', JSON.stringify({ type: 'message_stop' }) + '\n']),
      'sess-claude-2',
      artifactCb,
    ),
  );
  check('claude: handles split JSON across chunks', !!findEvent(events, 'node'));
}

// ─── codex-stream-json ────────────────────────────────────────────────────────

console.log('\n── codex-stream-json ──');
{
  const lines = [
    JSON.stringify({ type: 'response.output_text.delta', delta: '<sf:classify output_type="report" mode="single" confidence="0.8" complexity="2"/>' }) + '\n',
    JSON.stringify({ type: 'response.output_text.delta', delta: '<sf:step name="测试" status="done" elapsed_ms="100"/>' }) + '\n',
    JSON.stringify({ type: 'response.completed' }) + '\n',
  ];
  const events = await collect(parseCodexStreamJson(streamFromChunks(lines), 'sess-codex-1', artifactCb));
  check('codex: extracts string-delta', !!findEvent(events, 'classify'));
  check('codex: handles response.completed', !!findEvent(events, 'assemble'));
  check('codex: synthetic complete', countEvent(events, 'complete') === 1);
}

{
  // Test nested delta.text shape
  const lines = [
    JSON.stringify({ type: 'response.output_text.delta', delta: { text: '<sf:edge from="a" to="b"/>' } }) + '\n',
  ];
  const events = await collect(parseCodexStreamJson(streamFromChunks(lines), 'sess-codex-2', artifactCb));
  check('codex: extracts nested delta.text', !!findEvent(events, 'edge'));
}

// ─── gh-copilot ───────────────────────────────────────────────────────────────

console.log('\n── gh-copilot ──');
{
  const events = await collect(
    parseGhCopilot(
      streamFromChunks(['Sure! Try this: `git status`\n', 'And then `git commit`\n']),
      'sess-gh-1',
      artifactCb,
    ),
  );
  check('gh-copilot: emits running assemble step', events.some((e) => e.event === 'assemble' && (e.data as { status?: string }).status === 'running'));
  check('gh-copilot: emits done assemble step', events.some((e) => e.event === 'assemble' && (e.data as { status?: string }).status === 'done'));
  check('gh-copilot: synthetic complete', countEvent(events, 'complete') === 1);
}

// ─── plain-line ───────────────────────────────────────────────────────────────

console.log('\n── plain-line ──');
{
  const text = '<sf:classify output_type="workflow" mode="team" confidence="0.7" complexity="3"/>';
  const events = await collect(parsePlainLine(streamFromChunks([text]), 'sess-plain-1', artifactCb));
  check('plain-line: extracts sf:classify from raw text', !!findEvent(events, 'classify'));
  check('plain-line: synthetic complete', countEvent(events, 'complete') === 1);
}

{
  // Artifact extraction test — must trigger artifactCb
  captured.length = 0;
  const text = '<artifact type="markdown" filename="out.md">\n# Hello\n</artifact>';
  const events = await collect(parsePlainLine(streamFromChunks([text]), 'sess-plain-2', artifactCb));
  check('plain-line: artifact emits blueprint event', !!findEvent(events, 'blueprint'));
  check('plain-line: artifact callback fired', captured.some((c) => c.filename === 'out.md'));
}

// ─── dispatcher ───────────────────────────────────────────────────────────────

console.log('\n── dispatcher ──');
{
  const claudeFn = getStreamParser('claude-stream-json');
  const plainFn = getStreamParser('plain-line');
  const cursorFn = getStreamParser('cursor-acp');
  check('dispatcher: claude-stream-json maps to parseClaudeStreamJson', claudeFn === parseClaudeStreamJson);
  check('dispatcher: cursor-acp falls back to plain-line', cursorFn === plainFn);
  // unknown format is impossible with TS but test default branch via cast
  const fallbackFn = getStreamParser('something-weird' as never);
  check('dispatcher: unknown format defaults to plain-line', fallbackFn === plainFn);
}

// ─── summary ──────────────────────────────────────────────────────────────────

console.log(`\n────────────────────────────────────────────`);
console.log(`Total: ${passCount + failCount} | PASS: ${passCount} | FAIL: ${failCount}`);
if (failCount > 0) {
  process.exit(1);
}

}

main().catch((err) => {
  console.error('test crashed:', err);
  process.exit(1);
});
