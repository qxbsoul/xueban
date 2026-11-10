/**
 * claude-code-cli-stream-json-input.test.ts — gap-close (2026-05-31).
 *
 * Last sse-frame-leak-plan gap on Law 1: prior tool calls/results fed to the
 * CLI must NOT become `<tool_use>`/`<tool_result>` XML text. `serializeStreamJsonInput`
 * emits them as NATIVE Anthropic content blocks in a JSONL message stream
 * (for `--input-format stream-json`). These tests lock that: zero XML, valid
 * JSONL, structured tool blocks.
 */
import { describe, it, expect } from 'vitest';
import { serializeStreamJsonInput } from './claude-code-cli-api-client';
import type { ConversationMessage } from '../../lib/conversation-types';
import type { ToolSpec } from '../../lib/tool-spec';

const TOOL: ToolSpec = { name: 'Bash', description: 'run a shell command', input_schema: { type: 'object' } } as ToolSpec;

function parseLines(out: string): Array<Record<string, unknown>> {
  return out
    .split('\n')
    .filter((l) => l.trim().length > 0)
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

describe('serializeStreamJsonInput — tools never become XML text', () => {
  const history: ConversationMessage[] = [
    { role: 'user', blocks: [{ kind: 'text', text: 'list the files' }] },
    {
      role: 'assistant',
      blocks: [
        { kind: 'text', text: 'Let me check.' },
        { kind: 'tool_use', id: 'toolu_1', name: 'Bash', input: { command: 'ls' } },
      ],
    },
    {
      role: 'tool',
      blocks: [{ kind: 'tool_result', tool_use_id: 'toolu_1', tool_name: 'Bash', output: 'a.txt\nb.txt', is_error: false }],
    },
  ];

  it('emits NO <tool_use>/<tool_result> XML anywhere', () => {
    const out = serializeStreamJsonInput(history, [TOOL]);
    expect(out).not.toContain('<tool_use');
    expect(out).not.toContain('<tool_result');
    expect(out).not.toContain('</tool_use>');
  });

  it('every line is valid JSON (proper JSONL)', () => {
    const out = serializeStreamJsonInput(history, [TOOL]);
    expect(() => parseLines(out)).not.toThrow();
  });

  it('tool_use becomes a native structured block', () => {
    const lines = parseLines(serializeStreamJsonInput(history, []));
    const asst = lines.find((l) => l.type === 'assistant');
    const content = (asst?.message as { content?: Array<Record<string, unknown>> })?.content ?? [];
    const tu = content.find((b) => b.type === 'tool_use');
    expect(tu).toEqual({ type: 'tool_use', id: 'toolu_1', name: 'Bash', input: { command: 'ls' } });
  });

  it('tool_result becomes a native block in a user-role message', () => {
    const lines = parseLines(serializeStreamJsonInput(history, []));
    // The tool-result message maps to role 'user' (Anthropic convention).
    const withResult = lines.find(
      (l) =>
        l.type === 'user' &&
        ((l.message as { content?: Array<Record<string, unknown>> })?.content ?? []).some(
          (b) => b.type === 'tool_result',
        ),
    );
    expect(withResult).toBeTruthy();
    const tr = ((withResult!.message as { content: Array<Record<string, unknown>> }).content).find(
      (b) => b.type === 'tool_result',
    );
    expect(tr).toEqual({
      type: 'tool_result',
      tool_use_id: 'toolu_1',
      content: 'a.txt\nb.txt',
      is_error: false,
    });
  });

  it('tool DEFINITIONS ride a leading user text block (definitions ≠ calls)', () => {
    const lines = parseLines(serializeStreamJsonInput(history, [TOOL]));
    const first = lines[0];
    const text = ((first.message as { content: Array<{ type: string; text?: string }> }).content)[0];
    expect(text.type).toBe('text');
    expect(text.text).toContain('## Available tools');
    expect(text.text).toContain('Bash');
  });

  it('parses a stringified tool_use input back into structured JSON', () => {
    const msgs: ConversationMessage[] = [
      { role: 'assistant', blocks: [{ kind: 'tool_use', id: 't', name: 'X', input: '{"k":1}' }] },
    ];
    const lines = parseLines(serializeStreamJsonInput(msgs, []));
    const tu = ((lines[0].message as { content: Array<Record<string, unknown>> }).content)[0];
    expect((tu as { input: unknown }).input).toEqual({ k: 1 });
  });

  it('empty history → empty output (no trailing junk)', () => {
    expect(serializeStreamJsonInput([], [])).toBe('');
  });
});
