/**
 * ApiClientCallable.thinking.test.ts — gap-close (2026-05-31).
 *
 * `assistantEventToChunk` is the AssistantEvent → TurnChunk mapper. It used to
 * lack a `thinking_delta` case, so extended-thinking on the no-tools API path
 * (`callable.turn()`) was silently dropped — only the with-tools
 * ConversationRuntime path surfaced it. This locks the fix: thinking_delta now
 * maps to a typed `thinking-delta` chunk (Law 2: text/thinking/tool are
 * independent channels on EVERY path).
 */
import { describe, it, expect } from 'vitest';
import { assistantEventToChunk } from './ApiClientCallable';

describe('assistantEventToChunk — thinking channel', () => {
  it('maps thinking_delta → typed thinking-delta chunk', () => {
    const c = assistantEventToChunk({ kind: 'thinking_delta', text: '让我想想' });
    expect(c).toEqual({ type: 'thinking-delta', value: '让我想想' });
  });

  it('thinking is NOT routed through text-delta (no <sf:thinking> round-trip)', () => {
    const c = assistantEventToChunk({ kind: 'thinking_delta', text: 'reason' });
    expect(c?.type).toBe('thinking-delta');
    expect(c?.type).not.toBe('text-delta');
  });

  it('drops a malformed thinking_delta (non-string text)', () => {
    expect(assistantEventToChunk({ kind: 'thinking_delta', text: 42 })).toBeNull();
  });

  // Regression guard for the other channels (unchanged behavior).
  it('still maps text_delta / tool_use / usage / message_stop', () => {
    expect(assistantEventToChunk({ kind: 'text_delta', text: 'hi' })).toEqual({
      type: 'text-delta',
      value: 'hi',
    });
    expect(assistantEventToChunk({ kind: 'tool_use', id: 't1', name: 'Bash', input: { c: 'ls' } })).toEqual({
      type: 'tool-use',
      tool: { tool_name: 'Bash', tool_input: { c: 'ls' }, call_id: 't1' },
    });
    expect(assistantEventToChunk({ kind: 'message_stop', stop_reason: 'end_turn' })).toEqual({
      type: 'done',
    });
    expect(assistantEventToChunk({ kind: 'usage', usage: { input_tokens: 5 } })?.type).toBe('usage');
  });

  it('returns null for unknown kinds', () => {
    expect(assistantEventToChunk({ kind: 'mystery' })).toBeNull();
    expect(assistantEventToChunk(null)).toBeNull();
  });
});
