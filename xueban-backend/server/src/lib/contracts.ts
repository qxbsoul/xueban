/**
 * contracts.ts — Shared types for the intent-workflow pipeline (S0-S4).
 *
 * Pinned here so every collaborator on the stream (parser.ts, step-store.ts,
 * routes/run-sessions.ts, future Stream C intent-router.ts) imports from a
 * single source of truth instead of each side maintaining its own private
 * type alias. Anything cross-cutting goes here.
 *
 * See: docs/design/intent-workflow-design-v1.md §4.2 / §4.4.
 */

/**
 * What kind of artifact a single step is contracted to produce.
 *
 *   'nodes'     — sf:node tags (agent definitions)
 *   'edges'     — sf:edge tags (workflow connections)
 *   'yaml'      — an <artifact type="yaml"> block
 *   'classify'  — an sf:classify tag (intent metadata)
 *   'none'      — no observable artifact (analysis / planning step)
 *
 * Extending: skill authors will eventually be able to declare their own
 * output_kind via SKILL.md frontmatter (Story S5.1). Until then this union
 * stays closed — `parser.ts` only validates against these five literals.
 */
export type OutputKind = 'nodes' | 'edges' | 'yaml' | 'classify' | 'none';

/**
 * On-disk record for a single step's lifecycle + payload.
 *
 * Written by `step-store.ts` to
 *   <cwd>/.xueban/projects/<session_id>/steps/<step_index>.json
 *
 * Read by:
 *   - retry endpoint (S4.1) — needs 0..n-1 to replay context
 *   - resume endpoint (S4.2) — needs to find the last `status: 'done'`
 *
 * NOT a database row — we accept eventually-consistent disk writes (atomic
 * .tmp+rename) and tolerate partial reads on crash recovery.
 */
export interface StepArtifact {
  session_id: string;
  /** 0-based; matches the position in the skill's step sequence. */
  step_index: number;
  step_name: string;
  output_kind: OutputKind;
  /**
   * Free-form payload. Concrete shape varies by output_kind:
   *   'nodes'    → Array<{ node_id, type, title, ... }>
   *   'edges'    → Array<{ from, to }>
   *   'yaml'     → { filename, content }
   *   'classify' → { output_type, mode, confidence, complexity }
   *   'none'     → null (or any metadata the step wants to record)
   * Kept as `unknown` so the contract stays stable when payload schemas grow.
   */
  payload: unknown;
  /** ISO timestamp. */
  started_at: string;
  /** ISO timestamp; null while running. */
  finished_at: string | null;
  status: 'running' | 'done' | 'failed';
  /** Populated when status === 'failed'. */
  error?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// S6.10-A — TimelineMessage model
//
// The legacy SSE stream emits *fine-grained* events (node/edge/agent-substep/
// thinking-chunk/yaml-line/blueprint/classify/assemble/text/…). The front-end
// historically demultiplexes them into independent state slots and renders
// three fixed layouts (StepList / UserMessage / AgentDetail). That makes the
// UI feel like a static three-pane jigsaw.
//
// Trae / Cherry Studio / Codex / Claude Code all treat the run as a single
// ordered list of typed "messages": user_turn → thinking → assistant_meta →
// rationale → tool_call → step_panel → diff_panel → msg_foot → … the front-end
// then dispatches by `kind` to small per-kind renderers.
//
// To unlock that pattern without breaking the existing UI, the SSE handler
// keeps emitting *every* legacy event AND additionally emits two new events:
//   event: 'message'       → data is a full `TimelineMessage`
//   event: 'message-patch' → data is a `MessagePatch` that mutates an earlier
//                            long-lived message (step_panel grows, thinking
//                            streams body, msg_foot updates timer/cost, etc.)
//
// The projector that derives these from the legacy stream lives in
// `timeline-projector.ts`. Visual contract: docs/design/platform-v5/
// run-session-v8.html (timeline item kinds, ~lines 1568-1716).
// ─────────────────────────────────────────────────────────────────────────────

export type MessageKind =
  | 'user_turn'
  | 'thinking'
  | 'assistant_meta'
  | 'rationale'
  | 'tool_call'
  | 'tool_echo'
  // 2026-05-24 P0-1 — dedicated kind for the assistant's natural-language
  // answer body. Was previously crammed into tool_echo, one message per
  // chunk; now accumulates via `text_append` patches so a 1000-token answer
  // is one timeline row rather than 200 fragments.
  | 'assistant_text'
  // 2026-05-27 (T3, opendesign-streaming-architecture-study §5) — first-class
  // bucket for content that is NOT a recognized answer/tag: leaked SSE wire
  // frames, off-protocol blobs, raw CLI lines. Mirrors OpenDesign's `raw`
  // event kind. Routing such content here (instead of cramming it into
  // assistant_text) is what keeps the answer bubble clean — the additive
  // counterpart to the subtractive parser's "residue == text" leak.
  | 'raw'
  | 'step_panel'
  | 'diff_panel'
  | 'msg_foot'
  | 'status_line'
  // ── 2026-08-15 Polynoia-parity rich inline content ─────────────────────────
  //
  // The kinds above cover process narration (thinking/tool/step/foot). Real
  // IM-style collaboration needs *work artifacts* rendered inline in the chat:
  // the orchestrator's task plan, git commits, real file diffs, data tables,
  // images, live web previews, artifact references and verifier verdicts.
  // Each new kind is additive — old clients ignore unknown kinds, and the
  // front-end reducer already appends unknown-kind messages by id.
  | 'plan_card'
  | 'commit_record'
  | 'file_diff'
  | 'table_panel'
  | 'image_panel'
  | 'web_preview'
  | 'artifact_card'
  | 'verdict_card'
  // 2026-08-23 — WorkBuddy 式意图分发可视化：网关在 INTENT_PARSE 后补发一帧
  // 派发决策（选了哪个 team/agent、为什么）。additive；旧客户端按 id append 即可。
  | 'intent'
  // 2026-11-05 — 群聊 DAG 交接边实时播报（hop 生命周期可视化）：A→B 交接边
  // open→running→终态逐帧上屏，用户在聊天流里直接看到「谁的产出交给了谁、
  // 哪里断了」。additive；旧客户端按 id append 即可。
  | 'hop_card';

export interface TimelineMessageBase {
  /** ULID-ish stable id. Front-end uses this as React key + patch target. */
  id: string;
  kind: MessageKind;
  /**
   * All messages produced inside one user turn share this id. A new `user_turn`
   * opens the next turn; everything until the following `user_turn` belongs to
   * it. Convenient for grouping/anchoring without keeping turn boundaries on
   * the front-end side.
   */
  turn_id: string;
  /** epoch ms. */
  ts: number;
}

/**
 * A row inside a `step_panel`. Pending steps can be reported by the projector
 * (so the panel knows total progress) but the front-end is contracted to only
 * render `done` + `running` rows.
 */
export interface StepRow {
  name: string;
  status: 'done' | 'running' | 'pending';
  elapsed_ms?: number;
  substeps?: StepRow[];
}

export interface DiffLine {
  no: number;
  mark: '+' | '-' | ' ';
  code: string;
}

/**
 * One task row inside a `plan_card` — the orchestrator's decomposition of the
 * user goal into parallel-dispatchable units (Polynoia's "编排者拆任务派活").
 * Status transitions arrive as `plan_update_task` patches keyed by task_id.
 */
export interface PlanTaskRow {
  /** Stable id (e.g. `node:<node_id>`). Patch target for status updates. */
  id: string;
  title: string;
  /** Display name of the assigned agent (worker). */
  assignee?: string;
  /** Agent id true-source for deterministic attribution (U0 convention). */
  agent_id?: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'blocked';
  /** 1-based parallel batch/layer — same batch = dispatched concurrently. */
  batch?: number;
  elapsed_ms?: number;
  note?: string;
}

export interface CommitFileEntry {
  path: string;
  /** Git letter: A added / M modified / D deleted / R renamed. */
  status: string;
}

/** intent 卡里的一条派发明细（编排者拆出的 per-agent 子任务）。 */
export interface IntentAssignment {
  agent_id: string;
  /** 展示名（ roster 解析结果，可能等于 id）。 */
  name: string;
  /** 子任务标题（INTENT_PARSE 产物）。 */
  title: string;
}

export type TimelineMessage =
  | (TimelineMessageBase & {
      kind: 'user_turn';
      text: string;
    })
  | (TimelineMessageBase & {
      kind: 'thinking';
      label: string;
      tokens?: number;
      preview?: string;
      body?: string;
      status: 'streaming' | 'done';
      /** Thinking duration ms, set on finalize. Front-end shows "Thought for Xs". */
      duration_ms?: number;
      /**
       * 2026-11-06 Agent 检查器 — 群聊 fanout 下该过程卡属于哪个 agent。
       * additive：老数据/单聊不传 = 前端回落 label 前缀匹配。
       */
      agent_id?: string;
      agent_name?: string;
    })
  | (TimelineMessageBase & {
      kind: 'assistant_meta';
      model_brand: string;
      model_ver: string;
      identity: string;
      summary: string;
    })
  | (TimelineMessageBase & {
      kind: 'rationale';
      bullets: string[];
    })
  | (TimelineMessageBase & {
      kind: 'tool_call';
      name: string;
      args_summary: string;
      link?: { label: string; href: string };
      /** gap #2（2026-06-24）— per-tool 状态/耗时/结果摘要（对齐 Hermes 工具卡）。 */
      status?: 'running' | 'ok' | 'error';
      duration_ms?: number;
      result_summary?: string;
      /**
       * 2026-11-06 Agent 检查器（ZCode 后台活动式）— 群聊 fanout 下该工具调用
       * 属于哪个 agent。additive：单聊/老路径不传 = 无归属（检查器不筛它）。
       */
      agent_id?: string;
      agent_name?: string;
    })
  | (TimelineMessageBase & {
      kind: 'tool_echo';
      body: string;
      /** 2026-11-06 — 同 tool_call.agent_id（echo 与其 call 成对出现）。 */
      agent_id?: string;
      agent_name?: string;
    })
  | (TimelineMessageBase & {
      kind: 'assistant_text';
      body: string;
      /**
       * 2026-11-07 — 流式光标（镜像前端 types.ts assistant_text.streaming）：
       * text_append 置 true、text_finalize 置 false。仅 timeline-replay
       * applyPatch 回放路径读写；老客户端忽略该字段（additive）。
       */
      streaming?: boolean;
      /**
       * B2 (unified-conversation-core-plan-2026-06-11 §B2) — optional speaker
       * attribution for CHAT-originated messages (group MessageItem has
       * sender_name; run-originated timeline messages don't). Additive: the
       * front-end MessageRegistry ignores unknown fields, so old clients are
       * unaffected.
       */
      sender_name?: string;
      /** U0 绑角色（2026-06-23）— 发送者 agent_id 真源（sender_name 仅展示）。 */
      agent_id?: string;
    })
  | (TimelineMessageBase & {
      kind: 'raw';
      /** The unclassified content, verbatim. Rendered as a collapsed block. */
      body: string;
      /** Optional provenance hint (e.g. 'sse-frame-leak', 'unknown-tag'). */
      source?: string;
      /** B2 — optional speaker attribution (see assistant_text.sender_name). */
      sender_name?: string;
      /** U0 绑角色 — 发送者 agent_id（见 assistant_text.agent_id）。 */
      agent_id?: string;
    })
  | (TimelineMessageBase & {
      kind: 'step_panel';
      total_steps: number;
      steps: StepRow[];
    })
  | (TimelineMessageBase & {
      kind: 'diff_panel';
      filename: string;
      added: number;
      removed: number;
      lines: DiffLine[];
    })
  | (TimelineMessageBase & {
      kind: 'msg_foot';
      status: 'running' | 'done';
      elapsed_ms?: number;
      tools?: number;
      tokens?: number;
      cost_cny?: number;
    })
  | (TimelineMessageBase & {
      kind: 'status_line';
      verb: string;
      elapsed_s: number;
      tools_running: number;
      /**
       * True once the turn has reached a terminal state (onComplete). The
       * front-end freezes the elapsed-seconds ticker when set — `elapsed_s`
       * is the final duration, not a live baseline to drift from.
       */
      terminal?: boolean;
    })
  // ── Polynoia-parity rich inline content payloads ──────────────────────────
  | (TimelineMessageBase & {
      kind: 'plan_card';
      title: string;
      /** The user goal this plan decomposes. */
      goal?: string;
      tasks: PlanTaskRow[];
    })
  | (TimelineMessageBase & {
      kind: 'commit_record';
      repo: string;
      sha: string;
      short_sha: string;
      message: string;
      author: string;
      /** ISO date of the commit. */
      date?: string;
      files_changed: number;
      insertions: number;
      deletions: number;
      files?: CommitFileEntry[];
      /** Optional external link (e.g. web UI commit page). */
      url?: string;
      /** 2026-11-07 检查器产出归属 — 提交者 agent（additive；无归属 = 老数据 /
       *  编排者合并提交，检查器对无归属卡不进流）。 */
      agent_id?: string;
      agent_name?: string;
    })
  | (TimelineMessageBase & {
      kind: 'file_diff';
      path: string;
      /** Highlight language hint (e.g. 'ts', 'py'); optional. */
      language?: string;
      added: number;
      removed: number;
      lines: DiffLine[];
      /** 2026-11-07 检查器产出归属 — 同 commit_record.agent_id（与其成对出现）。 */
      agent_id?: string;
      agent_name?: string;
    })
  | (TimelineMessageBase & {
      kind: 'table_panel';
      caption?: string;
      columns: string[];
      rows: (string | number)[][];
    })
  | (TimelineMessageBase & {
      kind: 'image_panel';
      src: string;
      alt?: string;
      title?: string;
    })
  | (TimelineMessageBase & {
      kind: 'web_preview';
      title: string;
      /** URL to load in the sandboxed iframe (same-origin artifact paths ok). */
      url?: string;
      /** Inline HTML source alternative when no URL exists. */
      src?: string;
    })
  | (TimelineMessageBase & {
      kind: 'artifact_card';
      title?: string;
      /** 'yaml' | 'html' | 'markdown' today; open string for future kinds. */
      artifact_type: string;
      filename: string;
      /** Servable URL (e.g. /projects/<session>/artifact.html). */
      url?: string;
      note?: string;
    })
  | (TimelineMessageBase & {
      kind: 'verdict_card';
      title: string;
      /** Verifier/critique pass flag; omitted when no threshold semantics. */
      passed?: boolean;
      /** Aggregate score (verifier 0-1 probability or critique 0-10 avg). */
      score?: number;
      /** Score ceiling for display normalization (e.g. 10). */
      max_score?: number;
      summary?: string;
      metrics?: { label: string; value: string | number; detail?: string }[];
      notes?: string[];
    })
  | (TimelineMessageBase & {
      kind: 'intent';
      /** 用户原始意图（原文截断，≤120 字）。 */
      intent: string;
      /** 派发对象类型：team = 团队 DAG 派发；agent = 串行逐个派发。 */
      target_kind: 'team' | 'agent';
      target_name: string;
      /** 派发理由（LLM 拆解说明 / 降级原因），NeverRaising 侧已保证非空。 */
      reason: string;
      /** 可选：per-agent 子任务明细（仅 LLM 拆解成功时有）。 */
      assignments?: IntentAssignment[];
    })
  | (TimelineMessageBase & {
      kind: 'hop_card';
      /** 交接发起方 agent id（展示名由前端 agentById 解析，缺省回落 id）。 */
      from_agent: string;
      /** 接收方 agent id。 */
      to_agent: string;
      /** 展示名快照（open 时刻解析，避免前端查不到 agent 时裸露 id）。 */
      from_name: string;
      to_name: string;
      status: 'pending' | 'running' | 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted';
      /** 结算时上游产出的字符数（0 = 无产出）。 */
      output_chars?: number;
      /** 终态原因（错误信息 / denied 原因 / aborted 来源）。 */
      detail?: string;
      /** 结算耗时 ms（settle 时计算）。 */
      duration_ms?: number;
    });

/**
 * Patches let a long-lived message grow in place. They mutate the message
 * identified by `id` (which is always a message the front-end has previously
 * received).
 */
export type MessagePatch =
  | { id: string; op: 'append_step'; step: StepRow }
  | { id: string; op: 'update_step'; index: number; patch: Partial<StepRow> }
  | { id: string; op: 'append_substep'; step_index: number; sub: StepRow }
  | {
      id: string;
      op: 'update_substep';
      step_index: number;
      sub_index: number;
      patch: Partial<StepRow>;
    }
  | { id: string; op: 'thinking_append_body'; chunk: string }
  | { id: string; op: 'thinking_finalize'; tokens?: number; duration_ms?: number }
  // gap #2（2026-06-24）— tool_call 终态：tool_result 回来时落定 running → ok/error。
  | { id: string; op: 'tool_finalize'; status: 'ok' | 'error'; duration_ms?: number; result_summary?: string }
  // 2026-05-24 P0-1 — append a streamed chunk to an open assistant_text
  // message. The projector batches token-level deltas onto a single
  // assistant_text message until something orthogonal (tool call, thinking,
  // step transition) closes it.
  | { id: string; op: 'text_append'; chunk: string }
  // 2026-11-07 — 流式收口（前端 reducer 早有该 op，服务端契约补齐）：回放后
  // assistant_text 不再残留 streaming 光标。
  | { id: string; op: 'text_finalize' }
  | { id: string; op: 'diff_append_line'; line: DiffLine }
  | {
      id: string;
      op: 'msg_foot_update';
      patch: Partial<{
        status: 'running' | 'done';
        elapsed_ms: number;
        tools: number;
        tokens: number;
        cost_cny: number;
      }>;
    }
  // Polynoia-parity — live task status transitions inside an open plan_card.
  | { id: string; op: 'plan_update_task'; task_id: string; patch: Partial<PlanTaskRow> }
  // 2026-11-05 — hop_card 生命周期推进：running / 终态结算（open 已有全量帧）。
  | {
      id: string;
      op: 'hop_update';
      patch: Partial<{
        status: 'running' | 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted';
        output_chars: number;
        detail: string;
        duration_ms: number;
      }>;
    };

/**
 * What `TimelineProjector` returns from every callback. Either or both arrays
 * may be empty; the caller forwards them as SSE `message` / `message-patch`
 * events in order (messages first, then patches that depend on them).
 */
export interface ProjectorEmit {
  messages: TimelineMessage[];
  patches: MessagePatch[];
}
