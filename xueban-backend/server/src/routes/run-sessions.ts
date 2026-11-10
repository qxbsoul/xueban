/**
 * run-sessions.ts — POST /api/run-sessions + GET /api/run-sessions/:id/stream
 *
 * Story 15.2 — switched stream handler to runSkillAssembler (skill-driven Claude
 * streaming with <sf:*> + <artifact> tag extraction). Adds AbortController so
 * client disconnect cancels the upstream Claude call. POST accepts skill_name.
 *
 * Story 15.8 — added run history persistence. After the assembler generator
 * finishes (success, error, or aborted-by-client), we capture artifact info
 * from any blueprint events and persist a RunRecord via storage/runs.ts so
 * RunsPage + StartPage can show the history. Aborts (client disconnect) do
 * NOT persist — only fully-terminated runs do.
 */

import { Router, Request, Response } from 'express';
import { v4 as uuidv4 } from 'uuid';
import { runSkillAssembler } from '../assembler';
import { classifyErrorCode } from '../lib/classify-error';
import {
  createRunBus,
  type RunEventSink,
  type RunEventRecord,
  type RunStatus,
} from '../lib/run-event-bus';
import { createRunEventStore } from '../lib/run-event-store';
import { startLoopWatcher } from '../lib/loop-watcher';
// 阶段 1（ROADMAP-agentinfra / GAP G1）— Node 工具管线的人在环审批回路：
// tool-approval 注册表经下方 emitter 把 permission_check 帧写进本文件的
// 私有 runBus（落事件日志、可重放），决策经 POST /:id/permissions/:requestId 回填。
import { toolApprovals, type PermissionDecision } from '../lib/tool-approval';
// Round 4 PR-E — canonical `@skill` token parser shared byte-equal with the
// frontend (`src/lib/skillToken.ts`). Replaces an inline regex that drifted
// in shape from the SkillDropdown UI; both call sites now agree on which
// goal text patterns are recognised as inline-skill tokens.
import { parseSkillToken } from '../lib/skill-token';
import { saveRun, saveRunArtifacts, type ArtifactType } from '../storage/runs';
import { getSetting } from '../storage/settings';
// 经验记忆自动沉淀（2026-11-05，整合 XUEBAN-OS PR#3 memory.py 设计）：
// run 失败→教训、用户中断→偏好，全部从已落库事实挖、零 LLM 调用。
import { remember, promptBlock } from '../storage/memory-lessons';
import { coercePackIds, knowledgeBlock } from '../lib/memory-bank';
import { SKILLS } from '../skills';
import { DESIGN_SYSTEMS } from '../design-systems';
import { composeSystemPrompt, type LayerToggles } from '../prompt-assembly';
import { composeMultiTurnPrompt } from '../prompts';
import { CONVERSATIONAL_AGENT_PROMPT } from '../prompts/conversational';
import { loadSkillSideFiles } from '../loaders/skill-side-files';
import {
  PROVIDER_IDS,
  isProviderId,
  type ProviderId,
} from '../transport/api-clients';
import { resolveProviderKey } from '../transport/byokKey';
// Story 15.14 — auto-critique pass after artifact-saved.
import { runCritique } from '../critic';
// Story 15.29 — multi-turn conversation glue: validate / auto-create
// conversation, write user + assistant messages tied to this session.
import {
  appendMessage,
  createConversation,
  getConversation,
  getConversationByGroup,
  getRecentMessages,
  setConversationGroup,
  updateMessageByTimelineId,
} from '../storage/conversations';
import {
  listRunIndexByConversation,
  setRunIndexStatus,
  upsertRunIndex,
} from '../storage/run-index';
import { getOrCreateProject, getProject } from '../storage/projects';
// 2026-05-19 — Cherry Studio parity: replace in-memory Map with a JSON-file
// backed store so backend restarts no longer wipe live sessions. Survives
// hot reload + crash; sensitive fields persisted under .xueban/ (gitignored,
// same trust boundary as settings.json).
import { createSessionStore } from '../lib/session-store';
// S1 (skill-team-conversion-design-v1.md §5) — ContentBlock + ConversationMessage
// data model; SessionRecord now carries `messages` so the future
// ConversationRuntime (S5) can replay multi-turn history across SSE reconnects.
import {
  SESSION_SCHEMA_VERSION,
  type ConversationMessage,
} from '../lib/conversation-types';
// S1.2 (intent-workflow-design-v1 §4.1) — TS-side intent classifier. Runs in
// the SSE handler before the assembler kicks off so the front-end gets a
// `classify` frame within ~1ms of stream open (LLM's own <sf:classify .../>
// arrives many seconds later, after parser extraction). Two frames coexist;
// front-end may diff them for consistency (S5.2 future story).
import { classifyTS, detectExplicitSingleAgent, hasExplicitTeamIntent, shouldUsePlainConversation, SINGLE_AGENT_DIRECTIVE } from '../lib/intent-router';
import { loadTeam as loadGlobalTeam } from '../lib/team-yaml';
// S2.3 (intent-workflow-design-v1 §4.4) — step artifact persistence + per-step
// SSE frame. Parser fires node/edge/blueprint between `assemble:running` and
// `assemble:done`; we collect them per open step, then on `done` package a
// StepArtifact → stepStore.put() + emit `step-artifact` for the front-end.
import { createStepStore } from '../lib/step-store';
import type { OutputKind, StepArtifact } from '../lib/contracts';
// S6.10-A — Trae/Codex-style timeline projection. Runs in parallel with the
// legacy fine-grained event stream; emits `message` + `message-patch` SSE
// frames that the front-end can subscribe to incrementally. Legacy events are
// preserved unchanged so existing consumers keep working.
import { createTimelineProjector } from '../lib/timeline-projector';
// Polynoia-parity — git evidence (commit records / real file diffs) captured
// around the run inside a git-backed workspace. All helpers degrade to null.
import { inspectGitRun, resolveGitHead } from '../lib/git-introspect';
import type { PlanTaskRow } from '../lib/contracts';
// S6.3 — skill-team synthesizer. When the active skill ships a structured
// team.skill.yaml we bypass the LLM entirely and stream the design straight
// from disk. Deterministic, free, and perfectly matches the v3 stacked
// AgentDetail because every byte already lives in the skill files.
import type { TeamDef, SkillSlot } from '../lib/skill-types';

const router = Router();

// ── Tool-call projection (clawsgo left-pane parity) ──────────────────────────
// Extracted as a pure, exported factory so it can be unit-tested in isolation
// (mirrors the `emitToolChunk` contract test). It translates the runtime's
// structured `tool-use` / `tool-result` SSE payloads into normalized
// `tool-call` frames the StepList nests under the owning step:
//   { id, tool, title, input_summary, status: 'running'|'done'|'failed',
//     result_summary?, result_count?, links?, elapsed_ms?, step_index?, node_id? }
// Every field is derived from REAL runtime data — no fabrication. The projector
// keeps an in-flight map keyed by the tool_use call id so the matching
// `tool-result` flips status + attaches result summary, result_count, links and
// elapsed timing. `node_id` already scopes calls per agent, so team/DAG runs
// (whose chunks are node-stamped by executor.ts before pipeChunksToSse) project
// through this exact path with no extra wiring.

/** A normalized tool-call SSE payload. */
export interface ToolCallProjection {
  id: string;
  tool: string;
  title: string;
  input_summary: string;
  status: 'running' | 'done' | 'failed';
  result_summary?: string;
  /** clawsgo "找到 N 个结果" — real hit count parsed from the result, when known. */
  result_count?: number;
  links?: Array<{ label: string; url: string }>;
  elapsed_ms?: number;
  step_index?: number;
  node_id?: string;
}

interface InFlightToolCall {
  id: string;
  tool: string;
  step_index: number | null;
  node_id?: string;
  started_at: number;
}

/** Collapse an arbitrary tool input object into a one-line human summary. */
export function summarizeToolInput(input: unknown): string {
  if (input == null) return '';
  if (typeof input === 'string') return input.slice(0, 160);
  if (typeof input !== 'object') return String(input).slice(0, 160);
  const obj = input as Record<string, unknown>;
  for (const key of ['query', 'q', 'search', 'url', 'path', 'file_path', 'command', 'cmd', 'pattern', 'prompt']) {
    const v = obj[key];
    if (typeof v === 'string' && v.trim()) return `${key}: ${v.trim()}`.slice(0, 160);
  }
  try {
    return JSON.stringify(obj).slice(0, 160);
  } catch {
    return '';
  }
}

/** Collapse a tool result payload into a one-line summary. */
export function summarizeToolResult(output: unknown): string {
  if (output == null) return '';
  const text = typeof output === 'string' ? output : (() => {
    try { return JSON.stringify(output); } catch { return String(output); }
  })();
  return text.replace(/\s+/g, ' ').trim().slice(0, 200);
}

/**
 * Extract clickable result links (http(s) URLs) from a tool output — deduped,
 * capped at 5, host+path labelled — so the StepList can render clawsgo's
 * "found N results → clickable links". Only URLs that really appeared.
 */
export function extractToolLinks(output: unknown): Array<{ label: string; url: string }> {
  const text = typeof output === 'string' ? output : (() => {
    try { return JSON.stringify(output); } catch { return ''; }
  })();
  if (!text) return [];
  const urls = text.match(/https?:\/\/[^\s"'<>)\]]+/g);
  if (!urls) return [];
  const seen = new Set<string>();
  const links: Array<{ label: string; url: string }> = [];
  for (const url of urls) {
    const clean = url.replace(/[.,;]+$/, '');
    if (seen.has(clean)) continue;
    seen.add(clean);
    let label = clean;
    try {
      const u = new URL(clean);
      label = u.hostname.replace(/^www\./, '') + (u.pathname !== '/' ? u.pathname : '');
    } catch { /* keep raw */ }
    links.push({ label: label.slice(0, 60), url: clean });
    if (links.length >= 5) break;
  }
  return links;
}

/**
 * Parse a structured result count out of a tool result — clawsgo's
 * "找到 N 个结果" beat. Search / lookup tools announce their hit count in a
 * leading phrase ("found 12 results", "12 个结果", "3 matches", "5 行" …); we
 * pull the FIRST such number. Returns undefined when no count phrase is found,
 * so callers fall back to the link count. Only REAL numbers are surfaced.
 */
export function parseResultCount(output: unknown): number | undefined {
  const text = typeof output === 'string' ? output : (() => {
    try { return JSON.stringify(output); } catch { return ''; }
  })();
  if (!text) return undefined;
  const patterns: RegExp[] = [
    /found\s+(\d[\d,]*)\s+(?:results?|matches|hits|items|files?|rows?)/i,
    /(\d[\d,]*)\s+(?:results?|matches|hits|items|files?|rows?)\b/i,
    /找到\s*(\d[\d,]*)\s*(?:个|条)?\s*(?:结果|匹配|项|条目|文件|行)/,
    /(\d[\d,]*)\s*(?:个|条)\s*(?:结果|匹配|项|条目|文件)/,
    /(\d[\d,]*)\s*行/,
  ];
  for (const re of patterns) {
    const m = text.match(re);
    if (m && m[1]) {
      const n = Number(m[1].replace(/,/g, ''));
      if (Number.isFinite(n) && n >= 0) return n;
    }
  }
  return undefined;
}

/**
 * Create a stateful tool-call projector. `idSeed` seeds the synthetic call-id
 * fallback used when the runtime omits a tool_use id. `now` is injectable for
 * deterministic elapsed timing in tests.
 *
 * `onToolUse` / `onToolResult` each return the projected `tool-call` payload to
 * forward over SSE, or null when the event carries no usable tool data.
 */
export function createToolCallProjector(opts?: { idSeed?: string; now?: () => number }) {
  const idSeed = (opts?.idSeed ?? 'sess').slice(0, 6);
  const now = opts?.now ?? (() => Date.now());
  const inFlight = new Map<string, InFlightToolCall>();
  let seq = 0;

  return {
    onToolUse(data: unknown, currentStepIndex: number | null): ToolCallProjection | null {
      if (!data || typeof data !== 'object') return null;
      const d = data as { name?: unknown; input?: unknown; id?: unknown; node_id?: unknown };
      const tool = typeof d.name === 'string' && d.name ? d.name : 'tool';
      const callId = typeof d.id === 'string' && d.id ? d.id : `tc-${idSeed}-${seq++}`;
      const node_id = typeof d.node_id === 'string' ? d.node_id : undefined;
      inFlight.set(callId, { id: callId, tool, step_index: currentStepIndex, node_id, started_at: now() });
      return {
        id: callId,
        tool,
        title: summarizeToolInput(d.input) || tool,
        input_summary: summarizeToolInput(d.input),
        status: 'running',
        ...(currentStepIndex !== null ? { step_index: currentStepIndex } : {}),
        ...(node_id ? { node_id } : {}),
      };
    },

    onToolResult(data: unknown): ToolCallProjection | null {
      if (!data || typeof data !== 'object') return null;
      const d = data as { for?: unknown; output?: unknown; is_error?: unknown; node_id?: unknown };
      const callId = typeof d.for === 'string' ? d.for : undefined;
      const tracked = callId ? inFlight.get(callId) : undefined;
      const isError = d.is_error === true;
      const links = extractToolLinks(d.output);
      // clawsgo "找到 N 个结果" — real count phrase first, else link count.
      const parsedCount = parseResultCount(d.output);
      const resultCount = parsedCount != null ? parsedCount : links.length > 0 ? links.length : undefined;
      const resolvedNode = typeof d.node_id === 'string' ? d.node_id : tracked?.node_id;
      const out: ToolCallProjection = {
        id: callId ?? `tc-${idSeed}-${seq++}`,
        tool: tracked?.tool ?? 'tool',
        title: tracked?.tool ?? 'tool',
        input_summary: '',
        status: isError ? 'failed' : 'done',
        result_summary: summarizeToolResult(d.output),
        ...(resultCount != null ? { result_count: resultCount } : {}),
        ...(links.length > 0 ? { links } : {}),
        ...(tracked ? { elapsed_ms: now() - tracked.started_at } : {}),
        ...(tracked?.step_index != null ? { step_index: tracked.step_index } : {}),
        ...(resolvedNode ? { node_id: resolvedNode } : {}),
      };
      if (callId) inFlight.delete(callId);
      return out;
    },
  };
}

interface SessionRecord {
  goal: string;
  skill_name: string;
  /** 2026-06-01 — 用户显式 @skill:<id> 或显式 skill_name 触发(非默认回退)。
   *  true → assembler 走"组装意图"路:Branch 2 兜底时用组装指令(禁 discovery),
   *  而非该 skill 的对话 prompt。见 .xueban/skills/agent-team-assembly。 */
  explicit_skill?: boolean;
  /** 2026-06-01 — 用户原始请求的 skill id(@skill:<id> 的 <id> / 显式 skill_name),
   *  未经 SKILLS 注册表回退。≠ skill_name 时说明请求的 skill 不在注册表里;assembler
   *  Branch 2 据此用 read_skill 去磁盘/网络拉真实蓝图,而非静默用默认 blueprint。 */
  skill_requested?: string;
  output_hint?: string;
  workspace_id?: string;
  mode?: string;
  anthropic_key?: string;
  /** Story 15.18 — selected LLM provider (default 'anthropic'). */
  provider?: ProviderId;
  /** Story 15.18 — BYOK key for the selected provider. */
  api_key?: string;
  /** Story 15.5 — Design System id chosen by user; defaults to 'none'. */
  design_system_id?: string;
  /** Story 15.9 — generation overrides forwarded from front-end localStorage. */
  model?: string;
  max_tokens?: number;
  temperature?: number;
  /**
   * Story 15.19 v2 — optional executor override. POST body / future UI hint.
   * Resolution order at the assembler boundary:
   *   session.executor > skill.executor (frontmatter) > 'anthropic-direct'.
   */
  executor?: string;
  /** 2026-05-30 — CLI 工作目录(绝对路径)。空 → assembler 回退到产物目录。
   *  仅 cli:/acp:/mcp: 有意义。 */
  cwd?: string;
  /** Story 15.13 — multi-layer prompt assembly inputs (all optional). */
  project_meta?: Record<string, unknown> | null;
  /** Story 15.12 interface — pre-rendered side-files block; loader to be wired by 15.12. */
  side_files?: string;
  layer_toggles?: LayerToggles;
  /**
   * 2026-11-06 — 对话框知识库接线：多模态记忆库 pack id 列表（用户在 composer
   * KNOWLEDGE chip 勾选）。stream 组装提示词时按 goal 检索命中注入「知识库参考」
   * 块（fail-soft）；多轮接力每轮以当轮 goal 重新检索。
   */
  knowledge_pack_ids?: string[];
  /**
   * Story 15.29 — conversation this run belongs to. Always set: when the
   * client omits `conversation_id` we auto-create an anonymous one under
   * the 'default' project so every run lives somewhere.
   */
  conversation_id?: string;
  /**
   * Story 15.14 follow-up 2026-05-11 — per-request auto_critique override
   * from the UI toggle. When `false` the finally{} block skips the critique
   * pass. When `undefined` the server falls back to settings.json (default
   * true to preserve current behavior).
   */
  auto_critique?: boolean;
  /**
   * S1 (skill-team-conversion-design-v1.md §5 / D8) — session schema version.
   * 0 / missing  → pre-S1, no `messages` field on disk.
   * 1            → current; SessionRecord guarantees `messages: ConversationMessage[]`.
   * Migrator in createSessionStore() below normalizes 0 → 1 on load.
   */
  version?: number;
  /**
   * S1 — multi-turn conversation history. ContentBlock-shaped, mirrors
   * Anthropic Messages API. Populated by ConversationRuntime (S5) once it
   * lands; for now writers may leave it empty.
   */
  messages?: ConversationMessage[];
  created_at: number;
  /**
   * 2026-06-01 — last-activity timestamp for the activity-based TTL sweep.
   * Bumped on every persist (touchSession) AND on each /stream attach, so a
   * session a user keeps coming back to never expires out from under them.
   * Old records (pre-2026-06-01) lack it; migrator defaults it to created_at.
   * Replaces the old "created_at < now-1h" sweep that nuked sessions an hour
   * after creation regardless of activity (root cause of the spurious
   * "Session 不存在或已过期 404" the user hit on revisit).
   */
  updated_at?: number;
}

// ── Story 15.13 — layer_toggles + project_meta validators ────────────────────

// 2026-05-11 review M1 (15.29): 加 'conversation_history' — 否则 client 通过
// POST layer_toggles.conversation_history=false 想关闭历史会被 coerceLayerToggles
// 静默丢弃 → 服务端永远 always-on，违反 spec AC3 toggle 契约。
const LAYER_KEYS: ReadonlyArray<keyof LayerToggles> = [
  'discovery',
  'identity',
  'ds',
  'skill',
  'project',
  'conversation_history',
  'sides',
  'framework',
];

function coerceLayerToggles(raw: unknown): LayerToggles | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const out: LayerToggles = {};
  for (const k of LAYER_KEYS) {
    const v = (raw as Record<string, unknown>)[k];
    if (typeof v === 'boolean') out[k] = v;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

function coerceProjectMeta(raw: unknown): Record<string, unknown> | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  // Shallow copy — composer handles per-value JSON.stringify.
  return { ...(raw as Record<string, unknown>) };
}

// ── Story 15.9 — generation override validators ──────────────────────────────
//
// 2026-05-16 — Removed the claude-only MODEL_ALLOWLIST. Previously this Set
// of 3 ids silently dropped every non-Claude model the user picked, so a
// BYOK selection like `glm-5.1` / `gpt-4o` / `deepseek-chat` was coerced to
// undefined and the server fell back to the default Claude model — the
// classic "frontend done, backend not wired up" failure mode. We now accept
// any non-empty string and let the per-provider streamCompletion surface
// MODEL_NOT_FOUND if the id is bogus.

const MODEL_MAX_LEN = 200;
const MAX_TOKENS_MIN = 1024;
const MAX_TOKENS_MAX = 32768;

function coerceModel(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > MODEL_MAX_LEN) return undefined;
  // Reject suspicious patterns (whitespace mid-id, control chars) — anything
  // that wouldn't be a legal LLM model identifier — to avoid downstream
  // weirdness without enforcing a per-provider allowlist.
  if (/[\s\x00-\x1f]/.test(trimmed)) return undefined;
  return trimmed;
}

function coerceMaxTokens(raw: unknown): number | undefined {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return undefined;
  const n = Math.floor(raw);
  return n >= MAX_TOKENS_MIN && n <= MAX_TOKENS_MAX ? n : undefined;
}

function coerceTemperature(raw: unknown): number | undefined {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return undefined;
  return raw >= 0 && raw <= 1 ? raw : undefined;
}

// ── Story 15.29 — conversation history rendering ─────────────────────────
//
// Reads up to 20 recent messages from sqlite (ascending order — getRecentMessages
// already does this). Excludes the trailing user message we just appended in
// this turn (so it isn't duplicated inside the history layer AND inside the
// goal slot). Renders a markdown block with `### User` / `### Assistant`
// headings. Token-bloat guards:
//   - per-message content > 1024 char → truncate + "…(truncated)" suffix
//   - total block > 4096 char → drop oldest until under cap, preserve order
//
// Returns `undefined` when there is no conversation_id, no prior messages,
// or only the single just-written user message — composeSystemPrompt then
// drops the conversation_history layer entirely.
const HISTORY_LIMIT = 20;
const HISTORY_PER_MSG_MAX = 1024;
const HISTORY_BLOCK_MAX = 4096;

// sse-frame-leak stop-bleed (2026-05-31). Xueban serializes its own SSE as
// `event: <name>\ndata: <one-line json>\n\n`. When a prior turn's leaked frames
// got stored as assistant content, re-injecting them here as CONVERSATION
// HISTORY makes the LLM parrot the frame shape back — and the parser's
// sse-frame-leak guard then renders the WHOLE turn as `raw` blocks (no real
// <sf:node>/<sf:complete> ever produced → TEAM 0 + errored run). We strip the
// frame-shaped lines before they re-enter the prompt, closing the feedback loop
// at its source. Tight match (an `event:` line immediately followed by a `data:`
// line opening with `{`/`[`) so normal prose is untouched. Mirrors
// parser.ts:588's guard regex. The structural fix (typed event stream, control
// frames never sharing the text channel) is tracked in the root-cure plan doc.
const SSE_FRAME_LINE_RE =
  /^[ \t]*event:[ \t]*[\w.-]+[ \t]*\r?\n[ \t]*data:[ \t]*[{[][^\n]*\r?\n?/gm;

export function stripLeakedSseFrames(content: string): string {
  if (!content.includes('event:')) return content; // fast path — no candidates
  return content.replace(SSE_FRAME_LINE_RE, '');
}

function renderConversationHistoryBlock(
  conversation_id: string | undefined,
): string | undefined {
  if (!conversation_id) return undefined;
  let msgs;
  try {
    msgs = getRecentMessages(conversation_id, HISTORY_LIMIT);
  } catch (e) {
    console.error('[run-sessions] getRecentMessages failed:', e);
    return undefined;
  }
  // Drop the trailing user message (it IS this turn's goal — already shown
  // to the LLM as the user message; the layer is *prior* turns).
  if (msgs.length > 0 && msgs[msgs.length - 1].role === 'user') {
    msgs = msgs.slice(0, -1);
  }
  if (msgs.length === 0) return undefined;

  const renderOne = (m: { role: string; content: string }): string => {
    const role =
      m.role === 'user' ? 'User' : m.role === 'assistant' ? 'Assistant' : 'System';
    // sse-frame-leak stop-bleed: never re-feed frame-shaped text as context.
    let body = stripLeakedSseFrames(m.content);
    if (body.length > HISTORY_PER_MSG_MAX) {
      body = body.slice(0, HISTORY_PER_MSG_MAX) + '…(truncated)';
    }
    // 2026-05-11 review M2 (15.29, OpenDesign 模式 — 与 15.12 fence wrap 同款):
    // 用 markdown code fence 包裹 message body，防恶意 user message 含
    // `\n---\n## DISCOVERY\n…` 之类的 layer separator (`\n\n---\n\n`) 突破
    // composer 边界注入新 layer 段。strip 内含 ``` 防 fence escape。
    const safeBody = body.replace(/```/g, '` ` `');
    return `### ${role}\n\`\`\`\n${safeBody}\n\`\`\``;
  };

  // Drop oldest until total fits under cap.
  let rendered = msgs.map(renderOne);
  let block = rendered.join('\n\n');
  while (block.length > HISTORY_BLOCK_MAX && rendered.length > 1) {
    rendered = rendered.slice(1);
    block = rendered.join('\n\n');
  }
  // If even the most recent single message is over cap, hard-truncate it.
  if (block.length > HISTORY_BLOCK_MAX) {
    block = block.slice(0, HISTORY_BLOCK_MAX) + '…(truncated)';
  }

  return [
    '## CONVERSATION HISTORY',
    '',
    '(Earlier turns in this conversation, oldest first. Use them as context — do not restate.)',
    '',
    block,
  ].join('\n');
}

// Persistent session store — drop-in replacement for `new Map<>()` with
// JSON-file persistence. Hydrated at module load (fire-and-forget; reads
// before hydration completes simply miss until the file lands, which is
// fine because in normal flows POST creates the session before any GET).
// S1 (D8) — migrator runs on every record read from disk in loadAll(). Old
// records (pre-S1) lack `messages` + `version`; we inject `messages: []` and
// bump version to the current schema. We're defensive about the raw shape:
// anything not a plain object is dropped (warning logged by the caller).
const sessionStore = createSessionStore<SessionRecord>({
  migrate: (raw): SessionRecord | undefined => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
    const rec = raw as Partial<SessionRecord> & Record<string, unknown>;
    // Only the new fields are touched; everything else round-trips as-is.
    if (!Array.isArray(rec.messages)) rec.messages = [];
    if (typeof rec.version !== 'number' || rec.version < SESSION_SCHEMA_VERSION) {
      rec.version = SESSION_SCHEMA_VERSION;
    }
    // 2026-06-01 — pre-existing records have no `updated_at`; seed it from
    // created_at so the activity-based TTL has a sensible starting point.
    if (typeof rec.updated_at !== 'number') {
      rec.updated_at = typeof rec.created_at === 'number' ? rec.created_at : Date.now();
    }
    return rec as SessionRecord;
  },
});
// 后端合一·阶段0 — 启动回填：把内存 store 里已有的 session↔conversation 映射
// 补进持久 run_index（幂等 upsert），老 run 的历史从此也不怕 TTL 回收。
void sessionStore.loadAll().then(() => {
  try {
    for (const [id, s] of sessionStore.entries()) {
      if (!s.conversation_id) continue;
      upsertRunIndex({
        session_id: id,
        conversation_id: s.conversation_id,
        goal: s.goal ?? '',
        created_at: s.created_at,
      });
    }
  } catch (err) {
    console.warn('[run-sessions] run_index backfill failed:', err);
  }
});

// 2026-06-01 — activity-based session TTL. Old code swept any session
// `created_at < now-1h`, i.e. killed it one hour after CREATION no matter how
// recently it was viewed — so coming back to a run after lunch surfaced a
// spurious "Session 不存在或已过期 (404)". Sessions persist to disk (session-store),
// so the only thing TTL guards is unbounded cardinality; an idle window is the
// right axis, not absolute age. 24h of inactivity is generous for a desktop
// tool and still bounds the on-disk session count.
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

/** Effective last-activity instant for the TTL sweep. */
function sessionLastActivity(s: SessionRecord): number {
  return Math.max(s.created_at ?? 0, s.updated_at ?? 0);
}

/**
 * Re-persist a session with a refreshed `updated_at`. Cheap (session-store
 * double-writes async) and keeps an actively-viewed session from expiring.
 * No-op if the session is already gone.
 */
function touchSession(id: string): void {
  const s = sessionStore.get(id);
  if (!s) return;
  s.updated_at = Date.now();
  sessionStore.set(id, s);
}

// S2.3 — module-level singleton. One bucket per session; cleanup happens
// alongside sessionStore's activity-based TTL sweep (clear() on session delete
// is effectively handled by the step-store's idempotent disk-removal).
const stepStoreSingleton = createStepStore();

// T3-1 (2026-05-27): the legacy per-connection `activeStreams` registry +
// ActiveStream interface were removed. Abort / retry-pending / resume-pending
// now go through `runBus` (emit + cancel), which buffers and fans out to every
// attached view instead of poking one connection's `res`.

// T3-1 (docs/architecture/opendesign-streaming-architecture-study.md §5): the
// run-event bus makes each run a persistent entity with a buffered, monotonically
// numbered event log + a set of attached SSE views. The pipeline runs ONCE
// (guarded by runBus.claimStart) and emit()s into the log; every GET /stream
// connection is a detachable view that resumes from `?after`/Last-Event-ID.
// This is what decouples execution from the SSE connection — reconnects replay
// the buffered events instead of re-running the whole LLM pipeline.
// O2 / T3-5 — inject disk persistence so the run timeline survives a Node
// restart (hydrated at construction; debounced snapshots on emit/finish). Runs
// that were mid-flight at crash hydrate as `canceled` with their history intact
// + a synthetic "interrupted by restart" terminal frame, so a reconnect replays
// the timeline read-only instead of re-running the pipeline.
const runBus = createRunBus({ persist: createRunEventStore() });

// 阶段 1 — tool-approval 帧直写 runBus：待审请求与最终决策都以命名事件
// `permission_check` 落进该 run 的单调编号事件日志（重连经 attach 重放，
// 重启经 run-event-store hydrate）——「决策为命名事件，可回放」的落点。
// 注册表本体在 lib（assembler / executor 构造 prompter 时引用同一单例）。
toolApprovals.setEmitter((sessionId, event, frame) => {
  runBus.emit(sessionId, event, frame);
});

/**
 * T3 A8 — graceful shutdown entry point for the daemon. Cancels every active
 * run (aborting its signal → the cli spawner SIGTERM→SIGKILLs its child) and
 * waits a short grace so child processes are reaped before the process exits.
 * Wired to SIGTERM/SIGINT in index.ts.
 */
export async function shutdownActiveRuns(graceMs?: number): Promise<void> {
  await runBus.shutdownActive(graceMs !== undefined ? { graceMs } : undefined);
}

/**
 * O4 — /stream disposition resolver (pure, unit-testable).
 *
 * When a client opens GET /:id/stream we must decide HOW to serve it. The
 * legacy behavior hard-404'd the moment the in-memory session was gone (e.g.
 * the 1h session TTL fired, or the user came back to an old run after a restart)
 * — even though the run's full timeline may still be replayable from the
 * run-event-bus (hydrated from disk). That looked like a broken/lost run.
 *
 * New disposition, in priority order:
 *   - `live`   : the session is present → normal path (may claimStart + run).
 *   - `replay` : the session is gone but the run exists in the bus (live OR
 *                hydrated-from-disk) → READ-ONLY replay of the buffered timeline,
 *                ending with whatever terminal/synthetic frame the bus holds.
 *                NEVER claimStart, NEVER re-execute, NEVER spend tokens.
 *   - `gone`   : neither a session nor any recoverable run → accurate 404.
 *
 * `run` is the result of looking the run up in the bus AFTER an attempted lazy
 * `hydrateOne` (so a disk-only snapshot has already been pulled back in). Keeping
 * this a pure function of {hasSession, run} makes the decision testable without
 * Express/fs (mirrors the swapDagRun unit-test pattern in groups-chat.ts).
 */
export type StreamDisposition =
  | { kind: 'live' }
  | { kind: 'replay'; status: RunStatus }
  | { kind: 'gone' };

export function resolveStreamDisposition(args: {
  hasSession: boolean;
  run: { status: RunStatus } | undefined;
}): StreamDisposition {
  if (args.hasSession) return { kind: 'live' };
  if (args.run) return { kind: 'replay', status: args.run.status };
  return { kind: 'gone' };
}

// ── B2 · 统一消息读路径 (unified-conversation-core-plan-2026-06-11 §B2) ──────
// groups-chat.ts 的 GET /api/groups/:id/timeline 需要「conversation → run 列表
// → 每个 run 的事件日志」，但 sessionStore / runBus 都是本文件私有（刻意：执行
// 状态单一所有权）。这里导出两个轻量只读查询，而不是把 store 本体漏出去。

/**
 * List run-session ids belonging to a conversation, most-recently-updated
 * first. Read-only; never touches runBus.
 */
export function listSessionsByConversation(conversationId: string): string[] {
  const hits = new Map<string, number>();
  // 阶段0 — 持久索引为主源（TTL 回收后历史仍在）…
  try {
    for (const r of listRunIndexByConversation(conversationId)) {
      hits.set(r.session_id, r.updated_at);
    }
  } catch {
    // sqlite 不可用时仅靠内存——保持旧行为
  }
  // …内存 store 兜底合并（覆盖索引未及写入的瞬时窗口）。
  for (const [id, s] of sessionStore.entries()) {
    if (s.conversation_id !== conversationId) continue;
    hits.set(id, Math.max(hits.get(id) ?? 0, s.updated_at ?? s.created_at ?? 0));
  }
  return [...hits.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);
}

/**
 * Read-only snapshot of one run's status + buffered event log for timeline
 * replay. Uses `hydrateOne` so a run whose snapshot is on disk but not in
 * memory (post-restart / post-TTL) is recovered with the same read-only
 * semantics as the /stream replay path. Returns null when there is nothing
 * to replay (no in-memory run, no disk snapshot).
 */
export function getRunReplay(
  sessionId: string,
): { status: RunStatus; events: RunEventRecord[] } | null {
  const run = runBus.hydrateOne(sessionId) ?? runBus.get(sessionId);
  if (!run) return null;
  return { status: run.status, events: run.events };
}

/**
 * 2026-11-04 群聊「停止生成」— 按会话 id 取消一个在跑的 run（执行状态
 * 单一所有权仍在 runBus；groups-chat.ts 的 stop 路由经此入口取消 gateway
 * run，与 POST /:id/abort 的 runBus.cancel 同一通路）。幂等：unknown id /
 * 已终态都静默返回 false，调用方据此决定是否还走 AbortController 通道。
 */
export function cancelRunSession(sessionId: string): boolean {
  const run = runBus.get(sessionId) ?? runBus.hydrateOne(sessionId);
  if (!run || runBus.isTerminal(run.status)) return false;
  runBus.emit(sessionId, 'aborted', { session_id: sessionId, reason: 'user_requested' });
  runBus.cancel(sessionId);
  return true;
}

// ── B3 · 网关过程事件 (unified-conversation-core-plan-2026-06-11 §B3) ────────
// 聊天网关 (groups-chat.ts runFanout) 执行期的过程（thinking/原始流/状态行）
// 复用 run 体系管线：每轮 fanout 注册一条轻量 "gateway run"，过程投影成
// TimelineMessage 经 'message'/'message-patch' 事件 emit 进 runBus 事件日志。
// B2 的 GET /api/groups/:id/timeline 已按 conversation 归并所有 run —— 这些
// 过程卡因此零前端改动即可回放（实时推送是 F5 的活）。
//
// sessionStore / runBus 仍保持本文件私有（与 listSessionsByConversation /
// getRunReplay 同款边界）：这里只导出一个注册入口，emit/finish 以闭包形式
// 交还调用方，store 本体不外漏。

/** B3 — handle returned by registerGatewayRun; closures over the private runBus. */
export interface GatewayRunHandle {
  session_id: string;
  /** Append a 'message' / 'message-patch' event into this run's event log. */
  emit: (event: 'message' | 'message-patch', payload: unknown) => void;
  /** Mark the run terminal (idempotent — runBus.finish guards re-entry). */
  finish: (status: Exclude<RunStatus, 'running'>) => void;
}

/**
 * B3 — register a lightweight "gateway run" session for one chat fan-out round.
 * The session appears in GET /api/run-sessions (per conversation) and its event
 * log feeds the B2 timeline merge. `claimStart` is called here ON PURPOSE: if
 * the front-end later opens GET /:id/stream on this session, the 'live' path's
 * claimStart returns false → it only ATTACHES (replays the gateway's events)
 * and never boots the skill-assembler pipeline for a chat round.
 */
export function registerGatewayRun(meta: {
  goal: string;
  conversation_id: string;
}): GatewayRunHandle {
  const session_id = uuidv4();
  sessionStore.set(session_id, {
    goal: meta.goal,
    skill_name: 'chat-gateway',
    conversation_id: meta.conversation_id,
    created_at: Date.now(),
    updated_at: Date.now(),
  });
  // 阶段0 — 持久索引：TTL 回收后历史过程卡仍可经 conversation 找回
  try {
    upsertRunIndex({ session_id, conversation_id: meta.conversation_id, goal: meta.goal });
  } catch { /* sqlite 不可用不阻塞聊天 */ }
  runBus.claimStart(session_id); // ensure() + started=true（单一执行权归网关）
  return {
    session_id,
    emit: (event, payload) => {
      runBus.emit(session_id, event, payload);
    },
    finish: (status) => {
      runBus.finish(session_id, status);
      touchSession(session_id); // 结束时刷新活跃度，避免 TTL 早扫
      try { setRunIndexStatus(session_id, status ?? 'succeeded'); } catch { /* 同上 */ }
      // 经验记忆自动沉淀（best-effort）— 网关聊天轮失败同样记教训
      try {
        recordRunLesson(
          { skill_name: 'chat-gateway', goal: meta.goal },
          { aborted: status === 'canceled', failed: status === 'failed', errorMessage: status === 'failed' ? 'chat gateway round failed' : null },
        );
      } catch { /* 非致命 */ }
    },
  };
}

const VALID_ARTIFACT_TYPES: ReadonlyArray<ArtifactType> = ['yaml', 'html', 'markdown'];

/**
 * 经验记忆召回块（run 提示词注入用）。按 skill_name 分域，空块返回 ''。
 * 开关：settings.memory.autoLearn（默认开）；settings.memory.recallTopK（默认 4）。
 * 2026-11-07 — 开关/条数/格式统一走 storage/memory-lessons.promptBlock。
 */
function lessonsBlockForSession(skillName: string): string {
  return promptBlock(skillName ?? '', '');
}

/** skill 层内容 + 经验块拼接（两者都可能为空）。 */
function joinLessons(base: string | undefined, lessonsBlock: string): string | undefined {
  const baseStr = base ?? '';
  if (!baseStr) return lessonsBlock || undefined;
  return baseStr + lessonsBlock;
}

/**
 * 经验记忆自动沉淀（2026-11-05，整合 XUEBAN-OS PR#3 memory.py 设计）。
 *
 * 口径对齐对方"从已有事实里挖、不额外调模型"：
 *   - run 失败 → 教训（kind=lesson, source='run_failed'）：失败原因原文就是教训。
 *   - 用户中断 → 偏好（kind=preference, source='user_abort'）：用户不想让它跑完
 *     是对该类任务的明确态度。
 * 分域：scope_key 取 skill_name（与对方 employees.role 对齐）；task_type 一期留空。
 * 开关：settings.memory.autoLearn（默认开）。调用方已 try/catch，这里再兜一层。
 */
function recordRunLesson(
  session: { skill_name?: string; goal?: string },
  outcome: { aborted: boolean; failed: boolean; errorMessage?: string | null },
): void {
  if (getSetting('memory.autoLearn') === false) return;
  const scopeKey = (session.skill_name ?? '').slice(0, 120);
  const goal = (session.goal ?? '').trim();
  if (outcome.failed) {
    const reason = (outcome.errorMessage ?? '').trim().slice(0, 400);
    if (!reason) return; // 没有原因原文就不硬编——不造事实
    remember({
      content: `任务「${goal.slice(0, 80) || '(无目标)'}」失败：${reason}。同类任务执行前先按这条自查规避。`,
      kind: 'lesson',
      scope_key: scopeKey,
      source: 'run_failed',
      source_ref: '',
      score: 0.85,
    });
  } else if (outcome.aborted) {
    remember({
      content: `用户中断了「${goal.slice(0, 80) || '(无目标)'}」这类任务。再次执行同类目标时先与用户确认范围与方式，再动手。`,
      kind: 'preference',
      scope_key: scopeKey,
      source: 'user_abort',
      source_ref: '',
      score: 0.9,
    });
  }
}


function coerceArtifactType(raw: unknown): ArtifactType | null {
  if (typeof raw !== 'string') return null;
  return (VALID_ARTIFACT_TYPES as ReadonlyArray<string>).includes(raw)
    ? (raw as ArtifactType)
    : null;
}

// ── 多产物登记（2026-11-07）— blueprint 事件捕获器 ──────────────────────────
// 此前「last blueprint wins」：一个 run 只留最后一件产物（runs 表三字段），
// 其余产物在产物面板/项目产物里全部不可见。现在每件 blueprint 产物都进
// captured 列表（storage/runs.ts run_artifacts 表持久化）。
// 导出为纯函数供 __tests__/run-artifacts.test.ts 直测。

/** 一件捕获到的产物（= storage/runs.ts RunArtifact 的运行时形状）。 */
export interface CapturedArtifact {
  type: ArtifactType | null;
  filename: string | null;
  url: string | null;
}

/**
 * captureBlueprintArtifact — 把一条 blueprint 事件的载荷登记进 captured。
 *
 * - 载荷全空（type/filename/url 均无）→ 不登记，返回 null。
 * - 身份 = filename ?? url：同名产物的重复事件**覆盖**既有行（后值补齐
 *   缺字段，如先 filename 后带 url 的补发），不产生重复行。
 * - 返回本次的条目（无论新增还是覆盖）——调用方用它维持「最后一件」的
 *   主产物语义（critique/回执沿用旧行为）。
 */
export function captureBlueprintArtifact(
  captured: CapturedArtifact[],
  data: { artifact_type?: unknown; filename?: unknown; artifact_url?: unknown },
): CapturedArtifact | null {
  const type = coerceArtifactType(data.artifact_type);
  const filename = typeof data.filename === 'string' && data.filename ? data.filename : null;
  const url = typeof data.artifact_url === 'string' && data.artifact_url ? data.artifact_url : null;
  if (!type && !filename && !url) return null;
  const entry: CapturedArtifact = { type, filename, url };
  const key = filename ?? url;
  const idx = key ? captured.findIndex((a) => (a.filename ?? a.url) === key) : -1;
  if (idx >= 0) captured[idx] = entry;
  else captured.push(entry);
  return entry;
}

// ── GET /api/run-sessions — M3 三栏融合 ──────────────────────────────────────
// 轻量列表：右栏「工作区」tab 按 conversation_id 或 group_id（B1 互链换算）
// 查群关联的 run。status 来自 runBus（内存）；重启后无活跃 run 回落 'idle'。
router.get('/', (req: Request, res: Response) => {
  let conv =
    typeof req.query.conversation_id === 'string' && req.query.conversation_id
      ? req.query.conversation_id
      : undefined;
  // B1 — 群 id 一步到位：服务端经互链换算成 conversation_id。无链接 = 无 run。
  if (!conv && typeof req.query.group_id === 'string' && req.query.group_id) {
    const linked = getConversationByGroup(req.query.group_id);
    if (!linked) {
      res.json({ sessions: [] });
      return;
    }
    conv = linked.conversation_id;
  }
  type RunListItem = {
    session_id: string;
    goal: string;
    conversation_id: string | null;
    group_id: string | null;
    status: string;
    created_at: number;
    updated_at: number;
  };
  const byId = new Map<string, RunListItem>();
  // 阶段0 — 指定会话时持久索引为主源（TTL 回收后的历史 run 仍可见）
  if (conv) {
    try {
      for (const r of listRunIndexByConversation(conv)) {
        byId.set(r.session_id, {
          session_id: r.session_id,
          goal: r.goal,
          conversation_id: r.conversation_id,
          group_id: r.conversation_id
            ? (getConversation(r.conversation_id)?.group_id ?? null)
            : null,
          status: runBus.get(r.session_id)?.status ?? r.status ?? 'idle',
          created_at: r.created_at,
          updated_at: r.updated_at,
        });
      }
    } catch { /* sqlite 不可用回落内存 */ }
  }
  for (const [id, s] of sessionStore.entries()) {
    if (conv && s.conversation_id !== conv) continue;
    byId.set(id, {
      session_id: id,
      goal: s.goal ?? '',
      conversation_id: s.conversation_id ?? null,
      // F4 — 互链群 id 带出，前端可从 run 一步跳回聊天会话
      group_id: s.conversation_id
        ? (getConversation(s.conversation_id)?.group_id ?? null)
        : null,
      status: runBus.get(id)?.status ?? 'idle',
      created_at: s.created_at,
      updated_at: s.updated_at ?? s.created_at,
    });
  }
  const sessions = [...byId.values()];
  sessions.sort((a, b) => b.updated_at - a.updated_at);
  res.json({ sessions: sessions.slice(0, 50) });
});

// ── POST /api/run-sessions ────────────────────────────────────────────────────

/**
 * 2026-11-04 对话完善 — settings/byok 里第一个 enabled+key 的 provider id
 * （与 groups-chat.pickDefaultExecutor 同一策略）。无可用 BYOK / settings
 * 异常 → null（调用方维持 'anthropic' 历史默认）。NeverThrow。
 */
function pickDefaultProviderId(): ProviderId | null {
  try {
    const byok = getSetting('byok') as
      | { providers?: Record<string, { apiKey?: string; enabled?: boolean }> }
      | undefined;
    for (const [id, p] of Object.entries(byok?.providers ?? {})) {
      if (p?.enabled && typeof p.apiKey === 'string' && p.apiKey.trim() && isProviderId(id)) {
        return id;
      }
    }
  } catch {
    /* settings 不可用 → 历史默认 */
  }
  return null;
}

router.post('/', (req: Request, res: Response) => {
  const {
    goal,
    skill_name,
    output_hint,
    workspace_id,
    mode,
    design_system_id,
    // Story 15.9 — generation overrides; each is validated via coerce* below.
    model,
    max_tokens,
    temperature,
    // Story 15.13 — multi-layer prompt assembly inputs (all optional).
    project_meta,
    side_files,
    layer_toggles,
    // 2026-11-06 — 对话框知识库：记忆库 pack id 列表（coercePackIds 清洗）。
    knowledge_pack_ids,
    // Story 15.19 v2 — optional executor selector.
    executor,
    // Story 15.18 — optional provider selector.
    provider,
    // 2026-05-30 — CLI 工作目录(绝对路径)。仅 cli:/acp:/mcp: 用得上。
    cwd,
    // Story 15.29 — optional conversation linkage. Server validates / auto-creates.
    conversation_id,
    // B1 统一会话内核 — 可选聊天群挂靠：按群复用/创建互链 conversation。
    group_id,
    // GAP A3 回写（2026-08-23）— 自动创建 conversation 时的目标项目；
    // 缺省落 'default' 项目（现状不变）。
    project_id,
    // Story 15.14 follow-up — body-level auto_critique override (UI toggle).
    auto_critique,
  } = req.body as {
    goal?: string;
    skill_name?: string;
    output_hint?: string;
    workspace_id?: string;
    mode?: string;
    design_system_id?: string;
    model?: unknown;
    max_tokens?: unknown;
    temperature?: unknown;
    project_meta?: unknown;
    side_files?: unknown;
    layer_toggles?: unknown;
    knowledge_pack_ids?: unknown;
    executor?: unknown;
    provider?: unknown;
    cwd?: unknown;
    conversation_id?: unknown;
    group_id?: unknown;
    project_id?: unknown;
    auto_critique?: unknown;
  };

  if (!goal || typeof goal !== 'string' || goal.trim().length === 0) {
    res.status(400).json({ error: 'goal is required and must be a non-empty string' });
    return;
  }

  // Inline `@skill:<id>` token support (Story 16.x). Lets the user write
  //   `@skill:bmad-method 帮我做电商系统`
  // and have the server pull bmad-method out of the user library without the
  // client needing to manage state. The explicit `skill_name` body param wins
  // when both are present.
  let goal_text = goal.trim();
  let inline_skill_token: string | undefined;

  // W2 (Lane B) — Claude Code v2.1.88-style `/<id>:<cmd>` slash. Parsed BEFORE
  // `@skill:<id>` so that when both are present the slash wins (it's the more
  // specific target — a particular command inside a plugin). Skill-loader
  // registers commands/X.md as keys of the form `<id>:<X>` so SKILLS lookup
  // downstream just works.
  //
  // /review hardenings (2026-05-22):
  //   - Anchor at start-of-string or whitespace (NOT mid-URL) so a goal like
  //     "https://api.example.com/foo:bar" doesn't get parsed as a slash cmd.
  //     (finding A4)
  //   - Do NOT lowercase the captured token. canonical-id.ts preserves
  //     case, and SKILLS keys are case-sensitive (`BMAD-METHOD:prfaq` !=
  //     `bmad-method:prfaq`). (finding #1 / A3)
  const slashCmdRe = /(?:^|\s)\/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}):([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})(?=\s|$)/;
  const sm = goal_text.match(slashCmdRe);
  if (sm) {
    inline_skill_token = `${sm[1]}:${sm[2]}`;
    goal_text = goal_text.replace(slashCmdRe, '').replace(/\s{2,}/g, ' ').trim();
    if (!goal_text) goal_text = '执行该 skill 命令。';
  } else {
    // @<id> / @skill:<id> / @skill <id> — canonical parser. Case-sensitive
    // capture (BMAD-METHOD ≠ bmad-method) per canonical-id.ts. Defends
    // against `user@gmail.com` accidentally matching as `@gmail`. The same
    // module backs the frontend SkillDropdown so UI and route agree on
    // exactly which tokens are recognised.
    const parsed = parseSkillToken(goal_text);
    if (parsed.skill_id) {
      inline_skill_token = parsed.skill_id;
      goal_text = parsed.remaining;
      if (!goal_text) {
        // user typed only "@bmad" / "@skill:foo" without a goal — @skill 的本意是
        // "用这个 skill 组装 agent/team",所以默认目标按组装措辞(而非泛化"开始任务"),
        // 让 LLM 编排器明确去读该 skill 的声明并组队。
        goal_text = '用这个 skill 帮我组建 agent / team。';
      }
    }
  }

  // Story 15.18 — multi-provider BYOK. Provider → key resolution (header →
  // settings/byok → env) lives in transport/byokKey.ts (shared with the Node
  // chat gateway groups-chat.ts). The `anthropic_key` field stays for
  // back-compat (15.7 / 15.19 test paths).

  // Story 15.18 — validate provider; reject unknown ids loudly with 400.
  // 2026-11-04 对话完善：客户端未显式指定 provider 时，不再默认 anthropic
  // （api.anthropic.com 国内不可达 + settings 里的 anthropic key 无效 → 每条
  // 消息 Connection error，「设置检测通过却无法对话」单聊根因）。改读
  // settings/byok：第一个 enabled+key 的 provider 优先（与群聊网关
  // groups-chat.pickDefaultExecutor 同一策略），无可用 BYOK → 维持 anthropic。
  let validated_provider: ProviderId = 'anthropic';
  if (typeof provider === 'string' && provider.trim().length > 0) {
    const candidate = provider.trim();
    if (!isProviderId(candidate)) {
      res.status(400).json({
        error: {
          code: 'INVALID_PROVIDER',
          message: `Unknown provider: "${candidate}". Allowed: ${PROVIDER_IDS.join(', ')}`,
        },
      });
      return;
    }
    validated_provider = candidate;
  } else {
    const fallback = pickDefaultProviderId();
    if (fallback) validated_provider = fallback;
  }

  // Pick the API key for the chosen provider, in priority order:
  //   1. Per-request HTTP header (e.g. X-Zhipu-Key)        — overrides everything
  //   2. ByokSection's saved key in settings/byok          — the "UI-configured" path
  //   3. Per-provider env var (e.g. ZHIPU_API_KEY)         — last-resort default
  //
  // (2) is the critical fix for the "前端做了，后端没接上" class of bugs:
  // ByokSection writes apiKey to byok-config.json via PUT /api/settings/byok,
  // but until now run-sessions only read header + env, so configuring a
  // Zhipu/DeepSeek/OpenAI key in Settings did absolutely nothing at runtime.
  // 2026-05-29 — key 解析提炼到 transport/byokKey.ts（与 Node chat 网关
  // groups-chat.ts 共用同一策略：header → settings/byok → env）。
  const provider_api_key = resolveProviderKey(validated_provider, req.headers);

  // Back-compat field (still consumed by some legacy code paths). For the
  // anthropic provider it MUST mirror provider_api_key so the runner sees
  // the BYOK key whether it reads `anthropic_key` or `api_key`.
  const anthropic_key =
    validated_provider === 'anthropic'
      ? provider_api_key
      : (req.headers['x-anthropic-key'] as string | undefined) ||
        process.env.ANTHROPIC_API_KEY;

  // Story 15.5: validate design_system_id; unknown ids fall back to 'none' so
  // a stale client can never crash the run.
  const ds_id =
    typeof design_system_id === 'string' && DESIGN_SYSTEMS[design_system_id]
      ? design_system_id
      : 'none';

  // 2026-05-10 review M2 (15.5): explicit skill_name whitelist check. Unknown
  // skill_name 时显式回退到 'agent-team-blueprint'（默认）— 与 DS 注入链路保持
  // 一致，避免 SKILLS[skill_name] = undefined 时 DS injection 静默丢失。
  // OpenDesign 模式：skill resolve 时显式校验 + 默认 fallback。
  //
  // Story 16.x — explicit body param wins; inline @skill: token is the fallback.
  // Both must still resolve against the live SKILLS registry (populated by
  // hardcoded + FS + user-ingested skills via reloadSkills()).
  const skill_candidate =
    typeof skill_name === 'string' && skill_name
      ? skill_name
      : inline_skill_token;
  const validated_skill =
    skill_candidate && SKILLS[skill_candidate]
      ? skill_candidate
      : 'agent-team-blueprint';
  // 2026-06-01 — 用户是否显式指定了 skill(@skill:<id> 或显式 skill_name)。
  // 用于 assembler 的"组装意图"路由:显式时,Branch 2 兜底不走对话而走组装指令。
  const explicit_skill = !!skill_candidate;

  // Story 15.9 — validate and store generation overrides. Each helper returns
  // `undefined` for invalid input so the assembler falls back to env / default.
  // XUEBAN_DEFAULT_MODEL env, when set, will still win at the assembler
  // boundary (see assembler.ts) — but we do NOT silently drop the env-locked
  // value here so the front-end can be honest about what it submitted.
  const validated_model = coerceModel(model);
  const validated_max_tokens = coerceMaxTokens(max_tokens);
  const validated_temperature = coerceTemperature(temperature);

  // Story 15.13 — sanitize multi-layer prompt assembly inputs.
  const validated_project_meta = coerceProjectMeta(project_meta);
  const validated_side_files =
    typeof side_files === 'string' && side_files.trim().length > 0 ? side_files : undefined;
  const validated_layer_toggles = coerceLayerToggles(layer_toggles);
  // 对话框知识库（2026-11-06）— 记忆库 pack id 列表；非法项丢弃，空 = 不挂知识。
  const validated_knowledge_pack_ids = coercePackIds(knowledge_pack_ids);

  // Story 15.19 v2 — coerce executor. We accept any non-empty string and let
  // the dispatcher emit structured errors for unknown values; this keeps the
  // CLI registry data-driven and avoids hard-coding the list in two places.
  const validated_executor =
    typeof executor === 'string' && executor.trim().length > 0 ? executor.trim() : undefined;
  // CLI 工作目录:只接受非空绝对路径(Win 盘符 C:\… 或 POSIX /…)。相对路径丢弃。
  const validated_cwd =
    typeof cwd === 'string' && cwd.trim() && /^([a-zA-Z]:[\\/]|[\\/])/.test(cwd.trim())
      ? cwd.trim()
      : undefined;

  // ── Story 15.29 — conversation_id validate / auto-create ─────────────────
  // Three branches:
  //   (a) string + exists → use it
  //   (b) string + missing → 400 CONVERSATION_NOT_FOUND
  //   (c) wrong type → 400 INVALID_CONVERSATION_ID
  //   (d) undefined → auto-create anonymous conversation under 'default' project
  // GAP A3 回写（2026-08-23）— 自动创建（group_id 分支 / 匿名分支）可用 body
  // .project_id 指定目标项目；给了但不存在的项目 → 400 PROJECT_NOT_FOUND。
  let autoCreateProjectId: string | undefined;
  if (project_id !== undefined) {
    if (typeof project_id === 'string' && project_id.trim().length > 0) {
      if (!getProject(project_id.trim())) {
        res.status(400).json({
          error: {
            code: 'PROJECT_NOT_FOUND',
            message: `project ${project_id} not found`,
          },
        });
        return;
      }
      autoCreateProjectId = project_id.trim();
    } else {
      res.status(400).json({
        error: {
          code: 'INVALID_PROJECT_ID',
          message: 'project_id must be a non-empty string when provided',
        },
      });
      return;
    }
  }
  const autoCreateProject = () =>
    autoCreateProjectId
      ? getProject(autoCreateProjectId)!
      : getOrCreateProject('default', 'Default Project');
  let validated_conversation_id: string;
  if (typeof conversation_id === 'string') {
    if (!getConversation(conversation_id)) {
      res.status(400).json({
        error: {
          code: 'CONVERSATION_NOT_FOUND',
          message: `conversation ${conversation_id} not found`,
        },
      });
      return;
    }
    validated_conversation_id = conversation_id;
    // B1 统一会话内核 — 调用方同时给了群 id 时补链（conversation 还没挂群才写，
    // 不覆盖已有链接）。
    if (typeof group_id === 'string' && group_id) {
      const conv = getConversation(conversation_id);
      if (conv && !conv.group_id) setConversationGroup(conversation_id, group_id);
    }
  } else if (conversation_id !== undefined) {
    res.status(400).json({
      error: {
        code: 'INVALID_CONVERSATION_ID',
        message: 'conversation_id must be a string when provided',
      },
    });
    return;
  } else if (typeof group_id === 'string' && group_id) {
    // B1 统一会话内核 — 按群发起 run：复用该群已互链的 conversation，
    // 没有则创建并写入 group_id。群聊里发任务从此天然归属同一条会话。
    const existing = getConversationByGroup(group_id);
    if (existing) {
      validated_conversation_id = existing.conversation_id;
    } else {
      const project = autoCreateProject();
      const conv = createConversation(project.project_id, undefined, group_id);
      validated_conversation_id = conv.conversation_id;
    }
  } else {
    const project = autoCreateProject();
    const conv = createConversation(project.project_id);
    validated_conversation_id = conv.conversation_id;
  }

  const session_id = uuidv4();
  sessionStore.set(session_id, {
    goal: goal_text,
    skill_name: validated_skill,
    explicit_skill,
    skill_requested: skill_candidate || undefined,
    output_hint,
    workspace_id,
    mode,
    anthropic_key,
    provider: validated_provider,
    api_key: provider_api_key,
    design_system_id: ds_id,
    model: validated_model,
    max_tokens: validated_max_tokens,
    temperature: validated_temperature,
    project_meta: validated_project_meta ?? null,
    side_files: validated_side_files,
    layer_toggles: validated_layer_toggles,
    knowledge_pack_ids:
      validated_knowledge_pack_ids.length > 0 ? validated_knowledge_pack_ids : undefined,
    executor: validated_executor,
    cwd: validated_cwd,
    conversation_id: validated_conversation_id,
    auto_critique: typeof auto_critique === 'boolean' ? auto_critique : undefined,
    created_at: Date.now(),
    updated_at: Date.now(),
  });

  // 后端合一·阶段0 — 持久 run 索引（修：历史随 sessionStore TTL 消失）
  try {
    upsertRunIndex({
      session_id,
      conversation_id: validated_conversation_id,
      goal: goal_text,
    });
  } catch { /* sqlite 不可用不阻塞创建 */ }

  // Clean up sessions idle longer than the TTL (last-activity based, not
  // created-at — see SESSION_TTL_MS). An actively-viewed session is touched on
  // every /stream attach, so revisits never trip this.
  const ttlCutoff = Date.now() - SESSION_TTL_MS;
  for (const [id, sess] of sessionStore.entries()) {
    if (sessionLastActivity(sess) < ttlCutoff) sessionStore.delete(id);
  }

  console.log(
    `[run-sessions] Created session ${session_id} skill=${sessionStore.get(session_id)?.skill_name} ds=${ds_id}` +
      ` provider=${validated_provider} hasKey=${provider_api_key ? 'yes' : 'no'}` +
      ` model=${validated_model ?? '(default)'} max_tokens=${validated_max_tokens ?? '(default)'}` +
      ` temperature=${validated_temperature ?? '(default)'} goal="${goal.slice(0, 60)}"`,
  );

  res.status(201).json({
    session_id,
    stream_url: `/api/run-sessions/${session_id}/stream`,
    // Story 15.29 — always echo back; client may have omitted (auto-created
    // anonymous conv). Front-end stores this so the next visit auto-selects it.
    conversation_id: validated_conversation_id,
  });
});

// ── POST /api/run-sessions/:id/messages ──────────────────────────────────────
//
// Follow-up turn within an existing run-session's conversation. Each follow-up
// becomes a NEW run session that **inherits all settings** from the source
// (skill_name / DS / provider / BYOK key / model overrides / conversation_id),
// only the `goal` is replaced with the new user message.
//
// Story 15.29 prompt-assembly auto-pulls conversation history via the
// `conversation_id` layer, so the LLM sees prior turns as context — no
// extra wiring needed here.
//
// Returns the SAME envelope shape as POST /api/run-sessions so the front-end
// can just open a new EventSource on the returned stream_url.
router.post('/:id/messages', (req: Request, res: Response) => {
  const sourceId = req.params.id;
  const source = sessionStore.get(sourceId);
  if (!source) {
    res.status(404).json({
      error: { code: 'SESSION_NOT_FOUND', message: `Run session ${sourceId} not found` },
    });
    return;
  }

  const body = (req.body ?? {}) as {
    content?: unknown;
    goal?: unknown;
    // 2026-05-16 — follow-ups (incl. resend) may override the picker selection
    // so users can fix a 401 by switching provider/model in the input bar.
    // Undefined values inherit from source; explicit values replace.
    model?: unknown;
    provider?: unknown;
    api_key?: unknown;
    anthropic_key?: unknown;
    executor?: unknown;
    cwd?: unknown;
    knowledge_pack_ids?: unknown;
  };
  // Accept either `content` (chat-style payload that the RunSessionPage send box
  // uses) or `goal` (mirrors POST /api/run-sessions). content wins when both
  // are present so the chat UX is unambiguous.
  const raw = typeof body.content === 'string' ? body.content : typeof body.goal === 'string' ? body.goal : '';
  const content = raw.trim();
  if (!content) {
    res.status(400).json({
      error: { code: 'EMPTY_CONTENT', message: 'content (or goal) must be a non-empty string' },
    });
    return;
  }

  const new_session_id = uuidv4();
  const overrides: Partial<typeof source> = {};
  if (typeof body.model === 'string' && body.model.length > 0) overrides.model = body.model;
  if (typeof body.provider === 'string' && body.provider.length > 0) overrides.provider = body.provider as typeof source.provider;
  if (typeof body.api_key === 'string') overrides.api_key = body.api_key;
  if (typeof body.anthropic_key === 'string') overrides.anthropic_key = body.anthropic_key;
  if (typeof body.executor === 'string') overrides.executor = body.executor;
  // CLI 工作目录:follow-up 可改;仅接受绝对路径(否则保留 source 继承值)。
  if (typeof body.cwd === 'string' && /^([a-zA-Z]:[\\/]|[\\/])/.test(body.cwd.trim())) {
    overrides.cwd = body.cwd.trim();
  }
  // 对话框知识库（2026-11-06）— 显式给出数组即整体替换（空数组 = 本轮取消挂载）；
  // 未给继承 source 会话的选择。
  if (Array.isArray(body.knowledge_pack_ids)) {
    overrides.knowledge_pack_ids = coercePackIds(body.knowledge_pack_ids);
  }

  sessionStore.set(new_session_id, {
    ...source,
    ...overrides,
    goal: content,
    // Reset created_at so the 1h-cleanup window restarts for the follow-up turn.
    created_at: Date.now(),
  });

  console.log(
    `[run-sessions] Follow-up message: source=${sourceId} → new=${new_session_id} ` +
      `conversation=${source.conversation_id ?? '(none)'} content="${content.slice(0, 60)}"` +
      ` overrides=${Object.keys(overrides).join(',') || '(none)'}`,
  );

  res.status(201).json({
    session_id: new_session_id,
    stream_url: `/api/run-sessions/${new_session_id}/stream`,
    conversation_id: source.conversation_id,
  });
});

/**
 * O4 — read-only SSE replay of a run whose owning session is gone.
 *
 * Opens an SSE response and attaches a sink to the run-event-bus. The bus
 * replays every buffered event past the resume cursor (?after / Last-Event-ID),
 * then — because the run is terminal (or a hydrated/synthetic-terminal run) —
 * calls sink.end() and the response closes. This deliberately reuses NONE of the
 * live handler's pipeline machinery: no claimStart, no projector, no assembler,
 * no session reads. The front-end sees the full historical timeline plus, for a
 * restart-interrupted run, the synthetic "已中断" terminal frame the bus added —
 * never a hard 404. Costs zero tokens.
 */
function streamReadOnlyReplay(req: Request, res: Response, id: string): void {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.flushHeaders();

  // Heartbeat so an idle proxy doesn't cut the connection mid-replay (a large
  // buffered timeline can take a beat to flush). Cleared on end / close.
  const HEARTBEAT_MS = 15_000;
  const heartbeatTimer = setInterval(() => {
    if (res.writableEnded) {
      clearInterval(heartbeatTimer);
      return;
    }
    try {
      res.write(': heartbeat\n\n');
    } catch {
      clearInterval(heartbeatTimer);
    }
  }, HEARTBEAT_MS);

  const sink: RunEventSink = {
    send: (rec) => {
      if (res.writableEnded) return;
      const line = `id: ${rec.id}\nevent: ${rec.event}\ndata: ${JSON.stringify(rec.data)}\n\n`;
      res.write(line);
      if (typeof (res as unknown as { flush?: () => void }).flush === 'function') {
        (res as unknown as { flush: () => void }).flush();
      }
    },
    end: () => {
      clearInterval(heartbeatTimer);
      if (!res.writableEnded) res.end();
    },
  };

  // Connected handshake BEFORE attach: a terminal run makes attach() replay then
  // synchronously call sink.end() (res.end()); any write after that throws
  // ERR_STREAM_WRITE_AFTER_END. Send the comment frame while the response is open.
  if (!res.writableEnded) res.write(': connected\n\n');

  const afterRaw = req.query.after ?? req.headers['last-event-id'];
  const after = Number(Array.isArray(afterRaw) ? afterRaw[0] : afterRaw) || 0;
  const detach = runBus.attach(id, after, sink);

  req.on('close', () => {
    clearInterval(heartbeatTimer);
    detach();
  });

  console.log(`[run-sessions] read-only replay for session ${id} (after=${after}) — session gone, bus serves history`);
}

// ── GET /api/run-sessions/:id/stream ─────────────────────────────────────────

router.get('/:id/stream', async (req: Request, res: Response) => {
  const { id } = req.params;
  const session = sessionStore.get(id);

  // O4 — graceful degradation. The session may be gone (1h TTL fired, or the
  // user reopened an old run after a restart), but the run-event-bus may still
  // hold a replayable timeline (live OR hydrated from disk). Before deciding,
  // try a LAZY disk recovery so a snapshot that was never hydrated at startup
  // (created post-boot, then GC'd by TTL while its file lingered) can still be
  // replayed read-only instead of hard-404'd.
  if (!session) {
    const recovered = runBus.hydrateOne(id) ?? runBus.get(id);
    const disp = resolveStreamDisposition({ hasSession: false, run: recovered });
    if (disp.kind === 'gone') {
      res
        .status(404)
        .json({ error: `会话 ${id} 已过期且无可重放的历史 (session expired, no replayable run)` });
      return;
    }
    // disp.kind === 'replay' — read-only timeline replay. We do NOT touchSession
    // (there is none), NOT claimStart, NOT run any pipeline. We open SSE, attach
    // a sink to the bus (which replays buffered events from the resume cursor and
    // then ends — the run is terminal/hydrated), and return. Zero tokens spent.
    streamReadOnlyReplay(req, res, id);
    return;
  }

  // Viewing a session counts as activity — refresh its TTL so a run the user
  // keeps open / comes back to never expires mid-use (2026-06-01).
  touchSession(id);

  // Set SSE headers — must be set before any write
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');  // Disable nginx buffering
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.flushHeaders();

  // T3-1: this connection is a VIEW onto a persistent run. The run owns the
  // AbortController (so a cancel persists across reconnects) and the buffered
  // event log; we attach a sink that writes frames to THIS response.
  const run = runBus.ensure(id);
  const abortController = run.abort; // alias — existing signal refs keep working

  // S0.1 (intent-workflow-design-v1 §4.0) — periodic SSE heartbeat. Without
  // this, idle Vite/nginx proxies cut the connection after ~30s of silence
  // (Claude "thinking" + long YAML emit easily exceed this), the front-end
  // hits the "已达最大重试次数" alert and the run looks broken. We write a
  // comment frame (`: heartbeat\n\n`) every 15s — comment frames per SSE
  // spec are ignored by EventSource but reset proxy timers. Per-connection.
  const HEARTBEAT_MS = 15_000;
  const heartbeatTimer = setInterval(() => {
    if (res.writableEnded) {
      clearInterval(heartbeatTimer);
      return;
    }
    try {
      res.write(': heartbeat\n\n');
    } catch {
      clearInterval(heartbeatTimer);
    }
  }, HEARTBEAT_MS);

  // SSE sink: writes one buffered/live event frame to THIS response. The `id:`
  // field lets the browser resume via Last-Event-ID after a drop. Error-code
  // classification happens at emit time (see sendEvent), so replayed frames
  // already carry the normalized 6-bucket code.
  const sink: RunEventSink = {
    send: (rec) => {
      if (res.writableEnded) return;
      const line = `id: ${rec.id}\nevent: ${rec.event}\ndata: ${JSON.stringify(rec.data)}\n\n`;
      res.write(line);
      if (typeof (res as unknown as { flush?: () => void }).flush === 'function') {
        (res as unknown as { flush: () => void }).flush();
      }
    },
    end: () => {
      clearInterval(heartbeatTimer);
      if (!res.writableEnded) res.end();
    },
  };

  // Emit → run-event bus (buffer + fan out to all attached sinks). For `error`
  // events we normalize the code to the 6-bucket UI taxonomy (auth / rate_limit
  // / context_too_long / network / server / unknown) BEFORE buffering, so a
  // reconnect's replayed frames carry the normalized code. Original fine-grained
  // server code is preserved under `data.server_code`.
  const sendEvent = (event: string, data: unknown) => {
    let payload: unknown = data;
    if (event === 'error' && data && typeof data === 'object') {
      const d = data as Record<string, unknown>;
      const rawCode = typeof d.code === 'string' ? d.code : undefined;
      const message = typeof d.message === 'string' ? d.message : undefined;
      const stderrTail = typeof d.stderr_tail === 'string' ? d.stderr_tail : undefined;
      const status = typeof d.status === 'number' ? d.status : undefined;
      const uiCode = classifyErrorCode({
        code: rawCode,
        message,
        stderr_tail: stderrTail,
        status,
      });
      payload = {
        ...d,
        code: uiCode,
        ...(rawCode && rawCode !== uiCode ? { server_code: rawCode } : {}),
      };
    }
    runBus.emit(id, event, payload);
  };

  // Confirm connection (comment frame, ignored by EventSource). MUST be sent
  // BEFORE attach(): a reconnect to an already-terminal run makes attach()
  // replay the buffer then synchronously call sink.end() (res.end()). Any write
  // after that throws ERR_STREAM_WRITE_AFTER_END and crashes the process — so
  // the connected frame goes out first, while the response is still open.
  if (!res.writableEnded) res.write(': connected\n\n');

  // Resume cursor: ?after=N (our fetch client) or Last-Event-ID header (native
  // EventSource reconnect). attach() replays the buffered events with id > after,
  // then registers this sink for live events (or ends immediately if terminal).
  const afterRaw = req.query.after ?? req.headers['last-event-id'];
  const after = Number(Array.isArray(afterRaw) ? afterRaw[0] : afterRaw) || 0;
  const detach = runBus.attach(id, after, sink);

  // Client disconnect: detach this view ONLY. We deliberately do NOT abort the
  // run — it stays alive so the client can reconnect and resume from its cursor
  // (OpenDesign semantics; root-cause fix for "reconnect re-runs the LLM").
  // Explicit cancellation goes through POST /:id/abort → runBus.cancel.
  req.on('close', () => {
    clearInterval(heartbeatTimer);
    detach();
  });

  console.log(
    `[run-sessions] SSE view attached for session ${id} (after=${after}) skill=${session.skill_name}`,
  );

  // T3-1: run the pipeline EXACTLY ONCE. The first connection claims the start
  // and drives the LLM/assembler, emitting into the bus; reconnects get
  // claimStart()===false here and fall straight through to the end of the
  // handler — they are already attached above and stream the buffered + live
  // events via their sink. (Body kept at its original indentation to keep this
  // a surgical diff: it is the same code, now guarded + routed through the bus.)
  if (!runBus.claimStart(id)) {
    return;
  }

  // 2026-08-19 迭代环 watcher：run 活跃期间把 Python loops 投影差分上流为
  // 命名 wire 事件 `loop`（EVENT_NAMING §1；前端 useRunSession → 右栏「过程」
  // tab 实时渲染）。Python 不可达/无 run 记录时 watcher 静默，不影响执行。
  const stopLoopWatcher = startLoopWatcher(id, (payload) => {
    runBus.emit(id, 'loop', payload);
  });

  // Polynoia-parity git evidence anchor — HEAD at run start. Captured before
  // the pipeline executes so the finally block can diff exactly what the
  // agents committed during THIS run (startHead..HEAD). Non-repo / unborn
  // HEAD / no cwd → null → no commit/diff cards (never fabricated).
  let gitStartHead: string | null = null;
  if (session.cwd) {
    try {
      gitStartHead = await resolveGitHead(session.cwd);
    } catch (e) {
      console.warn(`[run-sessions] git head probe failed for session ${id}:`, e);
    }
  }

  // S1.2 — TS-side classify frame. Synchronous keyword classifier; runs in ~µs
  // so we can emit it right after the connection handshake, parallel to the
  // LLM warming up. LLM will later emit its own <sf:classify .../> via the
  // parser (which doesn't set a `source` attr — front-end treats unset as
  // 'llm'). Two frames coexist so the UI can surface a divergence warning.
  // 2026-06-18 — 单 agent 裁决（一处计算，classify 帧 + roster 锁两处复用）。
  // mode='single' 是既有语义（原 StartPage 单/团队开关；个人 start composer 的
  // Auto 现默认传 'single'）。逃生门：显式 @skill 或 goal 含团队词 → 仍走团队。
  // 团队态（mode≠'single'）完全不受影响。详 [[project_assembly_dimensionalization]]。
  const explicitSingleIntent = detectExplicitSingleAgent(session.goal);
  const wantSingleAgent =
    explicitSingleIntent.single ||
    (session.mode === 'single' && !session.explicit_skill && !hasExplicitTeamIntent(session.goal));
  // 2026-06-23 — 普通对话直通路（investigate 根因修复）。判定抽到 intent-router 的
  // 纯函数 shouldUsePlainConversation（可单测）：个人态默认 mode='single' 且既不是显式
  // @skill、又不是显式「建 1 个 agent」、又无团队词 → 这是一句寒暄/单步问答/写代码，
  // 应由普通对话/编码 agent 直接回答，而不是套「团队组装指挥官」人格走组装三相位。
  const wantPlainConversation = shouldUsePlainConversation({
    mode: session.mode,
    explicit_skill: session.explicit_skill,
    goal: session.goal,
  });

  try {
    const tsClassify = classifyTS(session.goal);
    sendEvent('classify', {
      // 普通对话直通路：单人直接回答，统一标 'answer'（否则 classifyTS 把"写段代码"
      // 之类判成 task→workflow，UI 误显"工作流"）。
      output_type: wantPlainConversation
        ? 'answer'
        : tsClassify.kind === 'task'
          ? 'workflow'
          : tsClassify.kind,
      mode: wantSingleAgent ? 'single' : 'team',
      confidence: tsClassify.confidence,
      complexity: 2,
      source: 'ts',
      reasons: tsClassify.reasons,
    });
  } catch (e) {
    // Never let a classifier bug kill the run — log + skip.
    console.error(`[run-sessions] classifyTS failed for session ${id}:`, e);
  }

  // S2.3 — per-step accumulator. Parser brackets each step with `assemble`
  // running/done; node/edge/blueprint/classify events arriving between them
  // belong to the currently-open step. On `done` we package a StepArtifact
  // and persist via stepStoreSingleton.put() + emit `step-artifact`.
  interface OpenStep {
    step_index: number;
    step_name: string;
    output_kind: OutputKind;
    started_at: string;
    nodes: unknown[];
    edges: unknown[];
    yaml?: { filename: string; content: string };
    classify?: unknown;
  }
  const openSteps = new Map<number, OpenStep>();
  let currentStepIndex: number | null = null;

  // ── Tool-call visualization (clawsgo parity) ─────────────────────────────
  // The agent runtime surfaces FIRST-CLASS `tool-use` / `tool-result` SSE frames
  // (assembler.ts pipeChunksToSse — sourced from typed TurnChunks, never
  // re-parsed from text). They drive the timeline tool card, but the legacy
  // left-pane <StepList> never saw them. We project each tool invocation into a
  // normalized `tool-call` frame via the module-level `createToolCallProjector`
  // (extracted + exported so it is unit-tested in isolation — see
  // __tests__/emit-tool-call.test.ts). All projected fields are REAL data
  // derived from the runtime's own tool-use / tool-result payload.
  const toolCallProjector = createToolCallProjector({ idSeed: id });

  // Story 15.8 — capture run outcome for persistence after generator drains.
  // 多产物登记（2026-11-07）：capturedArtifacts 登记本 run 的**每一件** blueprint
  // 产物（持久化到 run_artifacts 表）；artifactInfo 保留「最后一件 = 主产物」
  // 语义，critique / saveRun 三字段 / 回执摘要的单件行为不变。
  const capturedArtifacts: CapturedArtifact[] = [];
  let artifactInfo: {
    type: ArtifactType | null;
    filename: string | null;
    url: string | null;
  } = { type: null, filename: null, url: null };
  let sawError = false;
  let sawComplete = false;
  // Story 15.29 — collect text deltas + the most recent error message so the
  // finally block can synthesize a useful assistant message. We don't store
  // a full transcript (cap at 4 KB, only used when no artifact / error fires).
  let collectedStreamText = '';
  let lastErrorMessage: string | null = null;
  // Story 15.29 — stable run_id used by both saveRun() and the assistant
  // message. Mirrors the legacy `run-${session_id.slice(0, 8)}` shape.
  const persistedRunId = `run-${id.slice(0, 8)}`;
  // O3 — timeline back-trace anchor. Captured from the projector after the
  // generator drains (the projector itself is block-scoped inside the try
  // below, so we stash its last assistant_text id here for the finally's
  // appendMessage). null when the run produced no streamed prose (e.g.
  // artifact-only / immediate error).
  let assistantTimelineMsgId: string | null = null;

  // Story 15.29 — write user message at stream start. We do this before
  // runSkillAssembler so chat history is consistent even if the LLM call
  // immediately fails (NO_API_KEY, etc.). Failure to write must NOT crash
  // the stream (try/catch + console.error per spec AC5).
  if (session.conversation_id) {
    try {
      appendMessage(session.conversation_id, {
        role: 'user',
        content: session.goal,
        run_id: null, // run_id is not yet known at user-message time
      });
    } catch (e) {
      console.error(
        `[run-sessions] failed to write user message for session ${id}:`,
        e,
      );
    }
  }

  // Story 15.13 — multi-layer prompt assembly. Replaces the 15.5 manual
  // `skill + ds.injection` concat. Order: discovery → identity → ds → skill
  // → project → sides → framework. Each layer is independently togglable;
  // empty / disabled layers drop without leaving separators behind.
  const skillForPrompt = SKILLS[session.skill_name];
  const ds = DESIGN_SYSTEMS[session.design_system_id ?? 'none'];
  // Story 15.12 integration — when POST body did not pass an explicit `side_files`
  // bypass, auto-load from `.xueban/skills/<skill_name>/{assets,references}/`.
  const sideFilesAuto = session.side_files
    ? { prompt: session.side_files, files: [], truncated: false }
    : loadSkillSideFiles(session.skill_name);
  // Story 15.29 — render conversation history block from sqlite. We exclude the
  // user message we just wrote above (created_at is monotonic ascending so the
  // very last row is "this turn's goal" — repeating it inside the history layer
  // is duplicative). Truncate single assistant content > 1024 char and cap the
  // total injected block at 4096 char (drop oldest first) to keep the prompt
  // sane regardless of conversation length.
  const conversationHistoryBlock = renderConversationHistoryBlock(
    session.conversation_id,
  );
  if (conversationHistoryBlock) {
    console.log(
      `[run-sessions] injecting conversation_history (chars=${conversationHistoryBlock.length})`,
    );
  }
  // 普通对话直通路（investigate 根因修复）：skill 层换成普通对话/编码 agent 人设，
  // 并关掉 discovery（强制"复述意图+计划"，组装味）与 framework（mode 专属指令），
  // 不加载组装 skill 的 side files。保留 identity（反 AI 味）+ conversation_history（多轮）。
  // 经验记忆（2026-11-05，XUEBAN-OS PR#3 移植）：按 skill_name 分域召回，拼进 skill 层
  // 尾部（静态段内、技能指令之后），空块自动丢弃不打乱段落顺序。失败静默。
  const lessonsBlock = lessonsBlockForSession(session.skill_name);
  // 知识库参考（2026-11-06 对话框知识库接线）：会话挂了记忆库 pack → 按本轮
  // goal 检索命中注入「知识库参考」块（fail-soft：Python 未起/超时 → 空块）。
  // 多轮接力每轮以当轮 goal 重新检索，与经验块并列拼进 skill 层尾部。
  const knowledgeBlockText = await knowledgeBlock(session.knowledge_pack_ids, session.goal);
  if (knowledgeBlockText) {
    console.log(
      `[run-sessions] injecting knowledge_block (chars=${knowledgeBlockText.length}, packs=${session.knowledge_pack_ids?.length ?? 0})`,
    );
  }
  const dynamicContextBlock = lessonsBlock + (knowledgeBlockText ? `\n\n${knowledgeBlockText}` : '');
  const compose = wantPlainConversation
    ? composeSystemPrompt({
        ds_injection: ds?.injection_prompt,
        skill_system_prompt: joinLessons(CONVERSATIONAL_AGENT_PROMPT, dynamicContextBlock),
        skill_mode: undefined,
        project_meta: session.project_meta ?? null,
        conversation_history: conversationHistoryBlock,
        side_files: undefined,
        layer_toggles: { ...(session.layer_toggles ?? {}), discovery: false, framework: false },
      })
    : composeSystemPrompt({
        ds_injection: ds?.injection_prompt,
        skill_system_prompt: joinLessons(skillForPrompt?.system_prompt, dynamicContextBlock),
        skill_mode: skillForPrompt?.mode,
        project_meta: session.project_meta ?? null,
        conversation_history: conversationHistoryBlock,
        side_files: sideFilesAuto.prompt || undefined,
        layer_toggles: session.layer_toggles,
      });
  // Emit compose event BEFORE the assembler kicks off so the front-end /
  // inspector sees the layer manifest first. Payload is metadata-only —
  // never the prompt text itself (avoids SSE bloat + prompt leakage).
  sendEvent('compose', {
    layers: compose.layers_included,
    skipped: compose.layers_skipped,
    total_chars: compose.total_chars,
    ds_id: ds?.ds_id ?? null,
    skill_id: session.skill_name,
    framework: compose.framework,
  });

  // ── S6.10-A — TimelineMessage projector ──────────────────────────────────
  // Derives a Trae/Codex-style ordered timeline (user_turn → assistant_meta →
  // step_panel/thinking/diff_plan/msg_foot + Polynoia-parity rich cards) from
  // the legacy fine-grained event stream, emitting `message` + `message-patch`
  // SSE frames in addition to the legacy events. P3: seeded with the session id
  // so message ids are deterministic across SSE reconnects (this handler re-runs
  // the whole pipeline per connection) — the front-end overwrites by id instead
  // of appending duplicate user_turns on every reconnect.
  //
  // Hoisted to HANDLER scope (not inside the stream try) so the finally block
  // can emit post-completion rich cards (critique verdict, git commit records,
  // file diffs) through the same projector + bus — they then replay on
  // reconnect exactly like the in-flight messages.
  const projector = createTimelineProjector({ idSeed: id });
  const flushProjector = (emit: {
    messages: import('../lib/contracts').TimelineMessage[];
    patches: import('../lib/contracts').MessagePatch[];
  }) => {
    for (const m of emit.messages) sendEvent('message', m);
    for (const p of emit.patches) sendEvent('message-patch', p);
  };
  // Open the turn before the assembler kicks off — the user's goal anchors the
  // timeline as its first row even when the LLM call fails immediately.
  flushProjector(projector.onUserMessage(session.goal));

  // ── Polynoia-parity — orchestrator plan card ─────────────────────────────
  // `node` events during team assembly describe the roster the orchestrator
  // derived from the goal. On the first execution substep we materialize a
  // live plan_card (task per node); subsequent agent-substep running/done
  // frames patch task rows in place. Single-agent runs (roster < 2) skip the
  // card — the step_panel already narrates them.
  const teamNodes: { id: string; title: string }[] = [];
  let planCardEmitted = false;
  const planTaskId = (nodeId: string): string => `node:${nodeId}`;
  const emitPlanIfReady = (runningNodeId: string): void => {
    if (planCardEmitted || teamNodes.length < 2) return;
    planCardEmitted = true;
    flushProjector(
      projector.onPlanCard({
        goal: session.goal,
        tasks: teamNodes.map((n) => ({
          id: planTaskId(n.id),
          title: n.title,
          assignee: n.title,
          status: n.id === runningNodeId ? 'running' : 'pending',
        })),
      }),
    );
  };
  const planTaskUpdate = (nodeId: string, patch: Partial<PlanTaskRow>): void => {
    if (!planCardEmitted) return;
    flushProjector(projector.onPlanTaskUpdate(planTaskId(nodeId), patch));
  };

  try {
    // S0 (2026-05-20) — synthesizeTeamRun 不再默认触发。skill 即使带 team.skill.yaml
    // 也走 runSkillAssembler 真 LLM 路径。原写死回放路径降级为 opt-in fallback:
    //   GET /api/run-sessions/:id/stream?fallback=synthetic
    // 仅在 BYOK key 配置错 / 离线 demo / LLM 不可用兜底时使用。设计稿见
    // docs/design/skill-team-conversion-design-v1.md §G "Xueban 路径选择"。
    let teamSpec = skillForPrompt?.team;
    const wantSyntheticFallback = req.query.fallback === 'synthetic';

    // S6.10-D demo aid — `?fallback=synthetic` + no teamSpec is the common
    // case for users who didn't pick a Team Skill (the default
    // `agent-team-blueprint` has no team_ref). Without a fallback team the
    // synthetic path is unreachable, the assembler runs for real, and a
    // missing API key returns 4 meta events then closes — the front-end then
    // burns retries reconnecting to the same dead session. To make the
    // demo URL trick `?fallback=synthetic` always work, load the BMAD team
    // yaml from the global library as the default rehearsal script.
    if (wantSyntheticFallback && !teamSpec) {
      try {
        const result = loadGlobalTeam('BMAD-METHOD');
        if (result.team) {
          teamSpec = {
            name: result.team.name,
            mode: result.team.mode,
            policy: result.team.policy,
            retry: result.team.retry,
            agents: result.resolvedAgents,
            edges: result.team.edges_v1.map(e => ({ from: e.from, to: e.to })),
            loaded_at: result.team.loaded_at,
            source_dir: result.team.source_dir,
          };
          console.log(`[run-sessions] fallback=synthetic + no teamSpec → loaded BMAD demo team`);
        }
      } catch (err) {
        console.warn(`[run-sessions] fallback=synthetic BMAD demo team load failed:`, err);
      }
    }

    // 2026-05-20 — Team-first vs Agent-first prompt 分流
    // teamSpec 存在（Skill Pack 有团队蓝图，如 BMAD/gSTACK）→ team-first，
    //   LLM 先 emit "挑选 Team 蓝图" 再 "配置 Agent 角色"。
    // teamSpec 不存在（裸 agent-team-blueprint，用户自由 chat）→ agent-first，
    //   LLM 跳过 "挑选 Team 蓝图"，直接从 goal 推导 agent 名单。
    // 注意：当前 system_prompt 已 layer-composed，含 team-first phase-1。
    // agent-first 时需要把 phase-1 重组，所以 reassemble。
    let effectiveSystemPrompt = compose.prompt;
    // 普通对话直通路：compose.prompt 已是普通对话人设，绝不做 agent-first 重组、
    // 也不追加 SINGLE_AGENT_DIRECTIVE（那是组装路的 roster 锁，对对话无意义且会泄"建节点"措辞）。
    if (!wantPlainConversation && !teamSpec && skillForPrompt?.system_prompt === composeMultiTurnPrompt('team-first')) {
      // 通用 agent-team-blueprint skill + 无 team yaml → agent-first 流
      effectiveSystemPrompt = compose.prompt.replace(
        composeMultiTurnPrompt('team-first'),
        composeMultiTurnPrompt('agent-first'),
      );
      console.log(`[run-sessions] no teamSpec → agent-first prompt flow`);
    }

    // 2026-05-27 / 2026-06-18 — 锁 roster 至 1：
    //   · 显式 "一个 agent" 文字（detectExplicitSingleAgent）— 原有；
    //   · mode='single'（个人态 Auto 默认 / 用户在 composer 选「单」）— 新增。
    // 两者都已折进上方 wantSingleAgent（含团队词 / 显式 @skill 逃生门）。
    // 避免 "帮我创建一个开发工程师agent" 扩成 3 人 squad；个人寒暄/单任务不组团队。
    if (wantSingleAgent && !wantPlainConversation) {
      effectiveSystemPrompt += SINGLE_AGENT_DIRECTIVE;
      console.log(
        `[run-sessions] single-agent roster cap (explicitText=${explicitSingleIntent.single} mode=${session.mode ?? '-'})`,
      );
    }

    const generator = teamSpec && wantSyntheticFallback
      ? synthesizeTeamRun(teamSpec, id, abortController.signal)
      : runSkillAssembler({
          goal: session.goal,
          skill_name: session.skill_name,
          explicit_skill: session.explicit_skill,
          skill_requested: session.skill_requested,
          session_id: id,
          anthropic_key: session.anthropic_key,
          signal: abortController.signal,
          system_prompt: effectiveSystemPrompt,
          // Story 15.9 — forward sanitized generation overrides. assembler.ts
          // applies env/default fallbacks when each is undefined.
          model: session.model,
          max_tokens: session.max_tokens,
          temperature: session.temperature,
          // Story 15.19 v2 — forward executor; assembler defaults to skill.executor
          // → 'anthropic-direct' when undefined, preserving back-compat.
          executor: session.executor,
          // 2026-05-30 — CLI 工作目录(用户选的);空 → assembler 回退到产物目录。
          cwd: session.cwd,
          // Story 15.18 — provider + api_key forwarded; default executor runner
          // dispatches into llm-providers/. CLI / ACP runners ignore them.
          provider: session.provider,
          api_key: session.api_key,
          // 2026-06-23 — 普通对话直通：assembler 据此跳过组装路由，verbatim 用上面
          // 的 conversational system_prompt 单轮 turn()，不注入「组装指挥官」directive。
          conversational: wantPlainConversation,
          // 2026-08-22 — 组装落库的工作区归属（team-persistence 写 Python team/agent 用）。
          workspace_id: session.workspace_id,
        });
    if (teamSpec && wantSyntheticFallback) {
      console.log(
        `[run-sessions] explicit synthetic fallback for skill=${session.skill_name} (${teamSpec.agents.length} agent(s), no LLM)`,
      );
    } else if (teamSpec) {
      console.log(
        `[run-sessions] skill=${session.skill_name} has team yaml (${teamSpec.agents.length} agent(s)) but routing to LLM assembler (S0 default).`,
      );
    }

    // ── S6.10-A — timeline projector lives at handler scope (hoisted above
    // the try so the finally block can emit post-completion cards). The turn
    // was already opened with onUserMessage(session.goal) before the try.

    for await (const { event, data } of generator) {
      if (abortController.signal.aborted) break;
      sendEvent(event, data);
      console.log(`[run-sessions] → event:${event}`, JSON.stringify(data).slice(0, 80));

      // ── S6.10-A — fan out into TimelineMessage / MessagePatch ─────────
      // Each legacy event drives the projector (lib/timeline-projector.ts),
      // which returns 0+ new messages and 0+ patches. Forward them as
      // dedicated SSE frames so the next-gen front-end can render a single
      // ordered list keyed by message id. Wrapped in try so a projector
      // bug never blocks legacy event forwarding.
      try {
        if (event === 'classify' && data && typeof data === 'object') {
          flushProjector(projector.onClassify(data as Record<string, unknown>));
        } else if (event === 'assemble' && data && typeof data === 'object') {
          const d = data as {
            status?: string;
            step?: string;
            step_index?: number;
            elapsed_ms?: number | null;
          };
          if (d.status === 'running' && typeof d.step_index === 'number') {
            flushProjector(projector.onAssembleStart(d.step_index, d.step ?? ''));
          } else if (d.status === 'done' && typeof d.step_index === 'number') {
            flushProjector(projector.onAssembleDone(d.step_index, d.elapsed_ms ?? 0));
          }
        } else if (event === 'agent-substep' && data && typeof data === 'object') {
          const d = data as {
            node_id?: string;
            substep?: string;
            status?: string;
            elapsed_ms?: number | null;
          };
          if (d.node_id && d.substep) {
            if (d.status === 'running') {
              flushProjector(projector.onAgentSubstepStart(d.node_id, d.substep));
              // Polynoia-parity — first execution substep materializes the
              // orchestrator's plan card; later ones flip the task row live.
              emitPlanIfReady(d.node_id);
              planTaskUpdate(d.node_id, { status: 'running' });
            } else if (d.status === 'done') {
              flushProjector(projector.onAgentSubstepDone(d.node_id, d.substep, d.elapsed_ms ?? 0));
              planTaskUpdate(d.node_id, {
                status: 'done',
                ...(typeof d.elapsed_ms === 'number' ? { elapsed_ms: d.elapsed_ms } : {}),
              });
            }
          }
        } else if (event === 'thinking-chunk' && data && typeof data === 'object') {
          const d = data as { text?: string };
          if (typeof d.text === 'string') flushProjector(projector.onThinkingChunk(d.text));
        } else if (event === 'blueprint' && data && typeof data === 'object') {
          const d = data as {
            filename?: string;
            yaml?: string;
            artifact_type?: unknown;
            artifact_url?: unknown;
          };
          flushProjector(projector.onBlueprint({ filename: d.filename, yaml: d.yaml }));
          // Polynoia-parity — every persisted artifact gets a reference card
          // inline in the chat; HTML artifacts additionally get a live
          // sandboxed web preview. Only real blueprint payloads (filename
          // present) — never placeholder cards.
          const bpType = typeof d.artifact_type === 'string' ? d.artifact_type : '';
          const bpUrl = typeof d.artifact_url === 'string' ? d.artifact_url : '';
          if (typeof d.filename === 'string' && d.filename) {
            flushProjector(
              projector.onArtifactCard({
                artifact_type: bpType || 'yaml',
                filename: d.filename,
                ...(bpUrl ? { url: bpUrl } : {}),
              }),
            );
            if (bpType === 'html') {
              flushProjector(
                projector.onWebPreview({
                  title: d.filename,
                  ...(bpUrl ? { url: bpUrl } : {}),
                }),
              );
            }
          }
        } else if (event === 'yaml-line' && data && typeof data === 'object') {
          const d = data as { line?: string };
          if (typeof d.line === 'string') flushProjector(projector.onYamlLine(d.line));
        } else if (event === 'text' && data && typeof data === 'object') {
          const d = data as { text?: string };
          if (typeof d.text === 'string') flushProjector(projector.onText(d.text));
        } else if (event === 'raw' && data && typeof data === 'object') {
          // T3: unclassified content → collapsed raw block (never the answer).
          const d = data as { text?: string; source?: string };
          if (typeof d.text === 'string') flushProjector(projector.onRaw(d.text, d.source));
        } else if (event === 'tool-use' && data && typeof data === 'object') {
          // T3 tool chain: surface tool invocations as tool_call messages so
          // the timeline's tool-group card renders them (previously dropped).
          const d = data as { name?: string; input?: unknown };
          if (typeof d.name === 'string') flushProjector(projector.onToolUse(d.name, d.input));
        } else if (event === 'tool-result' && data && typeof data === 'object') {
          const d = data as { output?: unknown };
          const out = typeof d.output === 'string' ? d.output : d.output != null ? JSON.stringify(d.output) : '';
          if (out) flushProjector(projector.onToolResult(out));
        } else if (event === 'usage' && data && typeof data === 'object') {
          // T3 usage chain: token usage → msg_foot tokens (was previously
          // forwarded as a legacy event but never projected into the timeline).
          flushProjector(projector.onUsage(data as Record<string, number>));
        } else if (event === 'unknown-tag' && data && typeof data === 'object') {
          // T3: unrecognized <sf:foo>…</sf:foo> block content was previously
          // dropped. Surface its body as a raw block so nothing vanishes silently.
          const d = data as { name?: string; body?: string };
          if (typeof d.body === 'string' && d.body.trim()) {
            flushProjector(projector.onRaw(d.body, `unknown-tag:${d.name ?? '?'}`));
          }
        } else if (event === 'complete') {
          flushProjector(projector.onComplete());
        }
      } catch (projectorErr) {
        // Projector failures are non-fatal — legacy event already sent.
        console.error(`[run-sessions] timeline-projector error on event=${event}:`, projectorErr);
      }

      // ── S2.3 — per-step payload accumulation ────────────────────────────
      // `assemble:running` opens a step; `node`/`edge`/`blueprint`/`classify`
      // events inside the bracket attach to it; `assemble:done` closes it,
      // builds a StepArtifact, persists, and emits `step-artifact`.
      if (event === 'assemble' && data && typeof data === 'object') {
        const d = data as {
          status?: string;
          step?: string;
          step_index?: number;
          output_kind?: OutputKind;
        };
        const sIdx = typeof d.step_index === 'number' ? d.step_index : null;
        if (d.status === 'running' && sIdx !== null) {
          // Drop any silently-orphaned previous open step for this index.
          openSteps.set(sIdx, {
            step_index: sIdx,
            step_name: d.step ?? '',
            output_kind: (d.output_kind ?? 'none') as OutputKind,
            started_at: new Date().toISOString(),
            nodes: [],
            edges: [],
          });
          currentStepIndex = sIdx;
        } else if ((d.status === 'done' || d.status === 'failed') && sIdx !== null) {
          const open = openSteps.get(sIdx);
          if (open) {
            let payload: unknown = null;
            if (open.output_kind === 'nodes') payload = open.nodes;
            else if (open.output_kind === 'edges') payload = open.edges;
            else if (open.output_kind === 'yaml') payload = open.yaml ?? null;
            else if (open.output_kind === 'classify') payload = open.classify ?? null;

            // "No mock" — only emit step-artifact when there's real content
            // (or the step is intentionally 'none' with no observable output).
            // The disk record still lands so retry/resume sees the step ran.
            const hasContent =
              (Array.isArray(payload) && payload.length > 0) ||
              (payload !== null && !Array.isArray(payload));

            const artifact: StepArtifact = {
              session_id: id,
              step_index: open.step_index,
              step_name: open.step_name,
              output_kind: open.output_kind,
              payload,
              started_at: open.started_at,
              finished_at: new Date().toISOString(),
              status: d.status === 'failed' ? 'failed' : 'done',
            };
            try {
              stepStoreSingleton.put(id, open.step_index, artifact);
            } catch (e) {
              console.error(`[run-sessions] stepStore.put failed:`, e);
            }
            if (hasContent || open.output_kind === 'none') {
              sendEvent('step-artifact', {
                step_index: open.step_index,
                step_name: open.step_name,
                output_kind: open.output_kind,
                payload,
              });
            }
            openSteps.delete(sIdx);
          }
          if (currentStepIndex === sIdx) currentStepIndex = null;
        }
      } else if (event === 'node' && currentStepIndex !== null) {
        const open = openSteps.get(currentStepIndex);
        if (open) open.nodes.push(data);
        // Polynoia-parity — harvest the roster for the plan card. Node frames
        // are the assembler's agent definitions; title/name/node_id are all
        // optional in practice, so extraction is fully defensive.
        if (data && typeof data === 'object') {
          const n = data as { node_id?: unknown; id?: unknown; title?: unknown; name?: unknown };
          const nodeId = typeof n.node_id === 'string' ? n.node_id : typeof n.id === 'string' ? n.id : null;
          if (nodeId) {
            const title =
              (typeof n.title === 'string' && n.title) ||
              (typeof n.name === 'string' && n.name) ||
              nodeId;
            if (!teamNodes.some((t) => t.id === nodeId)) {
              teamNodes.push({ id: nodeId, title });
            }
          }
        }
      } else if (event === 'edge' && currentStepIndex !== null) {
        const open = openSteps.get(currentStepIndex);
        if (open) open.edges.push(data);
      } else if (event === 'blueprint' && currentStepIndex !== null && data && typeof data === 'object') {
        const open = openSteps.get(currentStepIndex);
        if (open) {
          const d = data as { yaml?: unknown; filename?: unknown };
          open.yaml = {
            filename: typeof d.filename === 'string' ? d.filename : 'output.yml',
            content: typeof d.yaml === 'string' ? d.yaml : '',
          };
        }
      } else if (event === 'classify' && currentStepIndex !== null) {
        // Only LLM-emitted classify lands here — the TS-side classify above
        // fires before any step is open (currentStepIndex === null) so it
        // never pollutes step payload.
        const open = openSteps.get(currentStepIndex);
        if (open) open.classify = data;
      }

      // ── Tool-call projection → `tool-call` SSE (clawsgo left-pane parity) ──
      // Translate the runtime's structured tool-use / tool-result frames into a
      // normalized tool-call shape nested under the currently-open step. Wrapped
      // defensively so a malformed payload never blocks the main event forward.
      //
      // R2 punch-list #1 — TEAM / DAG coverage: this projection is NOT limited to
      // the single-agent ConversationRuntime loop. Team-backed skills run their
      // tool loops inside workflow/scheduler.runDag → executor.executeNode →
      // callable.turn(), and EVERY chunk is stamped with `node_id`
      // (executor.ts:207) before flowing through the SAME assembler
      // pipeChunksToSse path that emits these typed `tool-use` / `tool-result`
      // frames (assembler.ts:1325-1361, carrying chunk.node_id). So when a team
      // node executes real tools (ApiClientCallable + picked tools →
      // buildRuntimeStream, which hosts the tool loop), its tool calls arrive
      // here already scoped per-agent via `node_id` and are projected exactly
      // like single-agent calls — no extra wiring needed.
      //
      // BOUNDARY (documented, not fabricated): two team transports genuinely do
      // not carry structured tool chunks, so they project NO tool-call frames:
      //   1. A team node whose callable is a plain `callable.turn()` with no
      //      picked tools (or a non-ApiClient CLI/ACP callable) does not run a
      //      structured tool loop — it only yields text/usage chunks.
      //   2. The `?fallback=synthetic` rehearsal (synthesizeTeamRun) is an
      //      ASSEMBLY-design replay with no LLM and no real tool execution; it
      //      emits node/substep/edge frames but never tool-use/tool-result.
      // In both cases we intentionally surface nothing rather than inventing
      // tool calls — the StepList simply shows the step without nested cards.
      try {
        if (event === 'tool-use' && data && typeof data === 'object') {
          const projected = toolCallProjector.onToolUse(data, currentStepIndex);
          if (projected) sendEvent('tool-call', projected);
        } else if (event === 'tool-result' && data && typeof data === 'object') {
          const projected = toolCallProjector.onToolResult(data);
          if (projected) sendEvent('tool-call', projected);
        }
      } catch (toolCallErr) {
        console.error(`[run-sessions] tool-call projection error on event=${event}:`, toolCallErr);
      }

      // 多产物登记（2026-11-07）— 每件 blueprint 产物都进 capturedArtifacts
      // （同名覆盖去重）；artifactInfo = 最后一件（主产物，沿用旧语义）。
      if (event === 'blueprint' && data && typeof data === 'object') {
        const entry = captureBlueprintArtifact(
          capturedArtifacts,
          data as {
            artifact_type?: unknown;
            filename?: unknown;
            artifact_url?: unknown;
          },
        );
        if (entry) artifactInfo = entry;
      } else if (event === 'error') {
        sawError = true;
        // Story 15.29 — capture error message for the assistant message.
        if (data && typeof data === 'object') {
          const d = data as { message?: unknown };
          if (typeof d.message === 'string') lastErrorMessage = d.message;
        }
      } else if (event === 'complete') {
        sawComplete = true;
      } else if (event === 'token' || event === 'text-delta') {
        // Story 15.29 — collect a slim transcript so the finally block has
        // *something* to write back when no artifact fires (e.g. answer mode).
        // Hard-cap at 4 KB to bound memory.
        if (collectedStreamText.length < 4096 && data && typeof data === 'object') {
          const d = data as { text?: unknown };
          if (typeof d.text === 'string') {
            collectedStreamText = (collectedStreamText + d.text).slice(0, 4096);
          }
        }
      }
    }
    // O3 — generator drained: stash the projector's last assistant_text id so
    // the finally's appendMessage can back-link the conversation row to its
    // timeline message. Read here (inside the try, projector still in scope).
    assistantTimelineMsgId = projector.lastAssistantTextId();
  } catch (err) {
    if (abortController.signal.aborted) {
      // Expected — client disconnected.
      console.log(`[run-sessions] stream cancelled for session ${id}`);
    } else {
      console.error(`[run-sessions] Stream error for session ${id}:`, err);
      sawError = true;
      // Emit unconditionally — the bus buffers it so reconnecting clients still
      // see the error even if no view is currently attached.
      sendEvent('error', {
        // sendEvent() runs classifyErrorCode() against this message; the
        // regexes will sort 5xx / ECONNREFUSED / 401 surfacing in `err`.
        code: 'INTERNAL_ERROR',
        message: `Internal server error during assembly: ${(err as Error)?.message ?? String(err)}`,
        session_id: id,
      });
    }
  } finally {
    // Story 15.14 — Auto-critique pass. Only when we saw a complete event AND
    // the client did not disconnect AND we know which artifact to evaluate.
    // We run critique BEFORE res.end() so the front-end can subscribe to the
    // critique-progress / critique-result events on the same SSE stream.
    // Critique failure NEVER breaks the main flow (saveRun + res.end still run).
    // 2026-05-11 review F4 (15.14 AC6): server-side opt-out from settings.json.
    // sf.auto_critique 默认 true（保留原行为）；用户可 PUT /api/settings/sf.auto_critique
    // false 跳过 critic 调用，节省 BYOK token。
    // 2026-05-11 follow-up — also honor per-request `body.auto_critique` so the
    // front-end GenerationSettings toggle (writes localStorage, forwarded via
    // createRunSession body) takes effect WITHOUT needing a settings.json
    // PUT round-trip. Body wins over server settings when explicit.
    const sessionAutoCritique = (session as unknown as Record<string, unknown>).auto_critique;
    const autoCritique =
      typeof sessionAutoCritique === 'boolean'
        ? sessionAutoCritique
        : getSetting('sf.auto_critique');
    const critiqueEnabled = autoCritique === undefined || autoCritique === true;
    if (
      sawComplete &&
      !abortController.signal.aborted &&
      artifactInfo.filename &&
      critiqueEnabled
    ) {
      try {
        const userGoal = session.goal;
        const skillForCritique = SKILLS[session.skill_name];
        // SkillDefinition currently has no `steps` field (Story 15.10 may add it
        // via SKILL.md frontmatter); read it defensively as an optional bag.
        const expectedSteps =
          ((skillForCritique as unknown as { steps?: unknown })?.steps as string[] | undefined) ??
          [];
        const critOut = await runCritique(
          {
            session_id: id,
            filename: artifactInfo.filename,
            user_goal: userGoal,
            expected_steps: expectedSteps,
            // 2026-05-11 review F1: 真功能 bug 修复 — sessionStore 字段是 `api_key`
            // (15.18 multi-provider 改名后)，非 `anthropic_key`。原代码读 undefined →
            // BYOK 用户的 critique 永远走 env fallback，无 env 时立刻 NO_API_KEY 降级。
            anthropic_key: session.api_key,
          },
          (stage, message) => sendEvent('critique-progress', { stage, message }),
        );
        sendEvent('critique-result', {
          session_id: id,
          artifact: artifactInfo.filename,
          scores: critOut.scores,
          overall_summary: critOut.overall_summary,
          lint_summary: critOut.lint_summary,
          duration_ms: critOut.duration_ms,
          error_code: critOut.error_code ?? null,
          error_message: critOut.error_message ?? null,
        });
        // Polynoia-parity — surface the critique as an inline verdict card on
        // the timeline (metrics table + summary). Only when the critic actually
        // scored (scores === null = NO_API_KEY / API_ERROR → no card, the
        // legacy critique-result event already told the FE why).
        if (critOut.scores) {
          const dimEntries = Object.entries(critOut.scores).filter(
            (entry): entry is [string, { score: number; rationale: string; improvement?: string }] =>
              entry[1] != null,
          );
          const avg =
            dimEntries.length > 0
              ? Math.round((dimEntries.reduce((s, [, v]) => s + v.score, 0) / dimEntries.length) * 10) / 10
              : 0;
          flushProjector(
            projector.onVerdictCard({
              title: '评审结论 · Verifier',
              score: avg,
              max_score: 10,
              summary: critOut.overall_summary,
              metrics: dimEntries.map(([dim, v]) => ({
                label: dim.split('_').join(' '),
                value: `${v.score}/10`,
                detail: v.rationale,
              })),
              notes: [
                `lint: ${critOut.lint_summary.errors} errors · ${critOut.lint_summary.warnings} warnings · ${critOut.lint_summary.infos} infos`,
                `耗时 ${(critOut.duration_ms / 1000).toFixed(1)}s`,
              ],
            }),
          );
        }
      } catch (critErr) {
        // Defensive — runCritique should never throw, but if it does we still
        // emit a degraded result so the front-end stops spinning.
        console.error(`[run-sessions] critique unexpected error for session ${id}:`, critErr);
        sendEvent('critique-result', {
          session_id: id,
          artifact: artifactInfo.filename,
          scores: null,
          overall_summary: `[critique threw: ${critErr instanceof Error ? critErr.message : String(critErr)}]`,
          lint_summary: { errors: 0, warnings: 0, infos: 0 },
          duration_ms: 0,
          error_code: 'CRITIQUE_FAILED',
          error_message: critErr instanceof Error ? critErr.message : String(critErr),
        });
      }
    }

    // ── Polynoia-parity — git evidence (commit records + real file diffs) ──
    // For workspace runs inside a git repo, surface what the agents actually
    // committed/changed during THIS run (startHead..HEAD), inline in the chat:
    // commit_record per new commit → table_panel of changed-file stats (when
    // >1 file) → file_diff per changed file (bounded). Emitted BEFORE
    // runBus.finish so the frames land in the replay buffer. All probes
    // degrade to null on non-repos → nothing emitted, never fabricated.
    if (!abortController.signal.aborted && (sawComplete || sawError) && session.cwd) {
      try {
        const evidence = await inspectGitRun(
          session.cwd,
          gitStartHead,
          new Date(session.created_at).toISOString(),
        );
        if (evidence) {
          for (const c of evidence.commits) {
            flushProjector(projector.onCommitRecord(c));
          }
          if (evidence.numstat.length > 1) {
            flushProjector(
              projector.onTablePanel({
                caption: '本次运行文件变更',
                columns: ['文件', '新增', '删除'],
                rows: evidence.numstat.map((n) => [n.path, n.insertions, n.deletions]),
              }),
            );
          }
          for (const fd of evidence.fileDiffs) {
            flushProjector(projector.onFileDiff(fd));
          }
        }
      } catch (gitErr) {
        console.error(`[run-sessions] git introspection failed for session ${id}:`, gitErr);
      }
    }

    // T3-1: mark the run terminal. runBus.finish() flushes `end()` to EVERY
    // attached view (closing each res + clearing its heartbeat), so we no longer
    // res.end() this one connection directly — a reconnect that attaches after
    // we finish will replay the buffer then end immediately (attach() handles
    // the terminal case). Status: failed iff we saw an error and no complete.
    runBus.finish(id, sawError && !sawComplete ? 'failed' : 'succeeded');
    stopLoopWatcher(); // 迭代环 watcher 随 run 终态停止
    // 阶段0 — 终态写进持久索引
    try { setRunIndexStatus(id, sawError && !sawComplete ? 'failed' : 'succeeded'); } catch { /* 非致命 */ }
    // 经验记忆自动沉淀（best-effort，绝不影响主流程）：
    //   失败 → 教训（失败原因原文）；用户中断 → 偏好。零 LLM 调用。
    try { recordRunLesson(session, { aborted: abortController.signal.aborted, failed: sawError && !sawComplete, errorMessage: lastErrorMessage }); } catch { /* 非致命 */ }
    console.log(`[run-sessions] run ${id} finished (complete=${sawComplete} error=${sawError})`);

    // Persist run record — only when the stream actually terminated (not when
    // the client aborted mid-flight). A 'complete' OR an explicit 'error'
    // event both count as termination.
    if (!abortController.signal.aborted && (sawComplete || sawError)) {
      try {
        const skill = SKILLS[session.skill_name];
        saveRun(
          {
            run_id: persistedRunId,
            session_id: id,
            goal: session.goal,
            skill_name: session.skill_name,
            skill_display_name: skill?.name ?? session.skill_name,
            artifact_type: artifactInfo.type,
            artifact_filename: artifactInfo.filename,
            artifact_url: artifactInfo.url,
            status: sawError && !sawComplete ? 'failed' : 'completed',
            created_at: new Date(session.created_at).toISOString(),
            completed_at: new Date().toISOString(),
            project_dir: `.xueban/projects/${id}`,
          },
          {
            // 项目产物面板修复（2026-09-03）— run 归属落库：conversation 在
            // POST /api/run-sessions 时已定（显式传入或按 project_id/group_id
            // 自动创建），project_id 由 conversation 反查；无会话则 NULL。
            conversation_id: session.conversation_id ?? null,
            project_id: session.conversation_id
              ? (getConversation(session.conversation_id)?.project_id ?? null)
              : null,
          },
        );
        // 多产物登记（2026-11-07）— 本 run 捕获的每件 blueprint 产物独立
        // 落 run_artifacts（幂等全量重登记）。runs 三字段仍是主产物（最后
        // 一件）——上面 saveRun 刚写完，这里补全其余产物/同名去重后的全集。
        saveRunArtifacts(
          persistedRunId,
          capturedArtifacts.map((a) => ({
            artifact_type: a.type,
            artifact_filename: a.filename,
            artifact_url: a.url,
          })),
        );
      } catch (persistErr) {
        // Don't fail the response just because we couldn't persist the
        // run record — the SSE stream is already over. Log and move on.
        console.error(`[run-sessions] saveRun failed for session ${id}:`, persistErr);
      }
    }

    // ── Story 15.29 — write assistant message to conversation log ─────────
    // Always run (even on client abort) so the chat timeline is complete.
    // Content priority:
    //   1. artifact preview ("[Generated artifact: <filename>]")
    //   2. collected stream text (1st 1024 chars)
    //   3. abort marker
    //   4. error marker
    //   5. empty fallback
    // Writing failure NEVER reaches the client (try/catch + console.error).
    if (session.conversation_id) {
      let summary: string;
      // 多产物登记（2026-11-07）— 多件产物在回执里全部点名（此前只提最后
      // 一件）。单件/无 filename 保持旧文案，链路优先级不变。
      const artifactNames = capturedArtifacts
        .map((a) => a.filename ?? a.url)
        .filter((n): n is string => typeof n === 'string' && !!n);
      if (artifactNames.length > 1) {
        summary = `[Generated artifacts: ${artifactNames.join(', ')}]`;
      } else if (artifactInfo.filename) {
        summary = `[Generated artifact: ${artifactInfo.filename}]`;
      } else if (stripLeakedSseFrames(collectedStreamText).trim().length > 0) {
        // P2 — control-frame channel isolation (root-cure plan §5). NEVER
        // persist frame-shaped text as assistant content: if it lands in the
        // conversation log it gets re-injected as CONVERSATION HISTORY next
        // turn, the LLM parrots the frame shape, and the whole turn collapses
        // into sse-frame-leak `raw` blocks. Sanitize at the WRITE side so a
        // frame can never enter storage in the first place; the read-side
        // strip in renderConversationHistoryBlock then only has to clean up
        // legacy rows written before this guard existed.
        const clean = stripLeakedSseFrames(collectedStreamText).trim();
        summary = clean.slice(0, 1024);
        if (clean.length > 1024) summary += '…(truncated)';
      } else if (abortController.signal.aborted) {
        summary = '[run aborted by user]';
      } else if (sawError) {
        summary = lastErrorMessage
          ? `[run failed: ${lastErrorMessage}]`
          : '[run failed]';
      } else {
        summary = '[run completed without artifact]';
      }
      try {
        appendMessage(session.conversation_id, {
          role: 'assistant',
          content: summary,
          // Only attach run_id when saveRun actually ran (terminated runs).
          run_id:
            !abortController.signal.aborted && (sawComplete || sawError)
              ? persistedRunId
              : null,
          // O3 — back-trace bridge: link this conversation row to the timeline
          // projector's assistant_text message that streamed the prose. null
          // when the run produced no streamed text (artifact-only / early error).
          timeline_message_id: assistantTimelineMsgId,
        });
      } catch (e) {
        console.error(
          `[run-sessions] failed to write assistant message for session ${id}:`,
          e,
        );
      }
    }
  }
});

// ── POST /api/run-sessions/:id/steps/:n/retry ────────────────────────────────
//
// S4.1 (intent-workflow-design-v1 §4.5) — step-level retry.
//
// Spec-compliant v2 would replay 0..n-1 produced artifacts into a fresh LLM
// call and restart from step n. MVP-fallback shipped here:
//   - drop step n..N from on-disk + in-memory state
//   - signal any active SSE stream via a `retry-pending` event
//   - return 202 with {strategy:'full_rerun', kept_steps, cleared_steps}
//
// The front-end is expected to open a new run (createRunSession) for the
// rerun; we do NOT auto-start it server-side so the endpoint stays
// idempotent and trivial to reason about. Future v2 will keep 0..n-1 and
// resume from n without dropping context.
router.post('/:id/steps/:n/retry', (req: Request, res: Response) => {
  const { id, n } = req.params;
  const session = sessionStore.get(id);
  if (!session) {
    res.status(404).json({
      error: { code: 'SESSION_NOT_FOUND', message: `Run session ${id} not found` },
    });
    return;
  }
  const stepN = Number.parseInt(n, 10);
  if (!Number.isInteger(stepN) || stepN < 0 || stepN > 99) {
    res.status(400).json({
      error: { code: 'INVALID_STEP_INDEX', message: `step index ${n} out of range 0..99` },
    });
    return;
  }

  const all = stepStoreSingleton.list(id);
  const cleared = all.filter((s) => s.step_index >= stepN).map((s) => s.step_index);
  const kept = all.filter((s) => s.step_index < stepN).map((s) => s.step_index);

  // Drop ALL on-disk state for this session — the in-memory list rebuilds on
  // the next run. step-store.clear() removes <session_id>/steps/.
  stepStoreSingleton.clear(id);

  // T3-1: surface retry-pending to currently-attached views, then reset the run
  // so the next GET /stream connection re-runs from scratch (full_rerun). Without
  // the reset, claimStart would return false on the existing (terminal) run and
  // the reconnect would only replay the old buffer instead of re-executing.
  runBus.emit(id, 'retry-pending', {
    session_id: id,
    from_step: stepN,
    cleared_steps: cleared,
    strategy: 'full_rerun',
  });
  runBus.reset(id);

  console.log(
    `[run-sessions] retry requested session=${id} from_step=${stepN} cleared=${cleared.join(',') || '(none)'}`,
  );

  res.status(202).json({
    session_id: id,
    strategy: 'full_rerun',
    from_step: stepN,
    cleared_steps: cleared,
    kept_steps: kept,
  });
});

// ── GET /api/run-sessions/:id/steps/:n ───────────────────────────────────────
//
// Task 1 — read a single step artifact (补 fetchStepArtifact 缺失后端端点).
//
// Returns the persisted StepArtifact for step n of session id.
// If the artifact is not in memory (e.g. after a Node restart), attempts to
// hydrate from disk first (idempotent). Returns 404 if still not found.
router.get('/:id/steps/:n', async (req: Request, res: Response) => {
  const { id } = req.params;
  const n = Number(req.params.n);
  if (!Number.isInteger(n) || n < 0) {
    res.status(400).json({ error: { code: 'BAD_STEP', message: `非法步骤序号: ${req.params.n}` } });
    return;
  }
  // 重启后内存为空 → 先从磁盘 hydrate（幂等）
  if (!stepStoreSingleton.get(id, n)) {
    await stepStoreSingleton.loadSession(id).catch(() => {});
  }
  const art = stepStoreSingleton.get(id, n);
  if (!art) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '该步骤产物尚未落盘' } });
    return;
  }
  res.json({ data: art });
});

// ── POST /api/run-sessions/:id/resume ────────────────────────────────────────
//
// S4.2 (intent-workflow-design-v1 §4.5) — resume from the last completed step.
// MVP-fallback shape mirrors S4.1: identify the next step we'd run, signal an
// active stream via `resume-pending`, return 202. If every persisted step is
// already `done` AND no active stream exists, return 410 Gone (session is
// already complete — there's nothing to resume).
router.post('/:id/resume', (req: Request, res: Response) => {
  const { id } = req.params;
  const session = sessionStore.get(id);
  if (!session) {
    res.status(404).json({
      error: { code: 'SESSION_NOT_FOUND', message: `Run session ${id} not found` },
    });
    return;
  }

  const all = stepStoreSingleton.list(id);
  const lastDone = [...all].reverse().find((s) => s.status === 'done');
  const resumeFrom = lastDone ? lastDone.step_index + 1 : 0;

  const run = runBus.get(id);
  const active = Boolean(run && !runBus.isTerminal(run.status));
  const allDone = all.length > 0 && all.every((s) => s.status === 'done');
  if (allDone && !active) {
    res.status(410).json({
      error: {
        code: 'SESSION_COMPLETE',
        message: `Run session ${id} has already completed (last done step ${lastDone?.step_index ?? '?'}).`,
      },
    });
    return;
  }

  console.log(
    `[run-sessions] resume requested session=${id} from_step=${resumeFrom} (last_done=${lastDone?.step_index ?? 'none'})`,
  );

  // T3-1: surface resume-pending to attached views, then reset so the next
  // connection re-runs (see retry endpoint for rationale).
  runBus.emit(id, 'resume-pending', {
    session_id: id,
    from_step: resumeFrom,
    strategy: 'full_rerun',
  });
  runBus.reset(id);

  res.status(202).json({
    session_id: id,
    strategy: 'full_rerun',
    from_step: resumeFrom,
    kept_steps: all.filter((s) => s.step_index < resumeFrom).map((s) => s.step_index),
  });
});

// ── POST /api/run-sessions/:id/abort ─────────────────────────────────────────
//
// 2026-05-16 — User pressed "Stop" in the run-session composer. Cancel the
// upstream LLM call via the per-stream AbortController, write a final SSE
// 'aborted' event so any still-connected client sees a reason, and drop the
// session from the in-memory store so subsequent GET /stream calls 404.
//
// Behavior:
//   - active stream exists      → abort + close + 204
//   - session exists, no stream → drop from store + 204
//   - nothing found             → 404 (lets the front-end know it was a no-op
//                                 BUT useRunSession.abort already updated UI
//                                 so this is purely informational)
//
// Always idempotent: a second POST after the first succeeds returns 404 and
// the UI shrugs it off.
router.post('/:id/abort', (req: Request, res: Response) => {
  const { id } = req.params;
  const run = runBus.get(id);
  const session = sessionStore.get(id);

  if (!run && !session) {
    res.status(404).json({
      error: { code: 'SESSION_NOT_FOUND', message: `Run session ${id} not found` },
    });
    return;
  }

  // T3-1: cancel through the bus. We first emit an `aborted` frame so every
  // attached view (and any reconnect that replays the buffer) sees the reason,
  // then runBus.cancel() aborts the run's AbortController — the pipeline's
  // for-await breaks on the signal and its finally{} calls runBus.finish()
  // (idempotent with cancel's own finish, which ends all views).
  if (run && !runBus.isTerminal(run.status)) {
    console.log(`[run-sessions] client requested abort for session ${id}`);
    runBus.emit(id, 'aborted', { session_id: id, reason: 'user_requested' });
    runBus.cancel(id);
  }

  // Drop the session record so a stale client cannot reconnect.
  sessionStore.delete(id);
  res.status(204).end();
});

// ────────────────────────────────────────────────────────────────────────────
// 阶段 1（ROADMAP-agentinfra / GAP G1）— Node 工具审批决策端点。
//
// prompt 模式的工具调用经 lib/tool-approval 注册表挂起并向本 run 事件日志
// 发 `permission_check`（decision:'pending'）帧；前端（或任何客户端）拿到
// request_id 后经下方端点回填决策，注册表 settle 掉挂起的 dispatch：
//   approve → 执行器续跑；deny → is_error tool_result（fail-closed，与
// 超时/中止同路）。决策帧同样落事件日志，重放可见。
// 语义对齐 Python 侧 runtime/approval/service.py（pending/approve/reject）。
// ────────────────────────────────────────────────────────────────────────────
router.get('/:id/permissions', (req: Request, res: Response) => {
  const { id } = req.params;
  res.json({ session_id: id, pending: toolApprovals.listPending(id) });
});

router.post('/:id/permissions/:requestId', (req: Request, res: Response) => {
  const { id, requestId } = req.params;
  const body = req.body as { decision?: unknown; reason?: unknown };
  const decision = body.decision;
  if (decision !== 'approve' && decision !== 'deny') {
    res.status(400).json({
      error: {
        code: 'INVALID_DECISION',
        message: `decision must be 'approve' or 'deny' (got ${JSON.stringify(decision)})`,
      },
    });
    return;
  }
  const reason = typeof body.reason === 'string' && body.reason.trim()
    ? body.reason.trim()
    : undefined;
  // expectedSession=id：跨会话误用他 session 的 request_id 直接拒绝。
  const ok = toolApprovals.decide(
    requestId,
    decision as PermissionDecision,
    reason,
    id,
  );
  if (!ok) {
    res.status(404).json({
      error: {
        code: 'REQUEST_NOT_FOUND_OR_SETTLED',
        message: `No pending permission request ${requestId} on session ${id}`,
      },
    });
    return;
  }
  res.json({ session_id: id, request_id: requestId, decision });
});

// ────────────────────────────────────────────────────────────────────────────
// Polynoia-parity — chat-side message editing ("直接在聊天里看还能编辑").
//
// The stream persists each assistant turn into the session's conversation with
// the timeline projector's assistant_text message id as back-trace (O3). The
// edit UI knows that timeline id, so the client PATCHes
// { timeline_message_id, content } here; we resolve the conversation via the
// session record and rewrite the persisted row (stamping edited_at).
//
// Scope note: this edits the PERSISTED conversation row (history / prompt
// context for subsequent turns). The live SSE replay regenerates timeline
// messages from the event buffer, so an edited body shows until the session
// is reloaded — same behaviour as IM apps editing their local copy.
// ────────────────────────────────────────────────────────────────────────────
router.patch('/:id/messages', (req: Request, res: Response) => {
  const { id } = req.params;
  const session = sessionStore.get(id);
  if (!session) {
    res.status(404).json({ error: 'session_not_found' });
    return;
  }
  if (!session.conversation_id) {
    res.status(409).json({ error: 'session_has_no_conversation' });
    return;
  }
  const body = req.body as { timeline_message_id?: unknown; content?: unknown };
  const timelineId = typeof body.timeline_message_id === 'string' ? body.timeline_message_id : '';
  const content = typeof body.content === 'string' ? body.content : '';
  if (!timelineId || !content.trim()) {
    res.status(400).json({ error: 'timeline_message_id_and_content_required' });
    return;
  }
  const updated = updateMessageByTimelineId(session.conversation_id, timelineId, content);
  if (!updated) {
    res.status(404).json({ error: 'message_not_found' });
    return;
  }
  res.json({
    message_id: updated.message_id,
    conversation_id: updated.conversation_id,
    timeline_message_id: updated.timeline_message_id,
    content: updated.content,
    edited_at: updated.edited_at,
  });
});

// ────────────────────────────────────────────────────────────────────────────
// S6.3 — Skill team synthesizer
//
// When a skill ships a structured `team.skill.yaml`, we don't need the LLM.
// Persona, model params, tools, memory, IO contracts — everything lives in
// the skill files already. We replay them as an SSE event stream identical
// in shape to what `runSkillAssembler` would produce, so the existing
// step-artifact accumulation logic (S2.3) downstream works unchanged.
//
// Why a synthesizer instead of letting the LLM read team.skill.yaml? Three
// reasons: (1) determinism — same input always produces the same UI;
// (2) zero token cost — substantial for paying users; (3) provenance —
// the front-end can claim "from reader.skill.yaml#persona" and mean it,
// because the byte arrived from disk, not from a model that might have
// paraphrased.
//
// Goal is intentionally absent from the synthesized run; skill-backed teams
// are by definition pre-designed. The goal still appears in `compose` /
// session metadata for later inspection, and a future "validation"
// pre-pass could add an optional LLM thinking-step before this generator
// fires. Out of scope for S6.x.
// ────────────────────────────────────────────────────────────────────────────

async function pace(ms: number, signal: AbortSignal): Promise<void> {
  // Light per-event delay so the front-end sees a "streaming" feel instead
  // of one synchronous flush. Returns immediately on abort.
  await new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      resolve();
    }, { once: true });
  });
}

function synthesizeYaml(team: TeamDef): string {
  const lines: string[] = [];
  lines.push(`# auto-generated by Xueban · team.skill.yaml`);
  lines.push(`team: ${team.name}`);
  lines.push(`mode: ${team.mode ?? 'serial'}`);
  if (team.policy) lines.push(`policy: ${team.policy}`);
  if (typeof team.retry === 'number') lines.push(`retry: ${team.retry}`);
  lines.push(`agents:`);
  for (const a of team.agents) {
    lines.push(`  - id: ${a.id}`);
    lines.push(`    title: ${a.title}`);
    lines.push(`    type: ${a.type ?? 'agent'}`);
    lines.push(`    model: ${a.model.id}`);
    lines.push(`    tools:`);
    for (const t of a.tools.picked) {
      lines.push(`      - ${t}`);
    }
    if (a.memory) lines.push(`    memory: ${a.memory}`);
  }
  if (team.edges.length > 0) {
    lines.push(`edges:`);
    for (const e of team.edges) {
      lines.push(`  - { from: ${e.from}, to: ${e.to} }`);
    }
  } else {
    lines.push(`edges: []`);
  }
  return lines.join('\n');
}

/** Build the per-substep source ref used by SSE + the front-end provenance label. */
function substepSource(agent: TeamDef['agents'][number], substep: string): { source: string; tokens: number } {
  // identity is a synthetic slot (no skill anchor) — borrow team.skill.yaml provenance.
  if (substep === 'identity') {
    return { source: `team.skill.yaml#agents.${agent.id}`, tokens: Math.ceil((agent.title.length + (agent.sub?.length ?? 0)) / 4) };
  }
  const key = substep as SkillSlot;
  const anchor = agent.anchors[key];
  if (!anchor) return { source: `${agent.source_file}#${substep}`, tokens: 0 };
  return { source: anchor.ref, tokens: anchor.tokens };
}

async function* synthesizeTeamRun(
  team: TeamDef,
  sessionId: string,
  signal: AbortSignal,
): AsyncGenerator<{ event: string; data: unknown }> {
  const SUBSTEPS = ['identity', 'persona', 'model', 'tools', 'memory'] as const;
  // Pacing — small per-event delay so SSE feels streamed rather than flushed
  // all at once. Total run ~3-5s for a 4-agent team, comparable to a fast LLM
  // call without burning a single token.
  const DELAY_EVENT = 30;
  const DELAY_SUBSTEP = 80;
  const DELAY_STEP = 200;

  yield {
    event: 'classify',
    data: {
      output_type: 'workflow',
      mode: 'team',
      confidence: 0.98,
      complexity: Math.min(5, Math.max(1, team.agents.length)),
      source: 'skill-team',
    },
  };
  await pace(DELAY_EVENT, signal);

  // Step 0 — 分析目标需求
  yield { event: 'assemble', data: { step: '分析目标需求', step_index: 0, output_kind: 'none', status: 'running', elapsed_ms: null } };
  await pace(DELAY_STEP, signal);
  yield {
    event: 'thinking-chunk',
    data: {
      step: '分析目标需求',
      text: `命中 skill team "${team.name}" — 从 team.skill.yaml 直接派生 ${team.agents.length} 个 agent，跳过 LLM 生成。`,
    },
  };
  await pace(DELAY_EVENT, signal);
  yield { event: 'assemble', data: { step: '分析目标需求', step_index: 0, output_kind: 'none', status: 'done', elapsed_ms: DELAY_STEP } };

  // Step 1 — 挑选 Team 蓝图
  yield { event: 'assemble', data: { step: '挑选 Team 蓝图', step_index: 1, output_kind: 'none', status: 'running', elapsed_ms: null } };
  await pace(DELAY_STEP, signal);
  yield {
    event: 'thinking-chunk',
    data: {
      step: '挑选 Team 蓝图',
      text: `mode=${team.mode ?? 'serial'} · policy=${team.policy ?? 'strict'} · retry=${team.retry ?? 3} · edges=${team.edges.length}`,
    },
  };
  await pace(DELAY_EVENT, signal);
  yield { event: 'assemble', data: { step: '挑选 Team 蓝图', step_index: 1, output_kind: 'none', status: 'done', elapsed_ms: DELAY_STEP } };

  // Step 2 — 配置 Agent 角色 (the big one — emits nodes + 5 substeps each + persona)
  yield { event: 'assemble', data: { step: '配置 Agent 角色', step_index: 2, output_kind: 'nodes', status: 'running', elapsed_ms: null } };
  let agentStepElapsed = 0;
  for (const agent of team.agents) {
    if (signal.aborted) return;
    // Emit node first so the front-end has a target for the substeps.
    yield {
      event: 'node',
      data: {
        node_id: agent.id,
        type: agent.type ?? 'agent',
        title: agent.title,
        sub: agent.sub ?? '',
        chips: [agent.model.id, ...agent.tools.picked.slice(0, 2)].filter(Boolean),
        status: 'building',
        avatar_char: agent.avatar_char ?? agent.title.charAt(0),
        model: agent.model.id,
        memory: agent.memory,
        tools_picked: agent.tools.picked,
        tools_candidate: agent.tools.candidate,
        persona: agent.persona.split('\n').find((l: string) => l.trim().length > 0)?.slice(0, 80),
        skill_ref: agent.source_file,
        temperature: agent.model.temperature,
        max_tokens: agent.model.max_tokens,
        context_window: agent.model.context_window,
        io_input: agent.io?.inputs?.expects ?? undefined,
        io_output: agent.io?.outputs?.produces ?? undefined,
      },
    };
    await pace(DELAY_EVENT, signal);

    // Persona body comes as a paired tag with provenance.
    yield {
      event: 'agent-persona',
      data: {
        node_id: agent.id,
        persona: agent.persona,
        source: agent.anchors.persona.ref,
        tokens: agent.anchors.persona.tokens,
        cached: true,
      },
    };
    await pace(DELAY_EVENT, signal);

    // 5 substeps per agent. Each fires running → done with paced delay so the
    // front-end's auto-follow has time to anchor-scroll.
    for (const substep of SUBSTEPS) {
      if (signal.aborted) return;
      const { source, tokens } = substepSource(agent, substep);
      yield {
        event: 'agent-substep',
        data: {
          node_id: agent.id,
          substep,
          status: 'running',
          elapsed_ms: null,
          source,
          tokens,
          cached: true,
        },
      };
      await pace(DELAY_SUBSTEP, signal);
      yield {
        event: 'agent-substep',
        data: {
          node_id: agent.id,
          substep,
          status: 'done',
          elapsed_ms: DELAY_SUBSTEP,
          source,
          tokens,
          cached: true,
        },
      };
      agentStepElapsed += DELAY_SUBSTEP * 2;
    }
  }
  yield { event: 'assemble', data: { step: '配置 Agent 角色', step_index: 2, output_kind: 'nodes', status: 'done', elapsed_ms: agentStepElapsed } };

  // 2026-05-20 — 对齐 v3 设计稿 step 4-5：「设置工具集」+「Policy 协作规则」。
  // Step 3 = 设置工具集（这里 emit YAML blueprint，因为 YAML 已经含 tools 列）。
  // Step 4 = Policy 协作规则（emit edges + team.policy/mode/retry 摘要）。
  // 旧 "生成 YAML Blueprint" / "配置 Team Workflow" 是实现细节，不再做可见 step label。

  // Step 3 — 设置工具集 (carries YAML blueprint emission)
  yield { event: 'assemble', data: { step: '设置工具集', step_index: 3, output_kind: 'yaml', status: 'running', elapsed_ms: null } };
  await pace(DELAY_STEP, signal);
  const yamlText = synthesizeYaml(team);
  const yamlFilename = `${team.name.replace(/\./g, '-')}.yml`;
  yield {
    event: 'blueprint',
    data: {
      yaml: yamlText,
      filename: yamlFilename,
      artifact_type: 'yaml',
      artifact_url: `/projects/${sessionId}/${yamlFilename}`,
    },
  };
  const yamlLines = yamlText.split('\n');
  for (const line of yamlLines) {
    if (signal.aborted) return;
    yield { event: 'yaml-line', data: { line, total_lines: yamlLines.length } };
    await pace(20, signal);
  }
  yield { event: 'assemble', data: { step: '设置工具集', step_index: 3, output_kind: 'yaml', status: 'done', elapsed_ms: DELAY_STEP + yamlLines.length * 20 } };

  // Step 4 — Policy 协作规则 (edges + policy summary)
  yield { event: 'assemble', data: { step: 'Policy 协作规则', step_index: 4, output_kind: 'edges', status: 'running', elapsed_ms: null } };
  await pace(DELAY_STEP, signal);
  // policy summary text event so the UI can show "mode=serial · policy=strict · retry=3" in left pane
  yield {
    event: 'policy',
    data: {
      mode: team.mode ?? 'serial',
      policy: team.policy ?? 'strict',
      retry: team.retry ?? 3,
      edge_count: team.edges.length,
    },
  };
  for (const edge of team.edges) {
    if (signal.aborted) return;
    yield { event: 'edge', data: { from: edge.from, to: edge.to, status: 'active' } };
    await pace(DELAY_EVENT, signal);
  }
  yield { event: 'assemble', data: { step: 'Policy 协作规则', step_index: 4, output_kind: 'edges', status: 'done', elapsed_ms: DELAY_STEP + team.edges.length * DELAY_EVENT } };

  yield {
    event: 'complete',
    data: {
      session_id: sessionId,
      run_id: `run-${sessionId.slice(0, 8)}`,
      redirect: `/editor?session=${sessionId}`,
    },
  };
}

export default router;
