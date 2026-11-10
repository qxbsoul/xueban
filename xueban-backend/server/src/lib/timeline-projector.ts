/**
 * timeline-projector.ts — Stream A → Stream B fanout
 *
 * Story S6.10-A. The SSE handler ships *fine-grained* events (`node` / `edge`
 * / `agent-substep` / `thinking-chunk` / `yaml-line` / `blueprint` / `classify`
 * / `assemble` / `text` / `complete`) so the existing front-end keeps working.
 * In parallel, we also need a Trae/Codex-style timeline: a single ordered list
 * of typed `TimelineMessage`s plus `MessagePatch`es that grow long-lived
 * messages (step_panel, thinking, diff_panel, msg_foot) in place.
 *
 * Rather than refactor every callsite, we projector the fine-grained stream
 * into the new model. The SSE handler instantiates one projector per session
 * and, for each legacy event yielded, calls the matching `on*` method. The
 * projector returns `{ messages, patches }` which the handler forwards as
 * additional `event: 'message'` / `event: 'message-patch'` SSE frames.
 *
 * Important behaviours (contract for downstream consumers):
 *
 *   • Every message has a stable `id` (ULID-ish, monotonically sortable). The
 *     front-end uses it as a React key and as the patch target.
 *   • One `step_panel` per turn. Its id is reused so subsequent legacy
 *     `assemble` / `agent-substep` events emit *patches* against the same
 *     message instead of inflating the timeline.
 *   • `thinking` messages stream: open with `status: 'streaming'`, accumulate
 *     body via `thinking_append_body` patches, then `thinking_finalize`
 *     flips them to `status: 'done'`.
 *   • `diff_panel` works the same way for YAML line streams: empty `lines[]`
 *     in the initial message + `diff_append_line` patches as each line lands.
 *   • `msg_foot` is the per-turn running status row. It is created lazily
 *     (first time we hear about *any* step) and patched on every step and on
 *     `onComplete()` (final `status: 'done'`).
 *   • Pending steps are NOT pre-allocated. Steps appear via `append_step`
 *     when their `assemble:running` lands; this matches the v3 design.
 *
 * The projector keeps NO global state across instances — each session
 * (`createTimelineProjector()`) is isolated and safe to GC when the SSE
 * connection ends.
 */

import type {
  CommitFileEntry,
  DiffLine,
  MessagePatch,
  ProjectorEmit,
  StepRow,
  TimelineMessage,
} from './contracts';

// ─── id generation ──────────────────────────────────────────────────────────
//
// Cherry/Codex-style timelines key off message id for diffing. We need ids
// that are:
//   1. Globally unique within a session (no Date.now() collisions on fast
//      synthetic streams that fire dozens of events per millisecond).
//   2. Monotonically increasing string-comparable so the front-end can sort
//      without parsing.
//   3. Cheap (this is a hot path — multiple per event).
//
// P3 fix (audit/thinking-transport-fix-plan-2026-05-27.md §2.6): message ids
// MUST be deterministic per (projector seed + emit order), NOT random. The SSE
// `/stream` handler creates a FRESH projector and re-runs the whole pipeline on
// every (re)connection; with random ids each reconnect re-emitted the user_turn
// (and everything else) under a new id, defeating the front-end's id-keyed
// dedup → the user message showed twice. With a stable `idSeed` (= session_id)
// + a per-projector monotonic counter, a reconnect reproduces the IDENTICAL id
// sequence, so the front-end OVERWRITES the prior run's messages by id instead
// of appending duplicates. The id factory now lives inside createTimelineProjector
// so each projector owns its own counter (no cross-instance global state).

function nowMs(): number {
  return Date.now();
}

/**
 * gap #2 — 工具结果摘要（对齐 Hermes 工具卡的 "3 files" / "527 elements"）。
 * 无逐工具语义解析，按通用启发：多行→"N 行"；JSON 数组→"N 项"；单行短→原文；否则字符数。
 */
export function summarizeToolResult(body: string): string | undefined {
  const b = (body ?? '').trim();
  if (!b) return undefined;
  // JSON 数组 → "N 项"
  if (b.startsWith('[')) {
    try {
      const arr = JSON.parse(b);
      if (Array.isArray(arr)) return `${arr.length} 项`;
    } catch { /* 非合法 JSON，落到行/字符摘要 */ }
  }
  const lines = b.split('\n').filter((l) => l.trim().length > 0);
  if (lines.length > 1) return `${lines.length} 行`;
  const one = lines[0] ?? b;
  if (one.length <= 48) return one;
  return `${one.length} 字符`;
}

// ─── projector ──────────────────────────────────────────────────────────────

export interface TimelineProjector {
  /** Open a new turn. Closes any in-flight thinking/foot from the previous turn. */
  onUserMessage(text: string): ProjectorEmit;
  /** Classify metadata → assistant_meta (only the first classify per turn). */
  onClassify(data: {
    output_type?: string;
    mode?: string;
    confidence?: number;
    complexity?: number;
    source?: string;
  }): ProjectorEmit;
  /** Open a step on the current turn's step_panel (creates panel on first call). */
  onAssembleStart(stepIndex: number, stepName: string): ProjectorEmit;
  /** Close a step (running → done). */
  onAssembleDone(stepIndex: number, elapsedMs: number): ProjectorEmit;
  /** Add a running substep under a step. */
  onAgentSubstepStart(nodeId: string, substep: string): ProjectorEmit;
  /** Mark a substep done. */
  onAgentSubstepDone(nodeId: string, substep: string, elapsedMs: number): ProjectorEmit;
  /** Stream a thinking chunk (opens a thinking message if none is open). */
  onThinkingChunk(chunk: string): ProjectorEmit;
  /** Blueprint event → open a diff_panel for the YAML artifact. */
  onBlueprint(data: { filename?: string; yaml?: string }): ProjectorEmit;
  /** Each yaml-line event appends to the open diff_panel. */
  onYamlLine(line: string): ProjectorEmit;
  /** Optional plain text assistant chunk → tool_echo (used rarely). */
  onText(text: string): ProjectorEmit;
  /**
   * T3: unclassified content (leaked SSE frames, off-protocol blobs, raw CLI
   * lines) → a `raw` message. Closes any open text/thinking first so raw never
   * merges into the answer. `source` is an optional provenance hint.
   */
  onRaw(text: string, source?: string): ProjectorEmit;
  /**
   * T3 tool chain — a tool invocation (CLI/ACP/parser `tool-use` event) → a
   * `tool_call` message. Closes open text/thinking first so the chip starts a
   * fresh block (the front-end Timeline groups a tool_call + following tool_echo
   * into one tool-group card).
   */
  onToolUse(name: string, input: unknown): ProjectorEmit;
  /** Tool result (`tool-result` event) → a `tool_echo` continuation line. */
  onToolResult(output: string): ProjectorEmit;
  /**
   * T3 usage chain — token usage frame (`usage` event from any executor) →
   * accumulate input+output tokens onto the turn's msg_foot. Idempotent-ish:
   * each frame adds its tokens to the running total.
   */
  onUsage(usage: { input_tokens?: number; output_tokens?: number; total_tokens?: number }): ProjectorEmit;
  /** Final event → close thinking, finalize msg_foot. */
  onComplete(): ProjectorEmit;

  // ── Polynoia-parity rich content emitters (2026-08-15) ────────────────────
  //
  // Structured work artifacts the orchestrator/verifier/git layer pushes into
  // the chat timeline. All are thin: close the open answer/thinking runs so
  // the card starts a fresh block, then emit one message. The plan card is
  // the only stateful one — its id is remembered so onPlanTaskUpdate can
  // target it with `plan_update_task` patches as workers start/finish.

  /** Orchestrator task decomposition → plan_card (live-updated afterwards). */
  onPlanCard(data: {
    title?: string;
    goal?: string;
    tasks: import('./contracts').PlanTaskRow[];
  }): ProjectorEmit;
  /** Task status transition inside the current turn's plan_card. */
  onPlanTaskUpdate(
    taskId: string,
    patch: Partial<import('./contracts').PlanTaskRow>,
  ): ProjectorEmit;
  /** Data table (verifier metrics, file stats, comparisons…). */
  onTablePanel(data: {
    caption?: string;
    columns: string[];
    rows: (string | number)[][];
  }): ProjectorEmit;
  /** Inline image. */
  onImagePanel(data: { src: string; alt?: string; title?: string }): ProjectorEmit;
  /** Sandboxed live web/artifact preview card. */
  onWebPreview(data: { title: string; url?: string; src?: string }): ProjectorEmit;
  /** Persisted artifact reference card. */
  onArtifactCard(data: {
    title?: string;
    artifact_type: string;
    filename: string;
    url?: string;
    note?: string;
  }): ProjectorEmit;
  /** Git commit record. */
  onCommitRecord(data: {
    repo: string;
    sha: string;
    short_sha: string;
    message: string;
    author: string;
    date?: string;
    files_changed: number;
    insertions: number;
    deletions: number;
    files?: CommitFileEntry[];
    url?: string;
  }): ProjectorEmit;
  /** Real file diff (unified-diff lines), distinct from the YAML diff_panel. */
  onFileDiff(data: {
    path: string;
    language?: string;
    added: number;
    removed: number;
    lines: DiffLine[];
  }): ProjectorEmit;
  /** Verifier / critique verdict card. */
  onVerdictCard(data: {
    title: string;
    passed?: boolean;
    score?: number;
    max_score?: number;
    summary?: string;
    metrics?: { label: string; value: string | number; detail?: string }[];
    notes?: string[];
  }): ProjectorEmit;

  /**
   * O3 — back-trace bridge. Returns the id of the most recent `assistant_text`
   * message created during the current turn (the row that holds the streamed
   * answer prose), or null if no assistant_text was opened this turn. Unlike the
   * internal `openAssistantTextId` (which is cleared whenever a non-text event
   * breaks the run), this remembers the LAST assistant_text id for the turn so
   * the stream handler can stamp it onto the persisted conversation row after
   * the generator drains. Pure getter — no state mutation, no emit.
   */
  lastAssistantTextId(): string | null;

  /** Test helper — internal state snapshot (kept tiny). */
  _debug(): {
    turnId: string;
    stepPanelId: string | null;
    msgFootId: string | null;
    openThinkingId: string | null;
    openDiffPanelId: string | null;
    stepIdToIndex: Record<number, number>;
  };
}

export function createTimelineProjector(
  opts?: { idSeed?: string },
): TimelineProjector {
  // P3: deterministic per-projector id factory. `idSeed` (= session_id from the
  // stream handler) makes ids stable across SSE reconnects so the front-end
  // dedups by id; absent a seed (legacy callers / unit tests) we fall back to a
  // random per-projector seed so two live projectors never collide.
  const idSeed = opts?.idSeed ?? `r${Math.random().toString(36).slice(2, 10)}`;
  let idCounter = 0;
  const newId = (prefix: string): string =>
    `${prefix}_${idSeed}_${(idCounter++).toString(36).padStart(4, '0')}`;

  // Per-turn state. Reset whenever onUserMessage opens a fresh turn.
  let turnId = newId('turn');
  let turnStartMs = nowMs();
  // T3 usage chain: running token total for the current turn (input+output),
  // surfaced on the msg_foot. Reset by onUserMessage.
  let totalTokens = 0;
  let stepPanelId: string | null = null;
  /** Index in the panel's `steps[]` keyed by the legacy step_index. */
  const stepIndexLookup = new Map<number, number>();
  /**
   * Running snapshot of the step_panel steps. We keep it server-side so
   * `onAgentSubstepStart` / `Done` can look up the right substep array index
   * without the front-end having to echo back state.
   */
  let stepPanelSteps: StepRow[] = [];
  /** Total step count. Inferred lazily — bumps when we see a higher index. */
  let totalSteps = 0;

  let msgFootId: string | null = null;
  let toolsRun = 0;
  /**
   * answer 模式（无 assemble/step 事件）下 complete 可能先于 usage 到达：
   * onComplete 时 msgFoot 尚未创建，done 落定被跳过，随后 onUsage 的
   * ensureMsgFoot 会把 foot 建成 'running' 并永远卡住。记录 completed，
   * 让迟到创建的 foot 直接以 'done' 出生。
   */
  let completed = false;

  let openThinkingId: string | null = null;
  let thinkingBuf = '';
  let thinkingStartMs = 0;

  // 2026-05-24 P0-1 — currently-accumulating assistant_text message id.
  // Set on the first onText() of a contiguous text run; cleared by any
  // non-text projector callback (tool/substep/thinking/blueprint/complete)
  // so the next text run opens a new message. This batches token deltas
  // into one timeline row rather than the previous one-per-chunk explosion
  // (a 1000-token answer was rendering as 200 separate `tool_echo`s).
  let openAssistantTextId: string | null = null;

  // O3 — sticky copy of the last assistant_text id created this turn. Unlike
  // openAssistantTextId (nulled by closeOpenText on any non-text event), this
  // survives so lastAssistantTextId() can report it after the run drains. Reset
  // per turn by onUserMessage.
  let lastAssistantTextId: string | null = null;

  // 2026-05-24 P0-3 — single status_line slot id (always-on bottom bar).
  // Lazily created on the first bumpStatusLine() call; survives across
  // turns (the front-end keys this slot off message kind 'status_line', so
  // re-emitting the same id keeps the same row updated rather than pushing
  // duplicates).
  let statusLineId: string | null = null;

  let openDiffPanelId: string | null = null;
  let diffLineNo = 0;
  let diffAdded = 0;

  // Polynoia-parity — id of the current turn's plan_card (null until the
  // orchestrator decomposition lands). onPlanTaskUpdate targets it.
  let planCardId: string | null = null;

  // gap #2 — 当前未落定的 tool_call（等 onToolResult 来 finalize）+ 其开始时间。
  // 顺序配对启发：CLI tool_use 后紧跟 tool_result，按"最近未落定"配。
  let openToolCallId: string | null = null;
  let toolStartMs = 0;

  // Substep tracking — outer key is nodeId. Each node maps to a `step_index`
  // (which step_panel row owns it) and the array of substeps it currently
  // holds. The first running substep for an unknown node anchors it to the
  // currently-running step.
  interface NodeState {
    stepIndex: number;
    subs: StepRow[];
    subIndexByName: Map<string, number>;
  }
  const nodeMap = new Map<string, NodeState>();
  let currentRunningStepIndex: number | null = null;

  function ensureMsgFoot(out: ProjectorEmit) {
    if (msgFootId) return;
    msgFootId = newId('msg');
    out.messages.push({
      id: msgFootId,
      kind: 'msg_foot',
      turn_id: turnId,
      ts: nowMs(),
      status: completed ? 'done' : 'running',
      elapsed_ms: 0,
      tools: 0,
    });
  }

  function bumpMsgFoot(out: ProjectorEmit, patch: { tools?: number; status?: 'running' | 'done'; tokens?: number }) {
    if (!msgFootId) return;
    const merged: { elapsed_ms: number; tools?: number; status?: 'running' | 'done'; tokens?: number } = {
      elapsed_ms: nowMs() - turnStartMs,
    };
    if (patch.tools !== undefined) merged.tools = patch.tools;
    if (patch.status !== undefined) merged.status = patch.status;
    if (patch.tokens !== undefined) merged.tokens = patch.tokens;
    out.patches.push({
      id: msgFootId,
      op: 'msg_foot_update',
      patch: merged,
    });
  }

  function ensureStepPanel(out: ProjectorEmit) {
    if (stepPanelId) return;
    stepPanelId = newId('msg');
    stepPanelSteps = [];
    out.messages.push({
      id: stepPanelId,
      kind: 'step_panel',
      turn_id: turnId,
      ts: nowMs(),
      total_steps: 0,
      steps: [],
    });
  }

  function closeOpenThinking(out: ProjectorEmit) {
    if (!openThinkingId) return;
    out.patches.push({
      id: openThinkingId,
      op: 'thinking_finalize',
      tokens: Math.max(1, Math.round(thinkingBuf.length / 4)),
      duration_ms: thinkingStartMs > 0 ? nowMs() - thinkingStartMs : undefined,
    });
    openThinkingId = null;
    thinkingBuf = '';
    thinkingStartMs = 0;
  }

  /**
   * Close the currently-accumulating assistant_text message so the next
   * onText() call opens a fresh one. Called whenever a semantically
   * different event (tool call/result, agent substep, blueprint, thinking,
   * step transition, complete) breaks the contiguous text run.
   *
   * The current implementation only nulls the id — the message's body has
   * already been built up via text_append patches and the front-end has it.
   * Future work could emit a `text_finalize` patch if downstream needs to
   * know "stream done" without inferring it from the next non-text event.
   */
  function closeOpenText(_out: ProjectorEmit) {
    // _out reserved for a future text_finalize patch; for now closing is
    // purely state — body already streamed and rendered by the front-end.
    void _out;
    openAssistantTextId = null;
  }

  /**
   * Update the always-on status_line slot. The id is FIXED so the front-end
   * can render exactly one bottom bar regardless of how many patches we
   * emit. First call pushes the message; subsequent calls replace it (the
   * front-end MessageRegistry-by-id collation overwrites the previous
   * status_line in place).
   *
   * We re-emit the full message rather than a patch op so the design's
   * "stable bottom slot keyed by kind" contract works without a new patch
   * shape — status_line is a singleton, not a growable container.
   */
  function bumpStatusLine(
    out: ProjectorEmit,
    verb: string,
    toolsRunning = 0,
    terminal = false,
  ) {
    // Each emission gets a fresh id but the same kind. The front-end's
    // MessageRegistry filters `status_line` out of the timeline stream and
    // renders only the latest one in a fixed bottom slot (StatusLine.tsx),
    // so duplicates don't accumulate visually. Using fresh ids keeps the
    // per-message id-uniqueness invariant intact.
    statusLineId = newId('msg');
    out.messages.push({
      id: statusLineId,
      kind: 'status_line',
      turn_id: turnId,
      ts: nowMs(),
      verb,
      elapsed_s: Math.max(0, Math.round((nowMs() - turnStartMs) / 1000)),
      tools_running: toolsRunning,
      terminal,
    });
  }

  function emit(): ProjectorEmit {
    return { messages: [], patches: [] };
  }

  return {
    onUserMessage(text: string): ProjectorEmit {
      const out = emit();
      // Close anything open from a prior turn before opening a new one.
      if (msgFootId) bumpMsgFoot(out, { status: 'done' });
      closeOpenThinking(out);
      closeOpenText(out);
      // Reset turn-scoped state.
      turnId = newId('turn');
      turnStartMs = nowMs();
      totalTokens = 0;
      stepPanelId = null;
      stepPanelSteps = [];
      stepIndexLookup.clear();
      totalSteps = 0;
      msgFootId = null;
      toolsRun = 0;
      completed = false;
      openDiffPanelId = null;
      diffLineNo = 0;
      diffAdded = 0;
      nodeMap.clear();
      currentRunningStepIndex = null;
      openAssistantTextId = null;
      lastAssistantTextId = null; // O3 — reset back-trace anchor per turn
      planCardId = null;
      out.messages.push({
        id: newId('msg'),
        kind: 'user_turn',
        turn_id: turnId,
        ts: nowMs(),
        text,
      });
      // Reset statusline to a neutral state on a fresh turn.
      bumpStatusLine(out, 'Thinking', 0);
      return out;
    },

    onClassify(data): ProjectorEmit {
      const out = emit();
      // Only the *first* classify per turn becomes assistant_meta; subsequent
      // classifies (e.g. the LLM-emitted one after the TS-emitted one) are
      // ignored at this layer — front-end still sees them on legacy stream.
      // We tag by msgFootId presence: if msg_foot doesn't exist yet, we're
      // first in the turn.
      if (msgFootId) return out;
      const id = newId('msg');
      const brand = 'Claude';
      const ver = data.mode ?? 'team';
      const identity = data.mode === 'team' ? '已识别 Team 模式' : `mode=${data.mode ?? 'task'}`;
      const summary = `output=${data.output_type ?? '?'} · confidence=${(data.confidence ?? 0).toFixed(2)} · complexity=${data.complexity ?? '?'}`;
      out.messages.push({
        id,
        kind: 'assistant_meta',
        turn_id: turnId,
        ts: nowMs(),
        model_brand: brand,
        model_ver: ver,
        identity,
        summary,
      });
      ensureMsgFoot(out);
      return out;
    },

    onAssembleStart(stepIndex: number, stepName: string): ProjectorEmit {
      const out = emit();
      // A new step boundary closes any in-flight assistant_text run so the
      // next free-text chunk opens a fresh message under the new step.
      closeOpenText(out);
      ensureMsgFoot(out);
      ensureStepPanel(out);
      const panelId = stepPanelId!;
      // Already seen? (idempotent — should not happen but defensive)
      if (stepIndexLookup.has(stepIndex)) {
        const idx = stepIndexLookup.get(stepIndex)!;
        out.patches.push({
          id: panelId,
          op: 'update_step',
          index: idx,
          patch: { status: 'running', name: stepName },
        });
        currentRunningStepIndex = stepIndex;
        bumpStatusLine(out, `Configuring · ${stepName}`, 0);
        return out;
      }
      const newRow: StepRow = { name: stepName, status: 'running', substeps: [] };
      stepPanelSteps.push(newRow);
      const localIdx = stepPanelSteps.length - 1;
      stepIndexLookup.set(stepIndex, localIdx);
      if (stepIndex + 1 > totalSteps) totalSteps = stepIndex + 1;
      out.patches.push({ id: panelId, op: 'append_step', step: newRow });
      currentRunningStepIndex = stepIndex;
      bumpStatusLine(out, `Configuring · ${stepName}`, 0);
      return out;
    },

    onAssembleDone(stepIndex: number, elapsedMs: number): ProjectorEmit {
      const out = emit();
      if (!stepPanelId) return out;
      const localIdx = stepIndexLookup.get(stepIndex);
      if (localIdx === undefined) return out;
      stepPanelSteps[localIdx].status = 'done';
      stepPanelSteps[localIdx].elapsed_ms = elapsedMs;
      out.patches.push({
        id: stepPanelId,
        op: 'update_step',
        index: localIdx,
        patch: { status: 'done', elapsed_ms: elapsedMs },
      });
      if (currentRunningStepIndex === stepIndex) currentRunningStepIndex = null;
      bumpMsgFoot(out, {});
      return out;
    },

    onAgentSubstepStart(nodeId: string, substep: string): ProjectorEmit {
      const out = emit();
      // Substep boundary closes any in-flight assistant_text run.
      closeOpenText(out);
      if (!stepPanelId || currentRunningStepIndex === null) return out;
      const stepLocalIdx = stepIndexLookup.get(currentRunningStepIndex);
      if (stepLocalIdx === undefined) return out;
      let nodeState = nodeMap.get(nodeId);
      if (!nodeState) {
        nodeState = {
          stepIndex: currentRunningStepIndex,
          subs: [],
          subIndexByName: new Map(),
        };
        nodeMap.set(nodeId, nodeState);
      }
      // Anchor substep name by `nodeId/substep` so the same substep firing
      // twice (running → done) updates instead of duplicating.
      const subName = `${nodeId} · ${substep}`;
      if (nodeState.subIndexByName.has(subName)) return out;
      const sub: StepRow = { name: subName, status: 'running' };
      nodeState.subs.push(sub);
      nodeState.subIndexByName.set(subName, nodeState.subs.length - 1);
      const targetSteps = stepPanelSteps[stepLocalIdx];
      if (!targetSteps.substeps) targetSteps.substeps = [];
      targetSteps.substeps.push(sub);
      out.patches.push({
        id: stepPanelId,
        op: 'append_substep',
        step_index: stepLocalIdx,
        sub,
      });
      bumpStatusLine(out, `${nodeId} · ${substep}`, 0);
      return out;
    },

    onAgentSubstepDone(nodeId: string, substep: string, elapsedMs: number): ProjectorEmit {
      const out = emit();
      if (!stepPanelId) return out;
      const nodeState = nodeMap.get(nodeId);
      if (!nodeState) return out;
      const stepLocalIdx = stepIndexLookup.get(nodeState.stepIndex);
      if (stepLocalIdx === undefined) return out;
      const subName = `${nodeId} · ${substep}`;
      const subLocalIdx = nodeState.subIndexByName.get(subName);
      if (subLocalIdx === undefined) return out;
      nodeState.subs[subLocalIdx].status = 'done';
      nodeState.subs[subLocalIdx].elapsed_ms = elapsedMs;
      out.patches.push({
        id: stepPanelId,
        op: 'update_substep',
        step_index: stepLocalIdx,
        sub_index: subLocalIdx,
        patch: { status: 'done', elapsed_ms: elapsedMs },
      });
      return out;
    },

    onThinkingChunk(chunk: string): ProjectorEmit {
      const out = emit();
      // Thinking interrupts a text run — close it so the next text chunk
      // opens a new message AFTER the thinking block.
      closeOpenText(out);
      if (!openThinkingId) {
        openThinkingId = newId('msg');
        thinkingBuf = '';
        thinkingStartMs = nowMs();
        out.messages.push({
          id: openThinkingId,
          kind: 'thinking',
          turn_id: turnId,
          ts: nowMs(),
          label: 'Thinking',
          body: '',
          status: 'streaming',
        });
      }
      thinkingBuf += chunk;
      out.patches.push({
        id: openThinkingId,
        op: 'thinking_append_body',
        chunk,
      });
      bumpStatusLine(out, 'Thinking', 0);
      return out;
    },

    onBlueprint(data): ProjectorEmit {
      const out = emit();
      ensureMsgFoot(out);
      // Close any open thinking first — blueprint marks the transition from
      // analysis to artifact production.
      closeOpenThinking(out);
      closeOpenText(out);
      bumpStatusLine(
        out,
        `Editing · ${data.filename ?? 'output.yml'}`,
        toolsRun + 1,
      );
      openDiffPanelId = newId('msg');
      diffLineNo = 0;
      diffAdded = 0;
      out.messages.push({
        id: openDiffPanelId,
        kind: 'diff_panel',
        turn_id: turnId,
        ts: nowMs(),
        filename: data.filename ?? 'output.yml',
        added: 0,
        removed: 0,
        lines: [],
      });
      toolsRun += 1;
      bumpMsgFoot(out, { tools: toolsRun });
      return out;
    },

    onYamlLine(line: string): ProjectorEmit {
      const out = emit();
      if (!openDiffPanelId) return out;
      diffLineNo += 1;
      diffAdded += 1;
      const dLine: DiffLine = { no: diffLineNo, mark: '+', code: line };
      out.patches.push({
        id: openDiffPanelId,
        op: 'diff_append_line',
        line: dLine,
      });
      return out;
    },

    onText(text: string): ProjectorEmit {
      const out = emit();
      if (!text.trim()) return out;
      // 2026-05-24 P0-1 — accumulate contiguous text-delta chunks onto a
      // single assistant_text message. The first chunk pushes the message;
      // subsequent chunks emit text_append patches that the front-end
      // applies in place. Any non-text event (tool/substep/blueprint/...)
      // calls closeOpenText() so the next text run opens a fresh message.
      if (!openAssistantTextId) {
        openAssistantTextId = newId('msg');
        lastAssistantTextId = openAssistantTextId; // O3 sticky back-trace anchor
        out.messages.push({
          id: openAssistantTextId,
          kind: 'assistant_text',
          turn_id: turnId,
          ts: nowMs(),
          body: text,
        });
      } else {
        out.patches.push({
          id: openAssistantTextId,
          op: 'text_append',
          chunk: text,
        });
      }
      // While text is streaming the status line reads "Writing".
      bumpStatusLine(out, 'Writing', 0);
      return out;
    },

    onRaw(text: string, source?: string): ProjectorEmit {
      const out = emit();
      if (!text.trim()) return out;
      // Raw content is its own block — never let it bleed into the open answer
      // text or thinking. Close them first, then push a standalone raw message.
      closeOpenThinking(out);
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'raw',
        turn_id: turnId,
        ts: nowMs(),
        body: text,
        ...(source ? { source } : {}),
      });
      return out;
    },

    onToolUse(name: string, input: unknown): ProjectorEmit {
      const out = emit();
      ensureMsgFoot(out);
      // A tool call starts a fresh block — close open answer text/thinking so
      // the front-end's tool-group card (tool_call + following tool_echo)
      // renders standalone rather than inside the prose.
      closeOpenThinking(out);
      closeOpenText(out);
      let argsSummary = '';
      try {
        argsSummary = typeof input === 'string' ? input : JSON.stringify(input ?? {});
      } catch {
        argsSummary = String(input);
      }
      if (argsSummary.length > 200) argsSummary = argsSummary.slice(0, 200) + '…';
      // gap #2 — 上一个 tool 若没收到 result（无 tool_result 事件）就被新 tool 顶替，
      // 先把它落定为 ok（避免永远转圈）。
      if (openToolCallId) {
        out.patches.push({ id: openToolCallId, op: 'tool_finalize', status: 'ok', duration_ms: Math.max(0, nowMs() - toolStartMs) });
        openToolCallId = null;
      }
      // tool_call 以 status:'running' 起，记开始时间与 id；等 onToolResult 落定。
      const toolCallId = newId('msg');
      openToolCallId = toolCallId;
      toolStartMs = nowMs();
      out.messages.push({
        id: toolCallId,
        kind: 'tool_call',
        turn_id: turnId,
        ts: nowMs(),
        name,
        args_summary: argsSummary,
        status: 'running',
      });
      toolsRun += 1;
      bumpStatusLine(out, `Running · ${name}`, toolsRun);
      bumpMsgFoot(out, { tools: toolsRun });
      return out;
    },

    onToolResult(output: string): ProjectorEmit {
      const out = emit();
      const body = (output ?? '').trim();
      // gap #2 — 落定最近未结的 tool_call：耗时 + 成功/失败 + 结果摘要。
      if (openToolCallId) {
        const isError = /^\s*(error|exception|traceback|failed|enoent|permission denied)/i.test(body)
          || /\b(error|failed)\b/i.test(body.slice(0, 80)) && body.length < 200;
        out.patches.push({
          id: openToolCallId,
          op: 'tool_finalize',
          status: isError ? 'error' : 'ok',
          duration_ms: Math.max(0, nowMs() - toolStartMs),
          result_summary: summarizeToolResult(body),
        });
        openToolCallId = null;
      }
      if (!body) return out;
      out.messages.push({
        id: newId('msg'),
        kind: 'tool_echo',
        turn_id: turnId,
        ts: nowMs(),
        body,
      });
      return out;
    },

    onUsage(usage): ProjectorEmit {
      const out = emit();
      ensureMsgFoot(out);
      const add =
        (typeof usage.total_tokens === 'number' ? usage.total_tokens : 0) ||
        (typeof usage.input_tokens === 'number' ? usage.input_tokens : 0) +
          (typeof usage.output_tokens === 'number' ? usage.output_tokens : 0);
      if (add > 0) {
        totalTokens += add;
        bumpMsgFoot(out, { tokens: totalTokens });
      }
      return out;
    },

    onComplete(): ProjectorEmit {
      const out = emit();
      completed = true;
      closeOpenThinking(out);
      closeOpenText(out);
      // gap #2 — 收尾落定任何仍在 running 的 tool_call。
      if (openToolCallId) {
        out.patches.push({ id: openToolCallId, op: 'tool_finalize', status: 'ok', duration_ms: Math.max(0, nowMs() - toolStartMs) });
        openToolCallId = null;
      }
      if (msgFootId) {
        bumpMsgFoot(out, { status: 'done' });
      }
      bumpStatusLine(out, 'Done', 0, true);
      return out;
    },

    // ── Polynoia-parity rich content emitters ──────────────────────────────

    onPlanCard(data): ProjectorEmit {
      const out = emit();
      closeOpenThinking(out);
      closeOpenText(out);
      ensureMsgFoot(out);
      planCardId = newId('msg');
      out.messages.push({
        id: planCardId,
        kind: 'plan_card',
        turn_id: turnId,
        ts: nowMs(),
        title: data.title ?? '执行计划',
        ...(data.goal ? { goal: data.goal } : {}),
        tasks: data.tasks,
      });
      bumpStatusLine(out, 'Dispatching plan', 0);
      return out;
    },

    onPlanTaskUpdate(taskId, patch): ProjectorEmit {
      const out = emit();
      if (!planCardId) return out;
      out.patches.push({
        id: planCardId,
        op: 'plan_update_task',
        task_id: taskId,
        patch,
      });
      return out;
    },

    onTablePanel(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'table_panel',
        turn_id: turnId,
        ts: nowMs(),
        columns: data.columns,
        rows: data.rows,
        ...(data.caption ? { caption: data.caption } : {}),
      });
      return out;
    },

    onImagePanel(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'image_panel',
        turn_id: turnId,
        ts: nowMs(),
        src: data.src,
        ...(data.alt ? { alt: data.alt } : {}),
        ...(data.title ? { title: data.title } : {}),
      });
      return out;
    },

    onWebPreview(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'web_preview',
        turn_id: turnId,
        ts: nowMs(),
        title: data.title,
        ...(data.url ? { url: data.url } : {}),
        ...(data.src ? { src: data.src } : {}),
      });
      return out;
    },

    onArtifactCard(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'artifact_card',
        turn_id: turnId,
        ts: nowMs(),
        artifact_type: data.artifact_type,
        filename: data.filename,
        ...(data.title ? { title: data.title } : {}),
        ...(data.url ? { url: data.url } : {}),
        ...(data.note ? { note: data.note } : {}),
      });
      return out;
    },

    onCommitRecord(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'commit_record',
        turn_id: turnId,
        ts: nowMs(),
        repo: data.repo,
        sha: data.sha,
        short_sha: data.short_sha,
        message: data.message,
        author: data.author,
        files_changed: data.files_changed,
        insertions: data.insertions,
        deletions: data.deletions,
        ...(data.date ? { date: data.date } : {}),
        ...(data.files ? { files: data.files } : {}),
        ...(data.url ? { url: data.url } : {}),
      });
      return out;
    },

    onFileDiff(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'file_diff',
        turn_id: turnId,
        ts: nowMs(),
        path: data.path,
        added: data.added,
        removed: data.removed,
        lines: data.lines,
        ...(data.language ? { language: data.language } : {}),
      });
      return out;
    },

    onVerdictCard(data): ProjectorEmit {
      const out = emit();
      closeOpenText(out);
      out.messages.push({
        id: newId('msg'),
        kind: 'verdict_card',
        turn_id: turnId,
        ts: nowMs(),
        title: data.title,
        ...(data.passed === undefined ? {} : { passed: data.passed }),
        ...(data.score === undefined ? {} : { score: data.score }),
        ...(data.max_score === undefined ? {} : { max_score: data.max_score }),
        ...(data.summary ? { summary: data.summary } : {}),
        ...(data.metrics ? { metrics: data.metrics } : {}),
        ...(data.notes ? { notes: data.notes } : {}),
      });
      return out;
    },

    lastAssistantTextId(): string | null {
      return lastAssistantTextId;
    },

    _debug() {
      const stepIdToIndex: Record<number, number> = {};
      stepIndexLookup.forEach((v, k) => {
        stepIdToIndex[k] = v;
      });
      return {
        turnId,
        stepPanelId,
        msgFootId,
        openThinkingId,
        openDiffPanelId,
        stepIdToIndex,
      };
    },
  };
}
