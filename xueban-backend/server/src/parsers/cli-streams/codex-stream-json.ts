/**
 * codex-stream-json.ts — OpenAI codex CLI line-delimited JSON parser (Story 15.19 v2)
 *
 * `codex --stream` emits OpenAI-style events:
 *   {"type":"response.output_text.delta","delta":"..."}
 *   {"type":"response.completed"}
 *
 * Like the Claude variant, we extract deltas → text buffer → `parseAndExtract`.
 *
 * 2026-05-27 additive-normalization (CLI lane) — reference:
 * open-design apps/daemon/src/json-event-stream.ts (handleCodexEvent).
 * Previously any line that wasn't a recognised text delta or a terminator was
 * silently `continue`d (SUBTRACTIVE — it vanished). We now go ADDITIVE:
 *   - text deltas (string `delta`, `delta.text/.content`, or bare `text`) →
 *     fed to parseAndExtract (yields text / <sf:*>).
 *   - usage frames (`turn.completed`/`response.completed` carrying usage) →
 *     `usage`.
 *   - malformed JSON / any line no branch claims → `raw` (NOT text), tagged
 *     with `source` so run-sessions renders it in a collapsed raw block.
 * No new SSE event names / contracts introduced — all names already consumed
 * by routes/run-sessions.ts.
 */

import type { Readable } from 'node:stream';
import { parseAndExtract, type SseEvent } from '../../parser';
import type { CliStreamArtifactCb } from './plain-line';

interface CodexEvent {
  type?: string;
  delta?: unknown;
  text?: unknown;
  usage?: Record<string, unknown>;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v != null && typeof v === 'object' && !Array.isArray(v);
}

function extractDeltaText(evt: CodexEvent): string | null {
  // Most common shape: delta is a string.
  if (typeof evt.delta === 'string') return evt.delta;
  // Some intermediate codex versions nest `delta.text` or `delta.content`.
  if (evt.delta && typeof evt.delta === 'object') {
    const obj = evt.delta as { text?: unknown; content?: unknown };
    if (typeof obj.text === 'string') return obj.text;
    if (typeof obj.content === 'string') return obj.content;
  }
  if (typeof evt.text === 'string') return evt.text;
  return null;
}

/** Codex/OpenAI terminator event types. */
function isCodexTerminator(t: string | undefined): boolean {
  return t === 'response.completed' || t === 'turn.completed' || t === 'message_stop' || t === 'done';
}

/** Structural frames with no observable content — intentionally swallowed. */
function isCodexNoise(t: string | undefined): boolean {
  return (
    t === 'response.created' ||
    t === 'response.in_progress' ||
    t === 'response.output_item.added' ||
    t === 'response.output_item.done' ||
    t === 'response.content_part.added' ||
    t === 'response.content_part.done' ||
    t === 'response.output_text.done' ||
    t === 'thread.started' ||
    t === 'turn.started' ||
    t === 'item.started'
  );
}

export async function* parseCodexStreamJson(
  stdout: Readable,
  sessionId: string,
  artifactCb: CliStreamArtifactCb,
): AsyncGenerator<SseEvent> {
  let lineBuf = '';
  let textBuf = '';
  let sawComplete = false;

  // 2026-05-11 review HIGH-1 (15.19): 防 unbounded buffer OOM (与 claude-stream-json 同款).
  const MAX_LINE_BUF = 1 * 1024 * 1024;
  const MAX_TEXT_BUF = 4 * 1024 * 1024;

  function* drainText(): Generator<SseEvent> {
    if (!textBuf.trim()) return;
    const { buffer: remaining, events } = parseAndExtract(textBuf, sessionId, artifactCb);
    textBuf = remaining;
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  function* classify(evt: CodexEvent): Generator<SseEvent> {
    const deltaText = extractDeltaText(evt);
    if (deltaText !== null) {
      textBuf += deltaText;
      yield* drainText();
      return;
    }

    const t = evt.type;
    if (isCodexTerminator(t)) {
      yield* drainText();
      if (evt.usage && isRecord(evt.usage)) {
        yield { event: 'usage', data: { ...evt.usage } };
      }
      return;
    }

    if (isCodexNoise(t)) return;

    // ── ADDITIVE fallback — a line no branch claimed. Surface as raw. ──
    yield { event: 'raw', data: { text: JSON.stringify(evt), source: `codex:${t ?? 'unknown'}` } };
  }

  for await (const chunk of stdout) {
    lineBuf += chunk.toString('utf8');
    if (lineBuf.length > MAX_LINE_BUF) {
      console.warn(`[codex-stream-json] lineBuf exceeded ${MAX_LINE_BUF}B — resyncing`);
      lineBuf = lineBuf.slice(-1);
    }
    const lines = lineBuf.split('\n');
    lineBuf = lines.pop() ?? '';
    if (textBuf.length > MAX_TEXT_BUF) {
      console.warn(`[codex-stream-json] textBuf exceeded ${MAX_TEXT_BUF}B — truncating`);
      textBuf = textBuf.slice(-MAX_TEXT_BUF / 2);
    }

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      let evt: CodexEvent;
      try {
        evt = JSON.parse(line) as CodexEvent;
      } catch {
        // 2026-05-27 — malformed line no longer dropped: emit as raw.
        yield { event: 'raw', data: { text: line, source: 'codex:non-json' } };
        continue;
      }
      yield* classify(evt);
    }
  }

  // Stream-end drain — handle a trailing partial line.
  if (lineBuf.trim()) {
    try {
      const evt = JSON.parse(lineBuf) as CodexEvent;
      yield* classify(evt);
    } catch {
      yield { event: 'raw', data: { text: lineBuf.trim(), source: 'codex:non-json' } };
    }
  }
  yield* drainText();

  if (!sawComplete) {
    yield {
      event: 'complete',
      data: {
        session_id: sessionId,
        run_id: `run-${sessionId.slice(0, 8)}`,
        redirect: `/editor?session=${sessionId}`,
      },
    };
  }
}
