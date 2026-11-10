/**
 * codex-cli-api-client.ts — ApiClient (S5) implementation backed by the
 * **OpenAI Codex CLI** (`codex --stream ...`).
 *
 * S14.2 (skill-team-conversion-design-v1.md §5 / Story brief 2026-05-21) —
 * companion to ClaudeCodeCliApiClient. With this in place a user who picked
 * executor `cli:codex` in BYOK gets the same v2 multi-turn ConversationRuntime
 * treatment as direct-API providers, by shelling out to the official OpenAI
 * codex binary.
 *
 * ⚠️ EXPERIMENTAL — proceed with caution
 * ──────────────────────────────────────
 * The Codex CLI's `--stream` JSONL output shape is not fully documented in
 * the public OpenAI repo (https://github.com/openai/codex) and has changed
 * across alpha builds. We model the events on the public OpenAI Responses API
 * since the CLI is a thin wrapper:
 *
 *   {"type":"response.output_text.delta","delta":"..."}
 *   {"type":"response.output_text.delta","delta":{"text":"..."}}     // older
 *   {"type":"response.output_item.added","item":{"type":"function_call","id":"...","name":"..."}}
 *   {"type":"response.function_call_arguments.delta","item_id":"...","delta":"..."}
 *   {"type":"response.output_item.done","item":{...}}
 *   {"type":"response.completed","response":{"usage":{...}}}
 *   {"type":"response.failed","response":{...}}                       // error path
 *
 * The text-delta path mirrors `parsers/cli-streams/codex-stream-json.ts`
 * (which is verified against the codex --stream tests in this repo).
 *
 * The tool-call path is BEST-EFFORT and untested against a real binary — the
 * OpenAI Codex CLI binary in current alpha builds may emit these events with
 * slightly different field names. We degrade gracefully: when no recognized
 * tool envelope is seen we just stream text + finish at `response.completed`.
 * The runtime then takes the no-tool_use exit branch and ends the turn, which
 * matches the legacy single-call behavior for codex skill executions.
 *
 * If/when we have a verified binary on a developer machine we'll dogfood the
 * tool path and tighten the parser. Until then, callers who NEED tool_use
 * over codex should prefer cli:claude or a direct-API provider.
 *
 * Architecture mirror: identical subprocess + stdin prompt + stdout NDJSON
 * line splitter scaffolding as ClaudeCodeCliApiClient. The two could share a
 * base class but at ~2 clients we keep them flat — easier to read in isolation
 * and the divergent NDJSON shapes don't have meaningful overlap.
 *
 * ALWAYS / NEVER rules
 *   - ALWAYS use Node built-in `child_process.spawn` — no execa / cross-spawn.
 *   - ALWAYS forward `args.signal` → `child.kill('SIGTERM')`.
 *   - ALWAYS buffer stdout chunks until `\n` before JSON.parse.
 *   - ALWAYS console.warn ONCE per stream when JSON.parse fails on a line.
 *   - NEVER hardcode the binary path — accept `opts.binPath` (default 'codex').
 *   - NEVER block on stderr — captured for non-zero-exit error message only.
 *   - NEVER throw on unrecognized event types — silently ignore for forward-
 *     compat with future CLI builds.
 */

import {
  spawn as nodeSpawn,
  type ChildProcessWithoutNullStreams,
  type SpawnOptionsWithoutStdio,
} from 'node:child_process';
import type { ApiClient, AssistantEvent } from '../../lib/conversation-runtime';
import type { ConversationMessage, TokenUsage } from '../../lib/conversation-types';
import type { ToolSpec } from '../../lib/tool-spec';

/**
 * SpawnFn — narrow contract over child_process.spawn so the client can be
 * dependency-injected with a fake subprocess factory in tests. Same shape as
 * ClaudeCodeCliApiClient's SpawnFn; redeclared locally to avoid cross-module
 * coupling between the two CLI clients.
 */
export type SpawnFn = (
  command: string,
  args: readonly string[],
  options: SpawnOptionsWithoutStdio,
) => ChildProcessWithoutNullStreams;

export interface CodexCliApiClientOptions {
  /** Path or command for the codex binary. Default: 'codex' (resolved via PATH). */
  binPath?: string;
  /** Optional model id passed via `--model`. */
  model?: string;
  /** Per-turn output cap (kept for parity — Codex may not honor it). */
  max_tokens?: number;
  /** Working directory for the child process. */
  cwd?: string;
  /**
   * BYOK fallback key. Set as `OPENAI_API_KEY` env on the child when caller
   * env doesn't already have one. Default: inherit parent env.
   */
  apiKey?: string;
  /**
   * Override extra args. Defaults to `['--stream']` per cli-registry.ts entry.
   * Override only for testing.
   */
  extraArgs?: string[];
  /**
   * Inject a spawn function (tests only). Production code leaves undefined.
   */
  spawnFn?: SpawnFn;
}

// ─── Codex event shape (best-effort, per public Responses API) ────────────

interface CodexItem {
  type?: string;                // 'function_call' | 'message' | ...
  id?: string;
  name?: string;
  arguments?: unknown;
}

interface CodexEvent {
  type?: string;                // 'response.output_text.delta' | 'response.output_item.added' | 'response.function_call_arguments.delta' | 'response.completed' | 'response.failed' | ...
  delta?: unknown;
  text?: unknown;
  item?: CodexItem;
  item_id?: string;
  response?: {
    usage?: unknown;
    status?: string;            // 'completed' | 'failed' | ...
    incomplete_details?: { reason?: string };
  };
  error?: { message?: string };
}

/**
 * Coax a string text-delta out of multiple known shapes:
 *   { delta: 'abc' }
 *   { delta: { text: 'abc' } }
 *   { delta: { content: 'abc' } }
 *   { text: 'abc' }   (rare)
 *
 * Returns null when the event carries no text.
 */
function extractTextDelta(evt: CodexEvent): string | null {
  if (typeof evt.delta === 'string') return evt.delta;
  if (evt.delta && typeof evt.delta === 'object') {
    const obj = evt.delta as { text?: unknown; content?: unknown };
    if (typeof obj.text === 'string') return obj.text;
    if (typeof obj.content === 'string') return obj.content;
  }
  if (typeof evt.text === 'string') return evt.text;
  return null;
}

/**
 * Extract OpenAI-style usage from the `response.completed` envelope.
 *
 *   response.usage.input_tokens   → input_tokens
 *   response.usage.output_tokens  → output_tokens
 *   response.usage.input_tokens_details.cached_tokens → cache_read_input_tokens
 *
 * Returns undefined when usage is absent so we don't emit empty events.
 */
function extractUsage(raw: unknown): TokenUsage | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const u = raw as Record<string, unknown>;
  const out: TokenUsage = {};
  if (typeof u.input_tokens === 'number') out.input_tokens = u.input_tokens;
  if (typeof u.output_tokens === 'number') out.output_tokens = u.output_tokens;
  const details = u.input_tokens_details;
  if (details && typeof details === 'object') {
    const d = details as Record<string, unknown>;
    if (typeof d.cached_tokens === 'number') out.cache_read_input_tokens = d.cached_tokens;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Normalize response status → our stop_reason vocab. Aligned with
 * openai-compat-api-client.normalizeFinishReason to keep the runtime branching
 * provider-agnostic.
 *
 *   'completed' / undefined  → 'end_turn'
 *   'failed'                 → 'error'
 *   anything else            → passes through
 *
 * Tool-use exit is decided by whether any tool_use events were emitted, not by
 * the response status — the Responses API doesn't have a separate tool-use
 * status (tool_calls show up as output items in a completed response).
 */
function normalizeResponseStatus(status: string | undefined, hadToolUse: boolean): string {
  if (hadToolUse) return 'tool_use';
  if (!status || status === 'completed') return 'end_turn';
  if (status === 'failed') return 'error';
  return status;
}

// ─── Prompt serialization ─────────────────────────────────────────────────

/**
 * Same role-label serialization as ClaudeCodeCliApiClient. Codex CLI is also
 * one-shot per invocation (no chat-history flag), so we encode the full
 * rolling history as labeled sections in the stdin prompt. tool_use /
 * tool_result blocks become tagged segments so the model can read prior turns.
 */
function serializePrompt(messages: ConversationMessage[], tools: ToolSpec[]): string {
  const out: string[] = [];
  if (tools.length > 0) {
    out.push('## Available tools');
    for (const t of tools) {
      out.push(`- ${t.name}: ${t.description}`);
    }
    out.push('');
  }
  for (const m of messages) {
    out.push(`## ${m.role.toUpperCase()}`);
    for (const b of m.blocks) {
      if (b.kind === 'text') {
        out.push(b.text);
      } else if (b.kind === 'tool_use') {
        const inputStr = typeof b.input === 'string' ? b.input : JSON.stringify(b.input ?? {});
        out.push(`<tool_use name="${b.name}" id="${b.id}">\n${inputStr}\n</tool_use>`);
      } else if (b.kind === 'tool_result') {
        out.push(
          `<tool_result tool_use_id="${b.tool_use_id}" name="${b.tool_name}" is_error="${b.is_error}">\n${b.output}\n</tool_result>`,
        );
      }
    }
    out.push('');
  }
  return out.join('\n');
}

const DEFAULT_EXTRA_ARGS = ['--stream'];

// ─── Client ───────────────────────────────────────────────────────────────

export class CodexCliApiClient implements ApiClient {
  constructor(private readonly opts: CodexCliApiClientOptions = {}) {}

  /**
   * Stream one LLM turn by spawning the Codex CLI. See file header for the
   * EXPERIMENTAL caveat on tool_call event shapes.
   *
   * Translation contract (Codex NDJSON event → AssistantEvent):
   *   response.output_text.delta (string/text/content)  → 'text_delta'
   *   response.output_item.added type=function_call     → buffer id+name
   *   response.function_call_arguments.delta             → accumulate args by item_id
   *   response.output_item.done type=function_call       → emit 'tool_use'
   *   response.completed.usage                           → emit 'usage'
   *   response.completed                                 → emit 'message_stop' end_turn
   *   response.failed                                    → throw with error message
   *
   * Errors / signal:
   *   - Spawn ENOENT → throw with install hint (`npm i -g @openai/codex`)
   *   - Non-zero exit → throw with stderr tail
   *   - args.signal.aborted → SIGTERM
   *   - JSON.parse failure → warn once, skip
   *   - `response.failed` → throw to surface to runtime's error policy
   */
  async *stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: ToolSpec[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent> {
    const binPath = this.opts.binPath ?? 'codex';
    const extraArgs = this.opts.extraArgs ?? DEFAULT_EXTRA_ARGS;
    const spawnArgs = [...extraArgs];
    if (this.opts.model && this.opts.model.length > 0) {
      spawnArgs.push('--model', this.opts.model);
    }

    const env = { ...process.env };
    if (this.opts.apiKey && this.opts.apiKey.length > 0 && !env.OPENAI_API_KEY) {
      env.OPENAI_API_KEY = this.opts.apiKey;
    }

    const spawnImpl: SpawnFn = this.opts.spawnFn ?? nodeSpawn;
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawnImpl(binPath, spawnArgs, {
        cwd: this.opts.cwd,
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch (err) {
      throw new Error(
        `codex CLI spawn failed (${binPath}): ${err instanceof Error ? err.message : String(err)}. Install: npm i -g @openai/codex, then set OPENAI_API_KEY env.`,
      );
    }

    // P1-2: Use Node's StringDecoder via setEncoding so multi-byte UTF-8
    // sequences (中文 / emoji) that span chunk boundaries are decoded safely
    // instead of producing `�` (`�`) replacements from a naïve
    // Buffer#toString('utf8') on a half-character slice.
    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');

    let stderrBuf = '';
    child.stderr.on('data', (c: string) => {
      stderrBuf += c;
      if (stderrBuf.length > 256 * 1024) stderrBuf = stderrBuf.slice(-128 * 1024);
    });

    let spawnError: Error | null = null;
    child.on('error', (err: Error) => {
      spawnError = err;
    });

    const onAbort = (): void => {
      if (!child.killed) {
        try {
          child.kill('SIGTERM');
        } catch {
          /* already dead */
        }
      }
    };
    if (args.signal.aborted) {
      onAbort();
    } else {
      args.signal.addEventListener('abort', onAbort, { once: true });
    }

    // Stitch system_prompt into the prompt body — Codex CLI doesn't have a
    // dedicated --system flag like Claude Code, so we prefix it as a SYSTEM
    // section. The model reads it identically.
    const prefixSection = args.system_prompt.length > 0
      ? `## SYSTEM\n${args.system_prompt}\n\n`
      : '';
    const prompt = prefixSection + serializePrompt(args.messages, args.tools);
    try {
      child.stdin.write(prompt);
      child.stdin.end();
    } catch {
      /* child dead — exit handler below will surface */
    }

    let exitCode: number | null = null;
    let exitSignal: NodeJS.Signals | null = null;
    const exitPromise = new Promise<void>((resolve) => {
      child.on('exit', (code, sig) => {
        exitCode = code;
        exitSignal = sig;
        resolve();
      });
    });

    // Per-stream tool-call accumulators, keyed by item_id (the Responses API's
    // canonical identifier — propagated through every function-call delta).
    interface PendingToolCall {
      id: string;
      name: string;
      argsBuf: string;
    }
    const pendingById = new Map<string, PendingToolCall>();
    let hadToolUse = false;
    let messageStopEmitted = false;
    let warnedBadLine = false;
    let responseStatus: string | undefined;
    let responseUsage: TokenUsage | undefined;
    let responseFailedMsg: string | null = null;

    const MAX_LINE_BUF = 1 * 1024 * 1024;
    let lineBuf = '';

    const processLine = function* (
      this: void,
      rawLine: string,
    ): Generator<AssistantEvent> {
      const line = rawLine.trim();
      if (!line) return;
      let evt: CodexEvent;
      try {
        evt = JSON.parse(line) as CodexEvent;
      } catch {
        if (!warnedBadLine) {
          // eslint-disable-next-line no-console
          console.warn(
            `[CodexCliApiClient] failed to JSON.parse stdout line (snippet='${line.slice(0, 80)}') — skipping further parse-failure warnings this stream`,
          );
          warnedBadLine = true;
        }
        return;
      }

      const t = evt.type ?? '';

      // ── text deltas ────────────────────────────────────────────────────
      if (t === 'response.output_text.delta' || t === 'message.delta') {
        const txt = extractTextDelta(evt);
        if (txt !== null && txt.length > 0) {
          yield { kind: 'text_delta', text: txt };
        }
        return;
      }

      // ── tool-call lifecycle (best-effort, EXPERIMENTAL) ───────────────
      if (t === 'response.output_item.added' && evt.item?.type === 'function_call') {
        const id = evt.item.id ?? `codex_${pendingById.size}`;
        pendingById.set(id, {
          id,
          name: evt.item.name ?? '',
          argsBuf: '',
        });
        return;
      }
      if (t === 'response.function_call_arguments.delta') {
        const id = evt.item_id ?? '';
        const p = id ? pendingById.get(id) : undefined;
        if (p && typeof evt.delta === 'string') p.argsBuf += evt.delta;
        return;
      }
      if (t === 'response.output_item.done' && evt.item?.type === 'function_call') {
        const id = evt.item.id ?? '';
        const p = id ? pendingById.get(id) : undefined;
        if (p) {
          // Name might only arrive on the `done` event in some CLI builds.
          if (!p.name && typeof evt.item.name === 'string') p.name = evt.item.name;
          // Some builds emit the final arguments string on `done` rather than
          // streaming via .arguments.delta. Take whichever is non-empty.
          if (p.argsBuf.length === 0 && typeof evt.item.arguments === 'string') {
            p.argsBuf = evt.item.arguments;
          }
          let input: unknown = {};
          if (p.argsBuf.length > 0) {
            try {
              input = JSON.parse(p.argsBuf);
            } catch {
              input = { __parse_error: true, raw: p.argsBuf };
            }
          }
          yield { kind: 'tool_use', id: p.id, name: p.name, input };
          pendingById.delete(id);
          hadToolUse = true;
        }
        return;
      }

      // ── terminators ────────────────────────────────────────────────────
      if (t === 'response.completed') {
        responseStatus = evt.response?.status ?? 'completed';
        const u = extractUsage(evt.response?.usage);
        if (u) responseUsage = u;
        // Defer the emit — we want a single message_stop with the right
        // stop_reason after we know whether any tool_use fired.
        return;
      }
      if (t === 'response.failed') {
        responseStatus = 'failed';
        const errMsg = evt.response?.incomplete_details?.reason
          ?? evt.error?.message
          ?? 'codex response.failed';
        responseFailedMsg = errMsg;
        return;
      }
      // Other event types ('response.created', 'response.in_progress',
      // 'response.output_item.added' with non-function_call items, etc.) are
      // intentionally ignored — they carry no state we need.
    };

    // P1-1: wrap the entire body in try/finally so that the abort listener is
    // removed on every exit path — non-zero exit, response.failed, spawnError,
    // parse errors, normal completion. The previous code only released the
    // listener on the happy path, which leaked listeners across turns when
    // ConversationRuntime reuses the same AbortSignal (stale listeners can
    // also fire and disrupt a later turn).
    try {
      try {
        // stdout is now string-mode (setEncoding('utf8') above), so chunks
        // are already decoded — no chunk.toString('utf8') needed.
        for await (const chunk of child.stdout as AsyncIterable<string>) {
          if (args.signal.aborted) {
            onAbort();
            return;
          }
          lineBuf += chunk;
          if (lineBuf.length > MAX_LINE_BUF) {
            // eslint-disable-next-line no-console
            console.warn(
              `[CodexCliApiClient] lineBuf > ${MAX_LINE_BUF}B without newline — resyncing`,
            );
            const lastNl = lineBuf.lastIndexOf('\n');
            lineBuf = lastNl >= 0 ? lineBuf.slice(lastNl + 1) : lineBuf.slice(-1);
          }
          const lines = lineBuf.split('\n');
          lineBuf = lines.pop() ?? '';
          for (const ln of lines) {
            for (const ev of processLine(ln)) yield ev;
          }
        }
        if (lineBuf.trim().length > 0) {
          for (const ev of processLine(lineBuf)) yield ev;
          lineBuf = '';
        }
      } catch (err) {
        if (args.signal.aborted) return;
        throw err instanceof Error ? err : new Error(String(err));
      }

      await exitPromise;

      if (spawnError) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const code = (spawnError as any).code;
        if (code === 'ENOENT') {
          throw new Error(
            `codex CLI not found (${binPath}). Install: npm i -g @openai/codex, then set OPENAI_API_KEY env.`,
          );
        }
        throw spawnError;
      }

      if (exitCode !== null && exitCode !== 0) {
        const tail = stderrBuf.slice(-2048);
        throw new Error(
          `codex CLI exited with code ${exitCode}${exitSignal ? ` (signal=${exitSignal})` : ''}: ${tail || '(no stderr)'}`,
        );
      }

      if (responseFailedMsg) {
        throw new Error(`codex response.failed: ${responseFailedMsg}`);
      }

      // Emit deferred usage + message_stop in order. usage first so the runtime
      // accumulates before the stop event in the same way openai-compat does.
      if (responseUsage) {
        yield { kind: 'usage', usage: responseUsage };
      }
      if (!messageStopEmitted) {
        yield {
          kind: 'message_stop',
          stop_reason: normalizeResponseStatus(responseStatus, hadToolUse),
        };
        messageStopEmitted = true;
      }
    } finally {
      args.signal.removeEventListener('abort', onAbort);
    }
  }
}
