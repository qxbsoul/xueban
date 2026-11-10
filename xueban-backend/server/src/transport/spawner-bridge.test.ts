/**
 * spawner-bridge.test.ts — ② sse-frame-leak CLI-path root cure (2026-05-31).
 *
 * The spawner (CLI/ACP/MCP) already parses its stream-json into STRUCTURED
 * Xueban SSE events. The bridge used to re-flatten EVERY event into a
 * `text-delta` carrying the literal `event:/data:` wire line — which the
 * downstream text parser then mis-flagged as sse-frame-leak `raw` (so
 * <sf:node>/complete never rendered → TEAM 0). These tests lock the new
 * behavior: core channels map to typed chunks; other structured events pass
 * through verbatim as `sse` chunks; NOTHING is re-encoded as wire-text.
 */
import { describe, it, expect } from 'vitest';
import { sseEventToChunk, bridgeSpawnerStream } from './spawner-bridge';
import type { SseEvent } from '../parser';

describe('sseEventToChunk — channel mapping', () => {
  it('text → text-delta with the raw text (NOT a wire-encoded line)', () => {
    const c = sseEventToChunk({ event: 'text', data: { text: 'hello' } });
    expect(c).toEqual({ type: 'text-delta', value: 'hello', node_id: undefined });
    // The cardinal regression: never the literal "event: text\ndata:" wire line.
    expect(c.type === 'text-delta' && c.value.includes('event:')).toBe(false);
  });

  it('thinking-chunk → thinking-delta', () => {
    const c = sseEventToChunk({ event: 'thinking-chunk', data: { text: '想一下' } });
    expect(c).toEqual({ type: 'thinking-delta', value: '想一下', node_id: undefined });
  });

  it('tool-use → typed tool-use chunk', () => {
    const c = sseEventToChunk({
      event: 'tool-use',
      data: { name: 'Bash', input: { command: 'ls' }, id: 'toolu_1' },
    });
    expect(c).toEqual({
      type: 'tool-use',
      tool: { tool_name: 'Bash', tool_input: { command: 'ls' }, call_id: 'toolu_1' },
      node_id: undefined,
    });
  });

  it('tool-result → typed tool-result chunk (for→tool_use_id)', () => {
    const c = sseEventToChunk({
      event: 'tool-result',
      data: { for: 'toolu_1', output: 'file1\nfile2', is_error: false },
    });
    expect(c).toEqual({
      type: 'tool-result',
      result: { tool_use_id: 'toolu_1', output: 'file1\nfile2', is_error: false },
      node_id: undefined,
    });
  });

  it('error → typed error chunk', () => {
    const c = sseEventToChunk({ event: 'error', data: { code: 'RATE_LIMITED', message: 'slow down' } });
    expect(c.type).toBe('error');
    if (c.type === 'error') {
      expect(c.error.kind).toBe('rate-limit');
      expect(c.error.message).toBe('slow down');
    }
  });

  it.each(['node', 'assemble', 'blueprint', 'classify', 'edge', 'yaml-line', 'usage', 'raw', 'complete'])(
    'business event %s → verbatim sse passthrough (NOT text-delta)',
    (event) => {
      const data = { foo: 'bar', n: 1 };
      const c = sseEventToChunk({ event, data });
      expect(c).toEqual({ type: 'sse', event, data, node_id: undefined });
      // Must NOT be flattened to a wire-text text-delta.
      expect(c.type).not.toBe('text-delta');
    },
  );

  it('propagates node_id from data onto the chunk', () => {
    const c = sseEventToChunk({ event: 'text', data: { text: 'x', node_id: 'n1' } });
    expect(c.node_id).toBe('n1');
    const s = sseEventToChunk({ event: 'node', data: { node_id: 'n2', title: 'A' } });
    expect(s.node_id).toBe('n2');
  });
});

describe('bridgeSpawnerStream — drains + maps + appends done', () => {
  async function run(events: SseEvent[]) {
    async function* gen() {
      for (const e of events) yield e;
    }
    const out = [];
    for await (const c of bridgeSpawnerStream(gen(), new AbortController().signal)) out.push(c);
    return out;
  }

  it('maps a realistic CLI assembly stream and ends with done', async () => {
    const chunks = await run([
      { event: 'assemble', data: { step: '分析', status: 'running' } },
      { event: 'node', data: { node_id: 'reader', title: 'Reader' } },
      { event: 'text', data: { text: '团队已就绪' } },
      { event: 'complete', data: { session_id: 's1' } },
    ]);
    const types = chunks.map((c) => c.type);
    // assemble/node/complete pass through as sse; text → text-delta; +done.
    expect(types).toEqual(['sse', 'sse', 'text-delta', 'sse', 'done']);
    // The node + complete reach the wire as themselves, never as raw wire-text.
    const node = chunks.find((c) => c.type === 'sse' && c.event === 'node');
    expect(node).toBeTruthy();
    const complete = chunks.find((c) => c.type === 'sse' && c.event === 'complete');
    expect(complete).toBeTruthy();
  });

  it('does NOT append done after an error chunk (error is terminal)', async () => {
    const chunks = await run([
      { event: 'text', data: { text: 'partial' } },
      { event: 'error', data: { code: 'CLI_EXIT_NONZERO', message: 'boom' } },
    ]);
    expect(chunks.map((c) => c.type)).toEqual(['text-delta', 'error']);
    expect(chunks.some((c) => c.type === 'done')).toBe(false);
  });
});
