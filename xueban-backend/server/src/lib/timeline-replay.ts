/**
 * timeline-replay.ts — B2 统一消息读路径 (unified-conversation-core-plan-2026-06-11 §B2).
 *
 * Two pure building blocks for `GET /api/groups/:id/timeline` (groups-chat.ts):
 *
 *   1. `applyPatch(msg, patch)` — server-side port of the front-end reducer
 *      (src/components/run-session/timeline/types.ts `applyPatch`). The run
 *      event log persists `message` + `message-patch` events verbatim
 *      (run-event-bus / run-event-store); to serve a run's history as plain
 *      TimelineMessage[] over a REST read path we must replay those patches
 *      server-side. Kept byte-for-byte equivalent in semantics: immutable,
 *      no-op on kind-mismatched patches (defensive, never throws).
 *
 *   2. `replayTimeline(events)` — folds a run's buffered event records into the
 *      final TimelineMessage[]: collects `message` payloads in order, applies
 *      each `message-patch` to its target (unknown target ids are ignored —
 *      the ring buffer may have dropped the original message). All other event
 *      kinds (node/edge/text/usage/complete/error/…, the legacy fine-grained
 *      stream) are skipped: the projector already mirrored everything relevant
 *      into `message`/`message-patch`.
 *
 *   3. `chatMessagesToTimeline(messages)` — adapts the Python group chat
 *      MessageItem records (sender_kind: user/agent/system) into
 *      TimelineMessage so chat history and run history merge into one list:
 *        user   → kind 'user_turn'  (text=content); opens a new turn — its
 *                 synthetic id `chat_<message_id>` becomes the turn_id shared
 *                 by every following agent/system message until the next user.
 *        agent  → kind 'assistant_text' (body=content, sender_name kept)
 *        system → kind 'raw' (body=content, source='chat-gateway')
 *      ids are stable across calls (`chat_<message_id>`), so the front-end can
 *      use them as React keys / dedup anchors.
 *
 * Pure functions only — no fs/HTTP/Express — so they're unit-testable the same
 * way as resolveStreamDisposition (run-sessions-stream-disposition.test.ts).
 */

import type { MessagePatch, TimelineMessage, StepRow } from './contracts';

/**
 * Apply one MessagePatch to its TimelineMessage. Returns a NEW message object
 * (immutability) or the original when the op doesn't match the message kind
 * (defensive no-op — mirrors the front-end reducer contract).
 */
export function applyPatch(msg: TimelineMessage, patch: MessagePatch): TimelineMessage {
  switch (patch.op) {
    case 'text_append': {
      if (msg.kind !== 'assistant_text') return msg;
      return { ...msg, body: msg.body + patch.chunk, streaming: true };
    }
    // 2026-11-07 — 流式收口 + hop 生命周期此前缺失（与前端 reducer 对齐）：
    // 回放后 assistant_text 不残留 streaming 光标、hop_card 不再永卡「待交接」。
    case 'text_finalize': {
      if (msg.kind !== 'assistant_text') return msg;
      return { ...msg, streaming: false };
    }
    case 'append_step': {
      if (msg.kind !== 'step_panel') return msg;
      return { ...msg, steps: [...msg.steps, patch.step] };
    }
    case 'update_step': {
      if (msg.kind !== 'step_panel') return msg;
      const steps = msg.steps.map((s, i) => (i === patch.index ? { ...s, ...patch.patch } : s));
      return { ...msg, steps };
    }
    case 'append_substep': {
      if (msg.kind !== 'step_panel') return msg;
      const steps = msg.steps.map((s, i) => {
        if (i !== patch.step_index) return s;
        return { ...s, substeps: [...(s.substeps ?? []), patch.sub] };
      });
      return { ...msg, steps };
    }
    case 'update_substep': {
      if (msg.kind !== 'step_panel') return msg;
      const steps = msg.steps.map((s, i) => {
        if (i !== patch.step_index) return s;
        const subs: StepRow[] = (s.substeps ?? []).map((sub, j) =>
          j === patch.sub_index ? { ...sub, ...patch.patch } : sub,
        );
        return { ...s, substeps: subs };
      });
      return { ...msg, steps };
    }
    case 'thinking_append_body': {
      if (msg.kind !== 'thinking') return msg;
      return { ...msg, body: (msg.body ?? '') + patch.chunk };
    }
    case 'thinking_finalize': {
      if (msg.kind !== 'thinking') return msg;
      return {
        ...msg,
        status: 'done',
        tokens: patch.tokens ?? msg.tokens,
        duration_ms: patch.duration_ms ?? msg.duration_ms,
      };
    }
    case 'tool_finalize': {
      if (msg.kind !== 'tool_call') return msg;
      return {
        ...msg,
        status: patch.status,
        duration_ms: patch.duration_ms ?? msg.duration_ms,
        result_summary: patch.result_summary ?? msg.result_summary,
      };
    }
    case 'diff_append_line': {
      if (msg.kind !== 'diff_panel') return msg;
      return { ...msg, lines: [...msg.lines, patch.line] };
    }
    case 'msg_foot_update': {
      if (msg.kind !== 'msg_foot') return msg;
      return { ...msg, ...patch.patch };
    }
    // Polynoia-parity（2026-08-15）— plan_card 任务行的实时状态。此前缺失该
    // 分支：群时间线 REST 读路径重放时 plan_update_task 被静默丢弃，刷新后
    // 计划卡的任务状态退回初始值。与前端 reducer 保持同等语义。
    case 'plan_update_task': {
      if (msg.kind !== 'plan_card') return msg;
      return {
        ...msg,
        tasks: msg.tasks.map((t) => (t.id === patch.task_id ? { ...t, ...patch.patch } : t)),
      };
    }
    // 2026-11-07 P0 — hop_card 生命周期推进（hop_update）。此前缺失该分支：
    // 群时间线 REST 读路径重放时 hop_update 被静默丢弃，刷新/重连回放后 hop 卡
    // 永卡 pending（前端 reducer 一直在收，服务端回放不收）。与前端 reducer
    // （src/components/run-session/timeline/types.ts）保持同等语义。
    case 'hop_update': {
      if (msg.kind !== 'hop_card') return msg;
      return { ...msg, ...patch.patch };
    }
    default:
      return msg;
  }
}

/** The subset of RunEventRecord this module needs (decoupled from run-event-bus). */
export interface ReplayableEvent {
  event: string;
  data: unknown;
}

function isTimelineMessage(data: unknown): data is TimelineMessage {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const m = data as Partial<TimelineMessage>;
  return typeof m.id === 'string' && typeof m.kind === 'string';
}

function isMessagePatch(data: unknown): data is MessagePatch {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const p = data as Partial<MessagePatch>;
  return typeof p.id === 'string' && typeof p.op === 'string';
}

/**
 * Fold a run's buffered event log into its final TimelineMessage[].
 * `message` events append; `message-patch` events mutate (immutably) the
 * previously-seen message with the matching id. Patches targeting unknown ids
 * are dropped (the ring buffer may have evicted the original). Everything else
 * is ignored.
 */
export function replayTimeline(events: ReplayableEvent[]): TimelineMessage[] {
  const out: TimelineMessage[] = [];
  const index = new Map<string, number>(); // message id → position in `out`
  for (const rec of events) {
    if (rec.event === 'message') {
      if (!isTimelineMessage(rec.data)) continue;
      const msg = rec.data;
      const existing = index.get(msg.id);
      if (existing !== undefined) {
        // Same id re-emitted (shouldn't happen with monotonic projector ids,
        // but replay must be tolerant) — last write wins, position kept.
        out[existing] = msg;
      } else {
        index.set(msg.id, out.length);
        out.push(msg);
      }
    } else if (rec.event === 'message-patch') {
      if (!isMessagePatch(rec.data)) continue;
      const pos = index.get(rec.data.id);
      if (pos === undefined) continue;
      out[pos] = applyPatch(out[pos], rec.data);
    }
  }
  return out;
}

/**
 * Python group MessageItem subset (xueban/api/groups.py `MessageItem`).
 * `message_id` exists on records written since 2026-05; legacy records without
 * it fall back to a positional id (stable as long as history is append-only,
 * which the Python store guarantees).
 */
export interface ChatMessageItem {
  message_id?: string | null;
  sender_name?: string;
  sender_kind?: string;
  content?: string;
  timestamp?: string;
  /** U0 绑角色（2026-06-23）— agent 发送者 agent_id；透传到 TimelineMessage。 */
  agent_id?: string | null;
}

/**
 * Adapt persisted group chat messages → TimelineMessage[] (see module JSDoc §3).
 * Pure; preserves input order; ts falls back to 0 when timestamp is missing or
 * unparsable (sorts to the front rather than throwing).
 */
export function chatMessagesToTimeline(messages: ChatMessageItem[]): TimelineMessage[] {
  const out: TimelineMessage[] = [];
  // Messages before the first user turn (e.g. a system bridge notice) get a
  // deterministic synthetic turn id.
  let turnId = 'chat_turn_0';
  messages.forEach((m, i) => {
    const id = `chat_${m.message_id ?? `idx_${i}`}`;
    const parsed = m.timestamp ? Date.parse(m.timestamp) : NaN;
    const ts = Number.isFinite(parsed) ? parsed : 0;
    const content = typeof m.content === 'string' ? m.content : '';
    const kind = m.sender_kind ?? 'user';
    if (kind === 'user') {
      turnId = id; // a user message opens a new turn
      out.push({ id, kind: 'user_turn', turn_id: turnId, ts, text: content });
    } else if (kind === 'agent') {
      out.push({
        id,
        kind: 'assistant_text',
        turn_id: turnId,
        ts,
        body: content,
        ...(m.sender_name ? { sender_name: m.sender_name } : {}),
        ...(m.agent_id ? { agent_id: m.agent_id } : {}),
      });
    } else {
      // system (and any unknown sender_kind — raw is the safe bucket)
      out.push({
        id,
        kind: 'raw',
        turn_id: turnId,
        ts,
        body: content,
        source: 'chat-gateway',
        ...(m.sender_name ? { sender_name: m.sender_name } : {}),
      });
    }
  });
  return out;
}
