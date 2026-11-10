/**
 * run-sessions.strip-frames.test.ts
 *
 * Unit tests for `stripLeakedSseFrames` — the sse-frame-leak stop-bleed
 * (2026-05-31). When a prior turn's leaked SSE wire frames got stored as
 * assistant content, re-injecting them as CONVERSATION HISTORY makes the LLM
 * parrot the frame shape back, turning the whole next turn into `raw` blocks.
 * This helper strips frame-shaped lines before they re-enter the prompt.
 *
 * The match must be TIGHT: only an `event:` line immediately followed by a
 * `data:` line whose payload opens with `{`/`[`. Normal prose that merely
 * mentions "event:" must survive untouched.
 */
import { describe, it, expect } from 'vitest';
import { stripLeakedSseFrames } from './run-sessions';

describe('stripLeakedSseFrames', () => {
  it('strips a leaked complete frame', () => {
    const input =
      'event: complete\ndata: {"session_id":"a667cabe","redirect":"/editor"}\n';
    expect(stripLeakedSseFrames(input).trim()).toBe('');
  });

  it('strips multiple stacked frames', () => {
    const input =
      'event: assemble\ndata: {"step":"plan"}\n' +
      'event: node\ndata: {"node_id":"reader"}\n';
    expect(stripLeakedSseFrames(input).trim()).toBe('');
  });

  it('keeps real prose that merely mentions the word event', () => {
    const prose =
      'The team will handle each event: parsing, review, and a final summary.';
    expect(stripLeakedSseFrames(prose)).toBe(prose);
  });

  it('keeps an `event:` line NOT followed by a `data:{` line', () => {
    const input = 'event: standup\nWe meet at 9am to sync.';
    expect(stripLeakedSseFrames(input)).toBe(input);
  });

  it('strips only the frame, preserving surrounding answer text', () => {
    const input =
      'Here is the plan.\n' +
      'event: complete\ndata: {"ok":true}\n' +
      'Thanks!';
    const out = stripLeakedSseFrames(input);
    expect(out).toContain('Here is the plan.');
    expect(out).toContain('Thanks!');
    expect(out).not.toContain('event: complete');
    expect(out).not.toContain('"ok":true');
  });

  it('fast-path returns identical string when no `event:` present', () => {
    const s = 'just a normal assistant message with no frames';
    expect(stripLeakedSseFrames(s)).toBe(s);
  });

  // P2 write-side isolation (root-cure plan §4b): the assistant-turn persistence
  // at run-sessions.ts:1428 sanitizes `collectedStreamText` through this helper
  // BEFORE appendMessage, so a frame can never enter the conversation log and be
  // re-injected as history next turn. This locks that scenario.
  it('P2 persistence: frame-only collected text → empty summary (nothing persisted)', () => {
    const collected =
      'event: assemble\ndata: {"step":"plan"}\n' +
      'event: complete\ndata: {"session_id":"a667cabe","run_id":"run-a667cabe"}\n';
    expect(stripLeakedSseFrames(collected).trim()).toBe('');
  });

  it('P2 persistence: real answer mixed with a stray frame → only clean answer persisted', () => {
    const collected =
      'The reader/critic team is assembled and ready.\n' +
      'event: complete\ndata: {"session_id":"x"}\n';
    const clean = stripLeakedSseFrames(collected).trim();
    expect(clean).toBe('The reader/critic team is assembled and ready.');
    expect(clean).not.toContain('event: complete');
  });
});
