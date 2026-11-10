/**
 * timeline-replay.test.ts — B2 unit tests for the unified read-path building
 * blocks (unified-conversation-core-plan-2026-06-11 §B2).
 *
 * Run (from repo root):
 *   npx vitest run --root server --config ../vitest.config.ts \
 *     src/lib/__tests__/timeline-replay.test.ts
 *
 * Pure functions only (no fs/Express side effects on import), mirroring the
 * run-sessions-stream-disposition.test.ts pattern of unit-testing the lifted
 * pure core instead of the HTTP route.
 */
import { describe, it, expect } from 'vitest';
import {
  applyPatch,
  replayTimeline,
  chatMessagesToTimeline,
  type ReplayableEvent,
} from '../timeline-replay';
import type { TimelineMessage } from '../contracts';

const base = { turn_id: 't1', ts: 1000 };

describe('replayTimeline — message + patch replay', () => {
  it('collects message events in order and applies text_append patches', () => {
    const events: ReplayableEvent[] = [
      { event: 'message', data: { ...base, id: 'u1', kind: 'user_turn', text: 'hi' } },
      { event: 'message', data: { ...base, id: 'a1', kind: 'assistant_text', body: 'Hel' } },
      { event: 'message-patch', data: { id: 'a1', op: 'text_append', chunk: 'lo ' } },
      { event: 'message-patch', data: { id: 'a1', op: 'text_append', chunk: 'world' } },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ id: 'u1', kind: 'user_turn', text: 'hi' });
    expect(out[1]).toMatchObject({ id: 'a1', kind: 'assistant_text', body: 'Hello world' });
  });

  it('applies thinking_append_body + thinking_finalize', () => {
    const events: ReplayableEvent[] = [
      {
        event: 'message',
        data: { ...base, id: 'th1', kind: 'thinking', label: '思考', status: 'streaming' },
      },
      { event: 'message-patch', data: { id: 'th1', op: 'thinking_append_body', chunk: '第一步…' } },
      { event: 'message-patch', data: { id: 'th1', op: 'thinking_append_body', chunk: '第二步' } },
      {
        event: 'message-patch',
        data: { id: 'th1', op: 'thinking_finalize', tokens: 42, duration_ms: 1234 },
      },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      id: 'th1',
      kind: 'thinking',
      body: '第一步…第二步',
      status: 'done',
      tokens: 42,
      duration_ms: 1234,
    });
  });

  it('applies append_step / update_step to a step_panel', () => {
    const events: ReplayableEvent[] = [
      {
        event: 'message',
        data: { ...base, id: 'sp1', kind: 'step_panel', total_steps: 2, steps: [] },
      },
      {
        event: 'message-patch',
        data: { id: 'sp1', op: 'append_step', step: { name: '解析', status: 'running' } },
      },
      {
        event: 'message-patch',
        data: { id: 'sp1', op: 'update_step', index: 0, patch: { status: 'done', elapsed_ms: 50 } },
      },
      {
        event: 'message-patch',
        data: { id: 'sp1', op: 'append_step', step: { name: '生成', status: 'running' } },
      },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    const panel = out[0] as Extract<TimelineMessage, { kind: 'step_panel' }>;
    expect(panel.steps).toEqual([
      { name: '解析', status: 'done', elapsed_ms: 50 },
      { name: '生成', status: 'running' },
    ]);
  });

  it('ignores non-message events, malformed payloads, and patches to unknown ids', () => {
    const events: ReplayableEvent[] = [
      { event: 'text', data: { text: 'legacy fine-grained event' } },
      { event: 'message', data: 'not-an-object' },
      { event: 'message', data: { kind: 'assistant_text', body: 'no id' } },
      { event: 'message-patch', data: { id: 'ghost', op: 'text_append', chunk: 'x' } },
      { event: 'message', data: { ...base, id: 'a1', kind: 'assistant_text', body: 'ok' } },
      { event: 'complete', data: { session_id: 's1' } },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: 'a1', body: 'ok' });
  });

  it('applyPatch is a defensive no-op on kind-mismatched patches', () => {
    const msg: TimelineMessage = { ...base, id: 'u1', kind: 'user_turn', text: 'hi' };
    const same = applyPatch(msg, { id: 'u1', op: 'text_append', chunk: 'x' });
    expect(same).toBe(msg); // returns the original instance untouched
  });

  it('Polynoia-parity：plan_update_task 重放后任务状态不丢（此前被静默丢弃）', () => {
    const events: ReplayableEvent[] = [
      {
        event: 'message',
        data: {
          ...base,
          id: 'plan1',
          kind: 'plan_card',
          title: '编排计划',
          tasks: [
            { id: 'node:writer', title: '写文案', assignee: 'Writer', status: 'pending' },
            { id: 'node:reviewer', title: '审校', assignee: 'Reviewer', status: 'pending' },
          ],
        },
      },
      { event: 'message-patch', data: { id: 'plan1', op: 'plan_update_task', task_id: 'node:writer', patch: { status: 'running' } } },
      { event: 'message-patch', data: { id: 'plan1', op: 'plan_update_task', task_id: 'node:writer', patch: { status: 'done', elapsed_ms: 900 } } },
      { event: 'message-patch', data: { id: 'plan1', op: 'plan_update_task', task_id: 'node:reviewer', patch: { status: 'failed' } } },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    const plan = out[0];
    if (plan.kind !== 'plan_card') throw new Error('kind drifted');
    expect(plan.tasks[0]).toMatchObject({ status: 'done', elapsed_ms: 900 });
    expect(plan.tasks[1]).toMatchObject({ status: 'failed' });
    // kind 不匹配 → 原实例原样返回（防御式 no-op）
    const stale = applyPatch(out[0], { id: 'plan1', op: 'text_append', chunk: 'x' });
    expect(stale).toBe(out[0]);
  });

  it('2026-11-07 — text_finalize 重放后收口 streaming 光标', () => {
    const events: ReplayableEvent[] = [
      { event: 'message', data: { ...base, id: 'a1', kind: 'assistant_text', body: 'hi' } },
      { event: 'message-patch', data: { id: 'a1', op: 'text_append', chunk: '!' } },
      { event: 'message-patch', data: { id: 'a1', op: 'text_finalize' } },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: 'a1', body: 'hi!', streaming: false });
  });
});

// ── 2026-11-07 P0 — hop_update 重放（hop 卡生命周期结算不丢）────────────────
describe('applyPatch / replayTimeline — hop_update', () => {
  const hopOpen = {
    ...base,
    id: 'hop1',
    kind: 'hop_card',
    from_agent: 'agent-a',
    to_agent: 'agent-b',
    from_name: 'Scout',
    to_name: 'Writer',
    status: 'pending',
  } as const;

  it('hop_update 重放后 hop 卡终态不丢（此前缺分支→刷新回放永卡 pending）', () => {
    const events: ReplayableEvent[] = [
      { event: 'message', data: hopOpen },
      { event: 'message-patch', data: { id: 'hop1', op: 'hop_update', patch: { status: 'running' } } },
      {
        event: 'message-patch',
        data: {
          id: 'hop1',
          op: 'hop_update',
          patch: { status: 'done', output_chars: 520, duration_ms: 4300 },
        },
      },
    ];
    const out = replayTimeline(events);
    expect(out).toHaveLength(1);
    const hop = out[0];
    if (hop.kind !== 'hop_card') throw new Error('kind drifted');
    expect(hop).toMatchObject({ status: 'done', output_chars: 520, duration_ms: 4300 });
    // 原始 open 帧的静态字段保留
    expect(hop).toMatchObject({ from_name: 'Scout', to_name: 'Writer', from_agent: 'agent-a', to_agent: 'agent-b' });
  });

  it('失败终态带 detail，且 patch 不可变（原实例不动）', () => {
    const failed = applyPatch(hopOpen, {
      id: 'hop1',
      op: 'hop_update',
      patch: { status: 'failed', detail: '上游产出为空' },
    });
    if (failed.kind !== 'hop_card') throw new Error('kind drifted');
    expect(failed).toMatchObject({ status: 'failed', detail: '上游产出为空' });
    // 不可变性：applyPatch 返回新对象，原实例保持 pending
    expect(hopOpen.status).toBe('pending');
  });

  it('kind 不匹配为防御式 no-op（返回原实例）', () => {
    const msg: TimelineMessage = { ...base, id: 't1', kind: 'assistant_text', body: 'x' };
    expect(applyPatch(msg, { id: 't1', op: 'hop_update', patch: { status: 'done' } })).toBe(msg);
    // hop_card 收到别类 patch 同样 no-op
    expect(applyPatch(hopOpen, { id: 'hop1', op: 'text_append', chunk: 'x' })).toBe(hopOpen);
  });

  it('patch 中缺席的字段不覆盖（部分结算只带 status）', () => {
    const next = applyPatch(hopOpen, {
      id: 'hop1',
      op: 'hop_update',
      patch: { status: 'timeout' },
    });
    if (next.kind !== 'hop_card') throw new Error('kind drifted');
    expect(next.status).toBe('timeout');
    // open 帧没写过的可选字段依旧缺席（不被 undefined 污染）
    expect('output_chars' in next).toBe(false);
    expect('detail' in next).toBe(false);
    expect('duration_ms' in next).toBe(false);
  });
});

describe('chatMessagesToTimeline — group MessageItem adaptation', () => {
  it('maps user/agent/system kinds and threads turn_id by user message', () => {
    const out = chatMessagesToTimeline([
      {
        message_id: 'm1',
        sender_kind: 'user',
        sender_name: 'user',
        content: '帮我做个东西',
        timestamp: '2026-06-11T08:00:00+00:00',
      },
      {
        message_id: 'm2',
        sender_kind: 'agent',
        sender_name: '小助手',
        content: '好的，开始',
        timestamp: '2026-06-11T08:00:05+00:00',
      },
      {
        message_id: 'm3',
        sender_kind: 'system',
        sender_name: 'bridge',
        content: 'gateway notice',
        timestamp: '2026-06-11T08:00:06+00:00',
      },
      {
        message_id: 'm4',
        sender_kind: 'user',
        sender_name: 'user',
        content: '继续',
        timestamp: '2026-06-11T08:01:00+00:00',
      },
      {
        message_id: 'm5',
        sender_kind: 'agent',
        sender_name: '小助手',
        content: '收到',
        timestamp: '2026-06-11T08:01:02+00:00',
      },
    ]);

    expect(out.map((m) => m.kind)).toEqual([
      'user_turn',
      'assistant_text',
      'raw',
      'user_turn',
      'assistant_text',
    ]);
    // stable ids
    expect(out.map((m) => m.id)).toEqual(['chat_m1', 'chat_m2', 'chat_m3', 'chat_m4', 'chat_m5']);
    // turn threading: m2/m3 belong to m1's turn; m5 to m4's turn
    expect(out[0].turn_id).toBe('chat_m1');
    expect(out[1].turn_id).toBe('chat_m1');
    expect(out[2].turn_id).toBe('chat_m1');
    expect(out[3].turn_id).toBe('chat_m4');
    expect(out[4].turn_id).toBe('chat_m4');
    // sender attribution survives (additive field)
    expect(out[1]).toMatchObject({ sender_name: '小助手', body: '好的，开始' });
    expect(out[2]).toMatchObject({ source: 'chat-gateway', sender_name: 'bridge' });
    // ts parsed from ISO timestamp
    expect(out[0].ts).toBe(Date.parse('2026-06-11T08:00:00+00:00'));
    expect(out[0].ts).toBeLessThan(out[3].ts);
  });

  it('falls back to positional id / ts 0 for legacy records and defaults kind to user', () => {
    const out = chatMessagesToTimeline([
      { content: 'legacy without message_id/timestamp' },
      { sender_kind: 'agent', content: 'reply', timestamp: 'not-a-date' },
    ]);
    expect(out[0]).toMatchObject({ id: 'chat_idx_0', kind: 'user_turn', ts: 0 });
    expect(out[1]).toMatchObject({ id: 'chat_idx_1', kind: 'assistant_text', ts: 0 });
    expect(out[1].turn_id).toBe('chat_idx_0'); // threaded under the legacy user turn
  });

  it('U0 绑角色：agent 消息的 agent_id 透传到 assistant_text；user/无 agent_id 不带该字段', () => {
    const out = chatMessagesToTimeline([
      { message_id: 'u1', sender_kind: 'user', content: 'hi', timestamp: '2026-06-23T08:00:00+00:00' },
      { message_id: 'a1', sender_kind: 'agent', sender_name: 'PM', agent_id: 'agent-123', content: '收到', timestamp: '2026-06-23T08:00:01+00:00' },
      { message_id: 'a2', sender_kind: 'agent', sender_name: 'Legacy', content: '老记录无 agent_id', timestamp: '2026-06-23T08:00:02+00:00' },
    ]);
    expect(out[1]).toMatchObject({ kind: 'assistant_text', sender_name: 'PM', agent_id: 'agent-123' });
    // user_turn 不带 agent_id
    expect('agent_id' in out[0]).toBe(false);
    // 老 agent 记录无 agent_id 字段（不写 undefined 键）
    expect('agent_id' in out[2]).toBe(false);
  });
});
