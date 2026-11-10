/**
 * claude-stream-json.ts — Anthropic Claude CLI line-delimited JSON parser (Story 15.19 v2)
 *
 * 2026-05-11 bug fix — `claude --output-format stream-json --verbose` (the
 * args we now spawn with) emits a NESTED envelope:
 *
 *   {"type":"system","subtype":"init",...}
 *   {"type":"stream_event","event":{"type":"message_start", ...}}
 *   {"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}}
 *   {"type":"stream_event","event":{"type":"content_block_stop","index":0}}
 *   {"type":"assistant","message":{"id":"...","content":[...]}}
 *   {"type":"result","usage":{...},"total_cost_usd":...,"stop_reason":"end_turn"}
 *
 * Earlier code only matched the FLAT `evt.type==='content_block_delta'` shape
 * (the non-verbose mode), so every line was silently skipped, text_delta
 * never accumulated, and the front-end saw a hung SSE stream → "已达最大
 * 重试次数". This parser now handles BOTH the flat (legacy) and nested
 * (verbose) envelopes, plus the `result` terminator.
 *
 * 2026-05-27 additive-normalization (CLI lane) — reference:
 * open-design apps/daemon/src/claude-stream.ts + json-event-stream.ts.
 * Previously, ANY line we didn't explicitly recognise (a thinking delta, a
 * tool_use block, a tool_result, a usage frame, or an entirely unknown
 * shape / malformed JSON) was silently `continue`d — i.e. SUBTRACTIVE: it
 * just vanished, and a stray non-text line could even leak into the text
 * stream. We now go ADDITIVE:
 *   - text_delta            → fed to parseAndExtract (yields text / <sf:*>).
 *   - thinking_delta / thinking block → `thinking-chunk`.
 *   - tool_use block        → `tool-use` (deduped by id via a Set, so a block
 *                             that streamed live AND re-arrives in the final
 *                             `assistant` wrapper is only emitted once).
 *   - tool_result block     → `tool-result`.
 *   - result.usage          → `usage`.
 *   - malformed JSON / a line no branch claims → `raw` (NOT text), with a
 *     `source` tag so run-sessions renders it in a collapsed raw block instead
 *     of dropping it or treating it as the answer.
 * No new SSE event names / contracts are introduced — all of the above are
 * names Xueban already consumes (see routes/run-sessions.ts).
 */

import type { Readable } from 'node:stream';
import { parseAndExtract, type SseEvent } from '../../parser';
import type { CliStreamArtifactCb } from './plain-line';

interface DeltaShape {
  type?: string;       // 'text_delta' | 'thinking_delta' | 'input_json_delta'
  text?: string;
  thinking?: string;
  partial_json?: string;
}

interface ContentBlock {
  type?: string;       // 'text' | 'thinking' | 'tool_use' | 'tool_result'
  text?: string;
  thinking?: string;
  // tool_use
  id?: string;
  name?: string;
  input?: unknown;
  // tool_result
  tool_use_id?: string;
  content?: unknown;
  is_error?: boolean;
}

interface ClaudeFlatEvent {
  type?: string;        // legacy flat: 'content_block_delta' | 'message_start' | ...
  index?: number;
  delta?: DeltaShape;
  content_block?: ContentBlock;
  // 'result' carries usage / stop_reason at the top level
  stop_reason?: string;
  usage?: Record<string, unknown>;
  total_cost_usd?: number;
  duration_ms?: number;
  // 'assistant' / 'user' carry message
  message?: { id?: string; content?: ContentBlock[] };
}

interface ClaudeNestedEvent extends ClaudeFlatEvent {
  // verbose mode: `{type:'stream_event', event:{...}}` wraps the real event.
  event?: ClaudeFlatEvent;
  subtype?: string;     // 'system' subtype: 'init' | 'status'
}

type ClaudeJsonEvent = ClaudeNestedEvent;

function isRecord(v: unknown): v is Record<string, unknown> {
  return v != null && typeof v === 'object' && !Array.isArray(v);
}

/** Flatten a string tool_result content (string | array of {type:'text',text} | object). */
function stringifyToolResult(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((c) => (isRecord(c) && c.type === 'text' ? String(c.text) : JSON.stringify(c)))
      .join('\n');
  }
  if (content == null) return '';
  return JSON.stringify(content);
}

/** Is this a stream-terminator event? (`message_stop` / `result`) */
function isTerminator(evt: ClaudeJsonEvent): boolean {
  if (evt.type === 'message_stop' || evt.type === 'result') return true;
  if (evt.type === 'stream_event' && evt.event?.type === 'message_stop') return true;
  return false;
}

export async function* parseClaudeStreamJson(
  stdout: Readable,
  sessionId: string,
  artifactCb: CliStreamArtifactCb,
): AsyncGenerator<SseEvent> {
  // Two buffers:
  //  - `lineBuf`: half-received JSON lines.
  //  - `textBuf`: accumulated text deltas, fed to <sf:*> parser.
  let lineBuf = '';
  let textBuf = '';
  let sawComplete = false;

  // tool_use blocks already emitted (keyed by id). A block can stream live via
  // stream_event content_block_start AND re-appear in the final `assistant`
  // wrapper — dedup so we only emit one `tool-use` per id. Anonymous (id-less)
  // blocks always emit (can't dedup safely).
  const emittedToolUse = new Set<string>();

  // 2026-05-11 review HIGH-1 (15.19): 防恶意/破损 CLI 长时间不发 \n 导致 lineBuf
  // 无界增长 → OOM。1MB 上限对正常 stream 远超富裕，触上限即放弃当前积累
  // 行（resync 到下个 \n）。textBuf 同理 — 但 textBuf 由 <sf:*> 解析器持续 drain，
  // 自然有界，给个 4MB 兜底防极端 case。
  const MAX_LINE_BUF = 1 * 1024 * 1024;
  const MAX_TEXT_BUF = 4 * 1024 * 1024;

  // Drain accumulated text through the <sf:*> machine, yielding any events.
  function* drainText(): Generator<SseEvent> {
    if (!textBuf.trim()) return;
    const { buffer: remaining, events } = parseAndExtract(textBuf, sessionId, artifactCb);
    textBuf = remaining;
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  // Classify ONE parsed JSON line into normalized SseEvents. Yields nothing
  // for purely structural frames (message_start, content_block_start for text,
  // etc.) we intentionally swallow; yields `raw` only when the line carries
  // observable content that no typed branch claimed. Text deltas are appended
  // to textBuf and flushed via drainText() (so `<sf:*>` extraction still runs).
  function* classify(evt: ClaudeJsonEvent): Generator<SseEvent> {
    // Unwrap the verbose `{type:'stream_event', event:{...}}` envelope. We
    // process the INNER event but remember we did, so unknown wrappers still
    // fall through to `raw` below.
    const inner: ClaudeFlatEvent = evt.type === 'stream_event' && evt.event ? evt.event : evt;
    const t = inner.type;

    // ── streaming deltas (verbose mode) ──────────────────────────────────
    if (t === 'content_block_delta' && inner.delta) {
      const d = inner.delta;
      if (d.type === 'text_delta' && typeof d.text === 'string') {
        textBuf += d.text;
        yield* drainText();
        return;
      }
      if (typeof d.text === 'string') {
        // legacy non-verbose: delta carries bare `text`.
        textBuf += d.text;
        yield* drainText();
        return;
      }
      if (d.type === 'thinking_delta' && typeof d.thinking === 'string' && d.thinking.length > 0) {
        yield { event: 'thinking-chunk', data: { step: null, text: d.thinking } };
        return;
      }
      if (d.type === 'input_json_delta') {
        // Partial tool-call JSON — accumulated by the CLI and re-delivered
        // whole in the final `assistant` wrapper, so swallow the partial here.
        return;
      }
      // content_block_delta with an unrecognised delta shape — surface it.
      yield { event: 'raw', data: { text: JSON.stringify(inner), source: 'claude:content_block_delta' } };
      return;
    }

    // ── structural frames we intentionally swallow (no observable content) ─
    if (
      t === 'message_start' ||
      t === 'message_delta' ||
      t === 'message_stop' ||
      t === 'content_block_start' ||
      t === 'content_block_stop' ||
      t === 'ping' ||
      (evt.type === 'system')          // init / status banner
    ) {
      return;
    }

    // ── final `assistant` wrapper — drain content blocks the stream may not
    //    have surfaced individually (thinking / tool_use), and dedup text. ──
    if (t === 'assistant' && Array.isArray(inner.message?.content)) {
      for (const block of inner.message!.content!) {
        if (!isRecord(block)) continue;
        const b = block as ContentBlock;
        if (b.type === 'text' && typeof b.text === 'string' && b.text.length > 0) {
          textBuf += b.text;
          yield* drainText();
        } else if (b.type === 'thinking' && typeof b.thinking === 'string' && b.thinking.length > 0) {
          yield { event: 'thinking-chunk', data: { step: null, text: b.thinking } };
        } else if (b.type === 'tool_use') {
          const key = typeof b.id === 'string' ? b.id : '';
          if (!key || !emittedToolUse.has(key)) {
            if (key) emittedToolUse.add(key);
            yield {
              event: 'tool-use',
              data: { id: b.id ?? null, name: b.name ?? 'unknown', input: b.input ?? null },
            };
          }
        }
      }
      return;
    }

    // ── `user` wrapper — carries tool_result blocks from prior turns. ─────
    if (t === 'user' && Array.isArray(inner.message?.content)) {
      for (const block of inner.message!.content!) {
        if (!isRecord(block)) continue;
        const b = block as ContentBlock;
        if (b.type === 'tool_result') {
          yield {
            event: 'tool-result',
            data: { for: b.tool_use_id ?? null, output: stringifyToolResult(b.content) },
          };
        }
      }
      return;
    }

    // ── result terminator — usage / cost. ────────────────────────────────
    if (t === 'result') {
      if (inner.usage && isRecord(inner.usage)) {
        yield { event: 'usage', data: { ...inner.usage } };
      }
      return;
    }

    // ── ADDITIVE fallback — a line no branch claimed. Surface it as raw so
    //    nothing vanishes silently and it is NOT mistaken for the answer. ──
    yield { event: 'raw', data: { text: JSON.stringify(evt), source: `claude:${t ?? 'unknown'}` } };
  }

  for await (const chunk of stdout) {
    lineBuf += chunk.toString('utf8');
    if (lineBuf.length > MAX_LINE_BUF) {
      console.warn(
        `[claude-stream-json] lineBuf exceeded ${MAX_LINE_BUF}B without newline — resyncing`,
      );
      // Drop everything before the LAST char so we resume at the next chunk.
      lineBuf = lineBuf.slice(-1);
    }
    const lines = lineBuf.split('\n');
    lineBuf = lines.pop() ?? '';
    if (textBuf.length > MAX_TEXT_BUF) {
      console.warn(
        `[claude-stream-json] textBuf exceeded ${MAX_TEXT_BUF}B — truncating`,
      );
      textBuf = textBuf.slice(-MAX_TEXT_BUF / 2);
    }

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      let evt: ClaudeJsonEvent;
      try {
        evt = JSON.parse(line) as ClaudeJsonEvent;
      } catch {
        // 2026-05-27 — malformed line is no longer dropped: emit it as raw so
        // a broken / non-JSON-emitting CLI still shows the user *something*.
        yield { event: 'raw', data: { text: line, source: 'claude:non-json' } };
        continue;
      }
      yield* classify(evt);
      if (isTerminator(evt)) {
        // Drain whatever's left in the text buffer at a terminator boundary.
        yield* drainText();
      }
    }
  }

  // Stream-end drain — handle a trailing partial line.
  if (lineBuf.trim()) {
    try {
      const evt = JSON.parse(lineBuf) as ClaudeJsonEvent;
      yield* classify(evt);
    } catch {
      yield { event: 'raw', data: { text: lineBuf.trim(), source: 'claude:non-json' } };
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
