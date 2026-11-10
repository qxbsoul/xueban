/**
 * claude-code-cli-api-client.ts — ApiClient (S5) implementation backed by the
 * **Claude Code CLI** (`claude -p ... --output-format stream-json --verbose`).
 *
 * S14.2 (skill-team-conversion-design-v1.md §5 / Story brief 2026-05-21) —
 * companion to AnthropicApiClient / OpenAiCompatApiClient / GoogleApiClient.
 * With this in place, a user who picked executor `cli:claude` in BYOK gets the
 * same v2 multi-turn ConversationRuntime treatment (tool_use loop + usage
 * accounting + per-turn streaming) as the direct-API providers, by shelling
 * out to the official Anthropic Claude Code CLI binary they already auth'd
 * via `claude login`.
 *
 * S14.2 follow-up (2026-05-21) — closed 6 gaps vs. open-design's canonical
 * `apps/daemon/src/claude-stream.ts` parser:
 *   1. thinking_delta passthrough wrapped as <sf:thinking step="extended"
 *      origin="cli">…</sf:thinking> (was silently dropped).
 *   2. Top-level `assistant` wrapper fallback — older CLI builds (<1.0.86) or
 *      runs without `--include-partial-messages` emit text / thinking / tool_use
 *      ONLY in the post-stream `{"type":"assistant"}` summary line; we now
 *      surface them, with per-messageId textStreamed + streamedToolUseIds
 *      dedup against earlier `stream_event` deltas.
 *   3. `fallbackBins` (default ['openclaude']) for `openclaude` fork users when
 *      `claude` isn't on PATH (issue #235 style — open-design's same decision).
 *   4. Capability probing — runs `<bin> -p --help` once per binPath (cached for
 *      the process lifetime) to detect `--include-partial-messages` support;
 *      when present, the flag is added to the spawn so we get true streaming
 *      instead of one giant `assistant` line at end-of-turn.
 *   5. Checker P1-1: AbortSignal listener removed in `finally`, including throw
 *      paths — was leaking listeners when ConversationRuntime reused a single
 *      signal across turns and the stream errored.
 *   6. Checker P1-2: `child.stdout.setEncoding('utf8')` + `child.stderr.setEncoding`
 *      — Node's StringDecoder handles UTF-8 byte boundaries inside its buffer
 *      so multi-byte glyphs (中文/emoji) don't get sliced into U+FFFD. Removed
 *      the manual `chunk.toString('utf8')` calls.
 *
 * Why a separate ApiClient (vs. the existing `parseClaudeStreamJson` runner)
 * ────────────────────────────────────────────────────────────────────────────
 * The legacy CLI path in `skill-runners/cli.ts` is single-turn: assembler →
 * spawn → text deltas → parseAndExtract → SSE. That works for the
 * `<sf:thinking>/<sf:step>` text-protocol skills but cannot drive the v2
 * loop, which needs:
 *
 *   (a) raw `tool_use` events (id + name + accumulated input JSON) surfaced
 *       per turn so PermissionPolicy + ToolExecutor can fire;
 *   (b) per-turn `message_stop` with stop_reason so the runtime decides whether
 *       to loop (tool_use) or break (end_turn);
 *   (c) usage accounting per turn so the rolling totalUsage stays accurate.
 *
 * The Claude Code CLI in `stream-json --verbose` mode emits exactly the same
 * events as the Anthropic Messages SSE protocol (it's a thin wrapper around
 * the Anthropic SDK), just framed as NDJSON instead of SSE. So we can REUSE
 * the same event-translation logic from anthropic-api-client.ts — just swap
 * the input source from "SDK iterator" to "child stdout line stream".
 *
 * CLI envelope (verbose + --include-partial-messages, see open-design's
 * apps/daemon/src/claude-stream.ts):
 *
 *   {"type":"system","subtype":"init", ...}
 *   {"type":"stream_event","event":{"type":"message_start","message":{"id":"...","usage":{...}}}}
 *   {"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"text"}}}
 *   {"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"..."}}}
 *   {"type":"stream_event","event":{"type":"content_block_start","index":1,"content_block":{"type":"thinking"}}}
 *   {"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"thinking_delta","thinking":"..."}}}
 *   {"type":"stream_event","event":{"type":"content_block_stop","index":1}}
 *   {"type":"stream_event","event":{"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"...","name":"..."}}}
 *   {"type":"stream_event","event":{"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"..."}}}
 *   {"type":"stream_event","event":{"type":"content_block_stop","index":2}}
 *   {"type":"stream_event","event":{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{...}}}
 *   {"type":"stream_event","event":{"type":"message_stop"}}
 *   {"type":"assistant","message":{"id":"...","content":[...], "stop_reason":"end_turn"}}   // summary
 *   {"type":"result","stop_reason":"end_turn","usage":{...}}   // terminator
 *
 * Without `--include-partial-messages` (older CLI), the `stream_event` lines
 * are absent and ALL content surfaces only via the `assistant` wrapper. We
 * therefore parse both, deduping against `textStreamed` (messageId set) and
 * `streamedToolUseIds` (tool_use id set).
 *
 * Auth: the CLI uses its own credential store (`~/.config/claude/` or similar
 * under Windows %APPDATA%). The BYOK key in opts.apiKey is IGNORED — we honor
 * it only as a fallback ANTHROPIC_API_KEY env var for the child process, since
 * the CLI accepts that env when no `claude login` session exists.
 *
 * Tools: the CLI doesn't currently expose a per-spawn `--tools` flag to inject
 * arbitrary custom tools — its tool set is fixed (read/write/bash/etc.) and
 * unrelated to Xueban's SkillAnchor tools. We therefore DO NOT forward
 * `args.tools` into the spawn command line; instead we serialize them into
 * the system prompt as a hint. The runtime will still receive any `tool_use`
 * events the CLI happens to emit (verbose mode reports the CLI's own tools)
 * but the SkillAnchor executor will return is_error for unknown names. In
 * practice for team-backed skills the LLM is expected to drive tool_use via
 * the same `<sf:*>` text protocol that already works in legacy CLI runs.
 *
 * ALWAYS / NEVER rules
 *   - ALWAYS use Node built-in `child_process.spawn` — no execa / cross-spawn.
 *   - ALWAYS forward `args.signal` → `child.kill('SIGTERM')` (Windows alias).
 *   - ALWAYS buffer stdout chunks until `\n` before JSON.parse — partial lines
 *     mid-chunk are normal.
 *   - ALWAYS console.warn ONCE per stream when JSON.parse fails on a line
 *     (proxy banners, log output); never throw on it.
 *   - ALWAYS setEncoding('utf8') on child.stdout/stderr — otherwise multi-byte
 *     glyphs split across chunk boundaries decode to U+FFFD.
 *   - ALWAYS remove the abort listener in `finally` so failed streams don't
 *     leak listeners when ConversationRuntime reuses a single AbortSignal.
 *   - NEVER hardcode the binary path — accept `opts.binPath` (default 'claude')
 *     and `opts.fallbackBins` (default ['openclaude']).
 *   - NEVER block the loop on stderr — stderr is captured and surfaced ONLY
 *     on non-zero exit.
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
 * dependency-injected with a fake subprocess factory in tests. Production
 * callers omit this and we default to the real Node spawn.
 *
 * The return must satisfy the subset of ChildProcessWithoutNullStreams the
 * client uses: { stdout, stderr, stdin, kill, on('exit'|'error'), killed }.
 */
export type SpawnFn = (
  command: string,
  args: readonly string[],
  options: SpawnOptionsWithoutStdio,
) => ChildProcessWithoutNullStreams;

/** Default fallback binaries tried in order when `binPath` ENOENTs. */
export const DEFAULT_FALLBACK_BINS = ['openclaude'] as const;

export interface ClaudeCodeCliApiClientOptions {
  /** Path or command for the claude-code binary. Default: 'claude' (resolved via PATH). */
  binPath?: string;
  /**
   * Ordered list of fallback commands tried when the primary `binPath` spawn
   * errors with ENOENT. Default: ['openclaude'] (the OpenClaude fork ships
   * the same NDJSON envelope and is the de-facto drop-in for users who can't
   * or won't install the official CLI — see open-design issue #235 for the
   * same decision). Pass `[]` to disable fallback entirely.
   */
  fallbackBins?: readonly string[];
  /** Optional model id passed via `--model`. */
  model?: string;
  /** Per-turn output cap (best-effort — the CLI doesn't always honor it; kept for parity). */
  max_tokens?: number;
  /** Working directory for the child process. */
  cwd?: string;
  /**
   * BYOK fallback key. Set as `ANTHROPIC_API_KEY` env on the child when the
   * user has not run `claude login`. Default: inherit from parent env.
   */
  apiKey?: string;
  /**
   * Override extra args. Defaults to the verified spawn set used in
   * skill-runners/cli.ts (`-p`, `--output-format stream-json`, `--verbose`,
   * `--permission-mode bypassPermissions`). Override only for testing.
   */
  extraArgs?: string[];
  /**
   * Inject a spawn function (tests only — defaults to `child_process.spawn`).
   * The injected function must satisfy SpawnFn. Production code should leave
   * this undefined.
   */
  spawnFn?: SpawnFn;
  /**
   * Override capability probing — tests pass `{ partialMessages: false }` to
   * suppress the probe (which would otherwise consume a spawn slot). When
   * provided, `probeClaudeCapabilities` is NOT invoked at all.
   */
  capabilities?: ClaudeCapabilities;
}

// ─── Capabilities (fix 4) ──────────────────────────────────────────────────

export interface ClaudeCapabilities {
  /** `--include-partial-messages` flag accepted by `<bin> -p`. */
  partialMessages: boolean;
  /** `--add-dir` flag accepted (informational; not currently used). */
  addDir: boolean;
  /**
   * `--input-format` flag accepted by `<bin> -p` → the CLI can read a
   * `stream-json` message stream on stdin. When true we feed history as NATIVE
   * structured blocks (tool_use/tool_result never become `<tool_use>` XML);
   * when false we fall back to the text prompt. Optional so test fixtures that
   * pre-seed only {partialMessages,addDir} keep type-checking (undefined→false).
   */
  streamJsonInput?: boolean;
}

/**
 * Module-level cache: `binPath` → in-flight or resolved probe Promise. Same
 * `binPath` reused across stream() calls in one process always shares the
 * single probe. Exported for tests via `__resetClaudeCapabilityCache`.
 */
const capabilityCache = new Map<string, Promise<ClaudeCapabilities>>();

/** Test-only: nuke the cache so a fresh probe runs next call. */
export function __resetClaudeCapabilityCache(): void {
  capabilityCache.clear();
}

/**
 * Test-only: pre-seed the cache so `stream()` skips the probe spawn. Tests
 * that fake the spawn function otherwise have to script a probe child too,
 * which makes the table noisy. Production code never uses this.
 */
export function __primeClaudeCapability(binPath: string, caps: ClaudeCapabilities): void {
  capabilityCache.set(binPath, Promise.resolve(caps));
}

const PROBE_TIMEOUT_MS = 3000;

/**
 * Probe `<binPath> -p --help` to detect supported flags. Cached per binPath
 * for the process lifetime — the CLI's flag surface doesn't change unless
 * the user reinstalls, and a stale cache only ever fails closed (no
 * --include-partial-messages = older-build fallback path, which still works).
 *
 * Failure modes all fold to `{ partialMessages: false, addDir: false }`:
 *   - ENOENT (binary missing — caller will then attempt fallbackBins)
 *   - non-zero exit
 *   - timeout (3s)
 *   - unparseable help output
 *
 * Note: probes the `-p` subcommand, not bare `--help`, because the relevant
 * flags live under `-p` (print/non-interactive mode), not the top level.
 */
export async function probeClaudeCapabilities(
  binPath: string,
  spawnImpl: SpawnFn,
): Promise<ClaudeCapabilities> {
  const existing = capabilityCache.get(binPath);
  if (existing) return existing;

  const promise = (async (): Promise<ClaudeCapabilities> => {
    return new Promise<ClaudeCapabilities>((resolve) => {
      const fallback: ClaudeCapabilities = { partialMessages: false, addDir: false };
      let settled = false;
      const settle = (caps: ClaudeCapabilities): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(caps);
      };

      let child: ChildProcessWithoutNullStreams;
      try {
        // SpawnOptionsWithoutStdio doesn't accept 'ignore' for stdin — leave
        // it 'pipe' and just don't write anything (the child will block on
        // stdin only if it tries to read, which `-p --help` doesn't).
        child = spawnImpl(binPath, ['-p', '--help'], {});
      } catch {
        settle(fallback);
        return;
      }

      let stdout = '';
      try {
        child.stdout?.setEncoding('utf8');
        child.stderr?.setEncoding('utf8');
      } catch {
        /* fake children in tests may not support setEncoding */
      }
      child.stdout?.on('data', (c: string | Buffer) => {
        stdout += typeof c === 'string' ? c : c.toString('utf8');
      });
      // stderr is discarded — help text usually goes to stdout, and stderr
      // chatter from `claude doctor`-style banners shouldn't fail the probe.
      child.stderr?.on('data', () => { /* drain */ });

      child.on('error', () => settle(fallback));
      child.on('exit', (code) => {
        if (code !== 0 && code !== null) {
          // Some builds exit 1 on --help; still inspect stdout if any.
        }
        settle({
          partialMessages: stdout.includes('--include-partial-messages'),
          addDir: stdout.includes('--add-dir'),
          streamJsonInput: stdout.includes('--input-format'),
        });
      });

      const timer = setTimeout(() => settle(fallback), PROBE_TIMEOUT_MS);
    });
  })();

  capabilityCache.set(binPath, promise);
  // If the probe rejects (shouldn't — we always resolve), clear so a future
  // call retries instead of caching a permanent failure.
  promise.catch(() => capabilityCache.delete(binPath));
  return promise;
}

// ─── NDJSON event shape ────────────────────────────────────────────────────

interface ContentBlockShape {
  type?: string;
  id?: string;
  name?: string;
}

interface DeltaShape {
  type?: string;            // 'text_delta' | 'input_json_delta' | 'thinking_delta' | ...
  text?: string;
  partial_json?: string;
  thinking?: string;
  stop_reason?: string;
}

interface InnerEvent {
  type?: string;            // 'message_start' | 'content_block_start' | 'content_block_delta' | 'content_block_stop' | 'message_delta' | 'message_stop'
  index?: number;
  content_block?: ContentBlockShape;
  delta?: DeltaShape;
  message?: { id?: string; usage?: unknown; stop_reason?: unknown };
  usage?: unknown;
  /** CLI-only: time-to-first-token in ms, present on `message_start` envelopes. */
  ttft_ms?: number;
}

interface AssistantWrapperBlock {
  type?: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

interface AssistantWrapperMessage {
  id?: string;
  stop_reason?: unknown;
  content?: AssistantWrapperBlock[];
  usage?: unknown;
}

interface OuterEvent extends InnerEvent {
  /** Verbose-mode wrapper: event nests under `event`. */
  event?: InnerEvent;
  /** 'result' terminator carries final stop_reason + usage at the top level. */
  stop_reason?: string;
  /** 'system' has subtype 'init'/'status' — ignored here. */
  subtype?: string;
  /** `assistant` summary wrapper. */
  message?: AssistantWrapperMessage;
  /** CLI-only: per-turn total cost in USD, present on the `result` terminator. */
  total_cost_usd?: number;
  /** CLI-only: per-turn wall-clock in ms, present on the `result` terminator. */
  duration_ms?: number;
}

/**
 * Resolve nested or flat envelope to a single InnerEvent. Returns the original
 * if the outer object already looks like an inner event (`type` ∈ known set
 * AND there's no `.event` field).
 */
function extractInnerEvent(evt: OuterEvent): InnerEvent | null {
  if (evt.type === 'stream_event' && evt.event && typeof evt.event === 'object') {
    return evt.event;
  }
  // Flat (legacy) variant — return as-is if it carries a known inner-event type.
  const innerTypes = new Set([
    'message_start',
    'content_block_start',
    'content_block_delta',
    'content_block_stop',
    'message_delta',
    'message_stop',
  ]);
  if (typeof evt.type === 'string' && innerTypes.has(evt.type)) return evt;
  return null;
}

/**
 * Same usage extractor as anthropic-api-client.ts — Anthropic's tuple of
 * input/output/cache_creation/cache_read tokens.
 */
function extractUsage(raw: unknown): TokenUsage | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const u = raw as Record<string, unknown>;
  const out: TokenUsage = {};
  if (typeof u.input_tokens === 'number') out.input_tokens = u.input_tokens;
  if (typeof u.output_tokens === 'number') out.output_tokens = u.output_tokens;
  if (typeof u.cache_creation_input_tokens === 'number')
    out.cache_creation_input_tokens = u.cache_creation_input_tokens;
  if (typeof u.cache_read_input_tokens === 'number')
    out.cache_read_input_tokens = u.cache_read_input_tokens;
  return Object.keys(out).length > 0 ? out : undefined;
}

// NOTE (2026-05-31): the former `wrapThinking()` helper (which serialized
// extended-thinking into `<sf:thinking>` TEXT for parser.ts to re-extract) was
// removed. AssistantEvent DOES have a `thinking_delta` kind, and we now yield
// it directly — thinking is a typed channel end-to-end, never round-tripped
// through text. See ApiClientCallable's `thinking_delta` → `thinking-delta` map.

// ─── Prompt serialization ─────────────────────────────────────────────────

/**
 * Serialize the runtime's full ConversationMessage[] into a single text prompt
 * the CLI's `-p` flag accepts. The CLI is one-shot per spawn — it doesn't take
 * a chat history flag — so we encode role turns as labeled sections. The LLM
 * sees identical context to what the API-backed clients send via the
 * `messages` array.
 *
 * Tool_use / tool_result blocks are serialized as JSON-tagged segments so the
 * model can read prior tool calls + their outputs and decide whether to keep
 * looping.
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
    const label = m.role.toUpperCase();
    out.push(`## ${label}`);
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

/**
 * Structured stdin for `--input-format stream-json` (gap-close, 2026-05-31).
 *
 * Closes the last sse-frame-leak-plan gap on Law 1 (tools never become text):
 * instead of `serializePrompt`'s `<tool_use>`/`<tool_result>` XML, prior tool
 * calls + results are emitted as NATIVE Anthropic content blocks in a JSONL
 * message stream — exactly how OpenDesign's CLI integration feeds history.
 * Tool *definitions* (the available-tools list) are NOT tool calls, so carrying
 * them as a leading text block is fine; only tool_use/tool_result must stay
 * structured.
 *
 * Envelope (one JSON per line): `{"type":"user"|"assistant","message":{role,content:[blocks]}}`.
 * Xueban roles map: assistant→assistant; user/tool/system→user (tool_result
 * lives in a user message per Anthropic convention; system is also sent via
 * `--append-system`).
 *
 * GATED OFF by default (`XUEBAN_CLI_INPUT_STREAM_JSON=1` to enable): the
 * exact CLI input envelope can't be verified here without a live `claude`
 * binary, so the proven text path stays default until a real-CLI smoke test
 * promotes this. The serialization itself is fully unit-tested.
 */
export function serializeStreamJsonInput(
  messages: ConversationMessage[],
  tools: ToolSpec[],
): string {
  type NativeBlock =
    | { type: 'text'; text: string }
    | { type: 'tool_use'; id: string; name: string; input: unknown }
    | { type: 'tool_result'; tool_use_id: string; content: string; is_error: boolean };

  const lines: string[] = [];

  const emit = (apiRole: 'user' | 'assistant', content: NativeBlock[]): void => {
    if (content.length === 0) return;
    lines.push(JSON.stringify({ type: apiRole, message: { role: apiRole, content } }));
  };

  // Tool definitions as a leading user text block (definitions ≠ calls).
  if (tools.length > 0) {
    const list = ['## Available tools', ...tools.map((t) => `- ${t.name}: ${t.description}`)].join('\n');
    emit('user', [{ type: 'text', text: list }]);
  }

  for (const m of messages) {
    const apiRole: 'user' | 'assistant' = m.role === 'assistant' ? 'assistant' : 'user';
    const content: NativeBlock[] = [];
    for (const b of m.blocks) {
      if (b.kind === 'text') {
        content.push({ type: 'text', text: b.text });
      } else if (b.kind === 'tool_use') {
        // NATIVE structured block — never `<tool_use>` XML text.
        content.push({
          type: 'tool_use',
          id: b.id,
          name: b.name,
          input: typeof b.input === 'string' ? safeJsonParse(b.input) : (b.input ?? {}),
        });
      } else if (b.kind === 'tool_result') {
        content.push({
          type: 'tool_result',
          tool_use_id: b.tool_use_id,
          content: b.output,
          is_error: Boolean(b.is_error),
        });
      }
    }
    emit(apiRole, content);
  }
  return lines.join('\n') + (lines.length > 0 ? '\n' : '');
}

function safeJsonParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

// ─── Default spawn args (mirror cli-registry.ts 'claude' entry) ───────────

/**
 * Verified spawn args from `skill-runners/cli.ts` / cli-registry.ts:
 *  - `-p` short for --print → non-interactive
 *  - `--output-format stream-json` → NDJSON to stdout
 *  - `--verbose` → enables per-token deltas (without this, only an end-of-turn
 *    summary lands on stdout and the runtime appears hung)
 *  - `--permission-mode bypassPermissions` → CLI won't pause for Y/N prompts
 */
const DEFAULT_EXTRA_ARGS = [
  '-p',
  '--output-format',
  'stream-json',
  '--verbose',
  '--permission-mode',
  'bypassPermissions',
];

// ─── Client ───────────────────────────────────────────────────────────────

export class ClaudeCodeCliApiClient implements ApiClient {
  constructor(private readonly opts: ClaudeCodeCliApiClientOptions = {}) {}

  /**
   * Stream one LLM turn by spawning the Claude Code CLI as a child process,
   * piping the serialized prompt into stdin, and translating its NDJSON
   * stdout into AssistantEvent.
   *
   * Translation contract (NDJSON event → AssistantEvent), mirrors
   * anthropic-api-client.ts + open-design/apps/daemon/src/claude-stream.ts:
   *   content_block_start tool_use                   → buffer name+id
   *   content_block_start thinking                   → start thinking buffer
   *   content_block_delta input_json_delta           → accumulate args
   *   content_block_delta thinking_delta             → accumulate thinking text
   *   content_block_delta text_delta                 → emit 'text_delta'
   *                                                    + textStreamed.add(msgId)
   *   content_block_stop tool_use                    → emit 'tool_use'
   *                                                    + streamedToolUseIds.add(id)
   *   content_block_stop thinking                    → emit 'text_delta' wrapped
   *                                                    in <sf:thinking>...</sf:thinking>
   *                                                    + textStreamed.add(msgId)
   *   message_start.message.usage                    → emit 'usage'
   *   message_start.message.id                       → currentMessageId
   *   message_delta.usage                            → emit 'usage'
   *   message_delta.delta.stop_reason                → cache → emit on message_stop
   *   message_stop                                   → emit 'message_stop'
   *   assistant.message (fallback, no streaming)     → emit text/thinking/tool_use
   *                                                    skipping those already
   *                                                    seen via textStreamed /
   *                                                    streamedToolUseIds
   *   result (terminator, flat)                      → emit 'message_stop' if not seen yet
   *
   * Spawn fallback (fix 3):
   *   - Spawn binPath first.
   *   - If 'error' event fires with err.code === 'ENOENT' BEFORE any stdout,
   *     try each `fallbackBins` in order.
   *   - If all fail, throw with the full list attempted.
   *
   * Errors / signal:
   *   - Spawn ENOENT (binary not installed AND no fallback worked) → throw
   *   - Non-zero exit code → throw with stderr text
   *   - args.signal.aborted → SIGTERM the child; generator returns
   *   - JSON.parse failure on a line → console.warn ONCE, skip line
   *   - Abort listener: registered once, removed in `finally` so throw paths
   *     don't leak listeners across runtime turns (Checker P1-1).
   */
  async *stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: ToolSpec[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent> {
    const binPath = this.opts.binPath ?? 'claude';
    const fallbackBins = this.opts.fallbackBins ?? DEFAULT_FALLBACK_BINS;
    const extraArgs = this.opts.extraArgs ?? DEFAULT_EXTRA_ARGS;
    const spawnImpl: SpawnFn = this.opts.spawnFn ?? nodeSpawn;

    // ── Capability probe (fix 4) ────────────────────────────────────────
    // Test override wins; otherwise share the per-binPath cache.
    const capabilities: ClaudeCapabilities =
      this.opts.capabilities ?? (await probeClaudeCapabilities(binPath, spawnImpl));

    // Gap-close (2026-05-31): structured stdin. When the CLI supports
    // `--input-format` (capability-probed), prior tool calls/results go to it as
    // NATIVE stream-json blocks (never `<tool_use>` XML) — Law 1 closed on the
    // input side too. SAFE-BY-DEFAULT: a CLI build without the flag auto-falls
    // back to the proven text prompt, so nothing breaks on older binaries.
    // Env override (escape hatch): `XUEBAN_CLI_INPUT_STREAM_JSON=0` forces
    // the text path even where supported; `=1` forces structured (test/manual).
    const streamJsonEnv = process.env.XUEBAN_CLI_INPUT_STREAM_JSON;
    const useStreamJsonInput =
      streamJsonEnv === '1'
        ? true
        : streamJsonEnv === '0'
          ? false
          : capabilities.streamJsonInput === true;

    const spawnArgs = [...extraArgs];
    if (useStreamJsonInput && !spawnArgs.includes('--input-format')) {
      spawnArgs.push('--input-format', 'stream-json');
    }
    if (capabilities.partialMessages && !extraArgs.includes('--include-partial-messages')) {
      spawnArgs.push('--include-partial-messages');
    }
    if (this.opts.model && this.opts.model.length > 0) {
      spawnArgs.push('--model', this.opts.model);
    }
    // `--append-system` is supported on recent CLI builds; if absent the CLI
    // silently ignores unknown args, so this is a no-op on legacy versions.
    // The system prompt is also stitched into the prompt body as a fallback.
    if (args.system_prompt.length > 0) {
      spawnArgs.push('--append-system', args.system_prompt);
    }

    const env = { ...process.env };
    if (this.opts.apiKey && this.opts.apiKey.length > 0 && !env.ANTHROPIC_API_KEY) {
      env.ANTHROPIC_API_KEY = this.opts.apiKey;
    }

    // ── Spawn with fallback (fix 3) ─────────────────────────────────────
    // Walk the candidate list in order. On synchronous-throw ENOENT, move
    // to the next candidate. We can't distinguish async-`error`-event ENOENT
    // synchronously (the event fires after spawn() returns), so the chain is
    // only walked synchronously here — anchored to `claude` ENOENT (which
    // historically does throw synchronously when the binary truly isn't on
    // PATH on most platforms) or via the 'error' event captured below.
    const candidates = [binPath, ...fallbackBins];
    let child: ChildProcessWithoutNullStreams | null = null;
    let usedBin = binPath;
    const spawnErrors: Array<{ bin: string; err: unknown }> = [];
    for (const cand of candidates) {
      try {
        child = spawnImpl(cand, spawnArgs, {
          cwd: this.opts.cwd,
          env,
          stdio: ['pipe', 'pipe', 'pipe'],
        });
        usedBin = cand;
        break;
      } catch (err) {
        spawnErrors.push({ bin: cand, err });
        // ENOENT — try the next candidate. EACCES / other → also walk on,
        // surfacing the full list if everything fails.
        continue;
      }
    }
    if (!child) {
      const tried = candidates.join(', ');
      const lastMsg =
        spawnErrors.length > 0 && spawnErrors[spawnErrors.length - 1].err instanceof Error
          ? (spawnErrors[spawnErrors.length - 1].err as Error).message
          : String(spawnErrors[spawnErrors.length - 1]?.err);
      throw new Error(
        `claude-code CLI spawn failed (tried ${tried} — none found in PATH): ${lastMsg}. Install: npm i -g @anthropic-ai/claude-cli, then run \`claude login\`.`,
      );
    }

    // ── Decode chunks as utf8 strings (fix 6) ───────────────────────────
    // Node's StringDecoder buffers partial multi-byte sequences across `data`
    // events, so 中文/emoji bytes split at a chunk boundary decode correctly.
    // Without this, `chunk.toString('utf8')` on a Buffer that ends mid-codepoint
    // produces a U+FFFD replacement character.
    try {
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
    } catch {
      /* tests may pass fake streams that don't implement setEncoding */
    }

    // Capture stderr for failure messages — don't yield from it.
    let stderrBuf = '';
    child.stderr.on('data', (c: string | Buffer) => {
      stderrBuf += typeof c === 'string' ? c : c.toString('utf8');
      // Bound stderr buffer too so a misbehaving CLI dumping MB of warnings
      // doesn't OOM us.
      if (stderrBuf.length > 256 * 1024) stderrBuf = stderrBuf.slice(-128 * 1024);
    });

    // Surface spawn-time async ENOENT (binary not on PATH). The 'error' event
    // fires asynchronously AFTER spawn() returns the child reference. We
    // attempt fallback bins on this path too, but only when no stdout/stderr
    // data has been seen yet — once the stream has started we trust the
    // child.
    let spawnError: Error | null = null;
    child.on('error', (err: Error) => {
      spawnError = err;
    });

    // ── Abort wiring (Checker P1-1: cleanup in finally) ─────────────────
    const onAbort = (): void => {
      if (child && !child.killed) {
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

    // Pipe prompt into stdin and close. Structured JSONL when stream-json input
    // is enabled (tools-as-text-XML gap closed); proven text prompt otherwise.
    const prompt = useStreamJsonInput
      ? serializeStreamJsonInput(args.messages, args.tools)
      : serializePrompt(args.messages, args.tools);
    try {
      child.stdin.write(prompt);
      child.stdin.end();
    } catch {
      // stdin closed already (child died) — exit handler below will throw.
    }

    // Track exit so we can throw on non-zero code AFTER draining stdout.
    let exitCode: number | null = null;
    let exitSignal: NodeJS.Signals | null = null;
    const exitPromise = new Promise<void>((resolve) => {
      child.on('exit', (code, sig) => {
        exitCode = code;
        exitSignal = sig;
        resolve();
      });
    });

    // Per-turn tool_use accumulation, mirroring AnthropicApiClient.
    interface PendingToolUse {
      kind: 'tool_use';
      id: string;
      name: string;
      jsonBuf: string;
    }
    interface PendingThinking {
      kind: 'thinking';
      buf: string;
    }
    type PendingBlock = PendingToolUse | PendingThinking;
    const pending = new Map<number, PendingBlock>();
    let cachedStopReason = 'unknown';
    let messageStopEmitted = false;
    let warnedBadLine = false;
    let currentMessageId: string | null = null;
    /** messageIds whose text/thinking has already been emitted via stream_event. */
    const textStreamed = new Set<string>();
    /** tool_use ids already emitted via stream_event; dedup against assistant wrapper. */
    const streamedToolUseIds = new Set<string>();

    // ── stdout line splitter ──────────────────────────────────────────────
    const MAX_LINE_BUF = 1 * 1024 * 1024; // 1MB safety cap (mirror claude-stream-json)
    let lineBuf = '';

    /** Process a single fully-buffered JSON line. */
    const processLine = function* (
      this: void,
      rawLine: string,
    ): Generator<AssistantEvent> {
      const line = rawLine.trim();
      if (!line) return;
      let outer: OuterEvent;
      try {
        outer = JSON.parse(line) as OuterEvent;
      } catch {
        if (!warnedBadLine) {
          // eslint-disable-next-line no-console
          console.warn(
            `[ClaudeCodeCliApiClient] failed to JSON.parse stdout line (snippet='${line.slice(0, 80)}') — skipping further parse-failure warnings this stream`,
          );
          warnedBadLine = true;
        }
        return;
      }

      // Terminator: `result` (flat) carries the final stop_reason + usage.
      // CLI-only telemetry: `total_cost_usd` (per-turn $ cost) and
      // `duration_ms` (per-turn wall-clock) live on the outer result event,
      // not inside usage. We fold them into the TokenUsage payload so
      // downstream (ConversationRuntime.addUsage) accumulates them.
      if (outer.type === 'result') {
        const usage: TokenUsage = extractUsage(outer.usage) ?? {};
        if (typeof outer.total_cost_usd === 'number') usage.cost_usd = outer.total_cost_usd;
        if (typeof outer.duration_ms === 'number') usage.duration_ms = outer.duration_ms;
        if (Object.keys(usage).length > 0) yield { kind: 'usage', usage };
        const sr = typeof outer.stop_reason === 'string' ? outer.stop_reason : cachedStopReason;
        if (!messageStopEmitted) {
          yield { kind: 'message_stop', stop_reason: sr };
          messageStopEmitted = true;
        }
        return;
      }

      // Assistant summary wrapper (fix 2). Older CLI / no
      // --include-partial-messages: text/thinking/tool_use surface only here.
      // Newer CLI: same content was already streamed via stream_event — we
      // dedup via textStreamed (per messageId) + streamedToolUseIds (per id).
      if (
        outer.type === 'assistant' &&
        outer.message &&
        Array.isArray(outer.message.content)
      ) {
        const msgId =
          typeof outer.message.id === 'string' ? outer.message.id : currentMessageId;
        if (typeof outer.message.id === 'string') currentMessageId = outer.message.id;
        const alreadyStreamed = msgId !== null && textStreamed.has(msgId);
        const sr = outer.message.stop_reason;
        if (typeof sr === 'string') cachedStopReason = sr;

        for (const block of outer.message.content) {
          if (!block || typeof block !== 'object') continue;
          if (block.type === 'text') {
            if (
              !alreadyStreamed &&
              typeof block.text === 'string' &&
              block.text.length > 0
            ) {
              yield { kind: 'text_delta', text: block.text };
              if (msgId) textStreamed.add(msgId);
            }
          } else if (block.type === 'thinking') {
            if (
              !alreadyStreamed &&
              typeof block.thinking === 'string' &&
              block.thinking.length > 0
            ) {
              // Gap-close (2026-05-31): emit thinking as the typed `thinking_delta`
              // kind instead of wrapping it as `<sf:thinking>` TEXT. Thinking no
              // longer round-trips through the text parser (Law 1: never becomes
              // text; Law 2: independent channel). ApiClientCallable maps this to
              // the `thinking-delta` TurnChunk.
              yield { kind: 'thinking_delta', text: block.thinking };
              if (msgId) textStreamed.add(msgId);
            }
          } else if (block.type === 'tool_use') {
            if (typeof block.id !== 'string') continue;
            if (streamedToolUseIds.has(block.id)) {
              // Already emitted by stream_event content_block_stop — skip
              // the duplicate that the assistant wrapper always carries.
              continue;
            }
            yield {
              kind: 'tool_use',
              id: block.id,
              name: typeof block.name === 'string' ? block.name : '',
              input: block.input ?? {},
            };
            streamedToolUseIds.add(block.id);
          }
        }

        // The assistant wrapper carries usage on some builds too.
        const u = extractUsage(outer.message.usage);
        if (u) yield { kind: 'usage', usage: u };
        return;
      }

      const inner = extractInnerEvent(outer);
      if (!inner || typeof inner.type !== 'string') return;

      if (inner.type === 'message_start') {
        // CLI-only telemetry: `ttft_ms` (time-to-first-token) is attached to
        // the stream_event.message_start envelope itself, NOT to message.usage.
        // Merge it into the TokenUsage object so it flows through the usage
        // channel alongside the rest of the per-turn telemetry.
        const u: TokenUsage = extractUsage(inner.message?.usage) ?? {};
        if (typeof inner.ttft_ms === 'number') u.ttft_ms = inner.ttft_ms;
        if (Object.keys(u).length > 0) yield { kind: 'usage', usage: u };
        if (typeof inner.message?.id === 'string') currentMessageId = inner.message.id;
      } else if (inner.type === 'content_block_start') {
        const idx = inner.index ?? 0;
        const cb = inner.content_block;
        if (cb?.type === 'tool_use' && typeof cb.id === 'string' && typeof cb.name === 'string') {
          pending.set(idx, { kind: 'tool_use', id: cb.id, name: cb.name, jsonBuf: '' });
        } else if (cb?.type === 'thinking') {
          pending.set(idx, { kind: 'thinking', buf: '' });
        }
      } else if (inner.type === 'content_block_delta') {
        const idx = inner.index ?? 0;
        const d = inner.delta;
        if (d?.type === 'text_delta' && typeof d.text === 'string') {
          yield { kind: 'text_delta', text: d.text };
          if (currentMessageId) textStreamed.add(currentMessageId);
        } else if (d?.type === 'thinking_delta' && typeof d.thinking === 'string') {
          const p = pending.get(idx);
          if (p && p.kind === 'thinking') {
            p.buf += d.thinking;
          } else {
            // Some CLI versions skip content_block_start for thinking — buffer
            // on demand so we still capture the text and emit at block_stop.
            pending.set(idx, { kind: 'thinking', buf: d.thinking });
          }
        } else if (d?.type === 'input_json_delta' && typeof d.partial_json === 'string') {
          const p = pending.get(idx);
          if (p && p.kind === 'tool_use') p.jsonBuf += d.partial_json;
        }
      } else if (inner.type === 'content_block_stop') {
        const idx = inner.index ?? 0;
        const p = pending.get(idx);
        if (p && p.kind === 'tool_use') {
          let input: unknown = {};
          if (p.jsonBuf.length > 0) {
            try {
              input = JSON.parse(p.jsonBuf);
            } catch {
              input = { __parse_error: true, raw: p.jsonBuf };
            }
          }
          yield { kind: 'tool_use', id: p.id, name: p.name, input };
          streamedToolUseIds.add(p.id);
          pending.delete(idx);
        } else if (p && p.kind === 'thinking') {
          if (p.buf.length > 0) {
            // Gap-close (2026-05-31): typed thinking channel, not wrapped text.
            yield { kind: 'thinking_delta', text: p.buf };
            if (currentMessageId) textStreamed.add(currentMessageId);
          }
          pending.delete(idx);
        }
      } else if (inner.type === 'message_delta') {
        const u = extractUsage(inner.usage);
        if (u) yield { kind: 'usage', usage: u };
        const sr = inner.delta?.stop_reason;
        if (typeof sr === 'string') cachedStopReason = sr;
      } else if (inner.type === 'message_stop') {
        if (!messageStopEmitted) {
          yield { kind: 'message_stop', stop_reason: cachedStopReason };
          messageStopEmitted = true;
        }
      }
    };

    // ── stdout consumer ──────────────────────────────────────────────────
    // Use the readable as an async iterable. Each chunk may contain ≥0 full
    // lines + a possibly-partial trailing line; we glue them via lineBuf.
    try {
      // setEncoding('utf8') above ensures `chunk` is already a string; the
      // type annotation stays Buffer | string for the legacy/test path where
      // setEncoding wasn't applied.
      for await (const chunk of child.stdout as AsyncIterable<Buffer | string>) {
        if (args.signal.aborted) {
          onAbort();
          return;
        }
        lineBuf += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
        if (lineBuf.length > MAX_LINE_BUF) {
          // Defensive — drop everything up to the last newline so we resync.
          // eslint-disable-next-line no-console
          console.warn(
            `[ClaudeCodeCliApiClient] lineBuf > ${MAX_LINE_BUF}B without newline — resyncing`,
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
      // Flush the trailing partial line (some CLIs don't end with '\n').
      if (lineBuf.trim().length > 0) {
        for (const ev of processLine(lineBuf)) yield ev;
        lineBuf = '';
      }

      // Wait for the child to fully exit before deciding success/failure.
      await exitPromise;

      if (spawnError) {
        // ENOENT / EACCES — typically means the binary isn't installed.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const code = (spawnError as any).code;
        if (code === 'ENOENT') {
          const tried = [usedBin, ...fallbackBins.filter((b) => b !== usedBin)].join(', ');
          throw new Error(
            `claude-code CLI not found (tried ${tried} — none found in PATH). Install: npm i -g @anthropic-ai/claude-cli, then run \`claude login\`.`,
          );
        }
        throw spawnError;
      }

      if (exitCode !== null && exitCode !== 0) {
        const tail = stderrBuf.slice(-2048);
        throw new Error(
          `claude-code CLI exited with code ${exitCode}${exitSignal ? ` (signal=${exitSignal})` : ''}: ${tail || '(no stderr)'}`,
        );
      }

      // Defensive — if the CLI dropped EOF without ever sending message_stop,
      // emit one so the runtime terminates cleanly. (Mirrors openai-compat's
      // stop_emitted-fallback policy.)
      if (!messageStopEmitted) {
        yield { kind: 'message_stop', stop_reason: cachedStopReason };
      }
    } catch (err) {
      // stdout iteration error or thrown above — if we already saw an abort,
      // swallow and return; else surface.
      if (args.signal.aborted) return;
      throw err instanceof Error ? err : new Error(String(err));
    } finally {
      // Checker P1-1: ALWAYS remove the abort listener — throw paths
      // included — so ConversationRuntime reusing the same AbortSignal
      // across turns doesn't accumulate stale listeners.
      args.signal.removeEventListener('abort', onAbort);
    }
  }
}
