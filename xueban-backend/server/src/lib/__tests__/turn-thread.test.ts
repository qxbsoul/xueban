/**
 * turn-thread.test.ts — Whole-thread 回放保真契约（2026-11-07）。
 *
 * 「thread 丢段即测试红」：一次 turn 的 (history, prompt) 必须覆盖持久化线程的
 * 每个文本段恰好一次；除被提升为 prompt 的用户轮外任何消息都不得丢弃；history
 * 折叠相邻同角色且不以 'user' 收尾（Anthropic 严格交替，两相邻同角色 → 422）。
 *
 * 回归背景：旧 `history.slice(0, -1)` 盲删末位——多 agent 串行轮末位是上一个
 * agent 的回复，被整条删掉且用户消息与 prompt 重复。
 */
import { describe, it, expect } from 'vitest';
import { buildTurnThread, foldConsecutiveRoles, textOf } from '../turn-thread';
import type { ConversationMessage } from '../conversation-types';

const u = (text: string): ConversationMessage => ({
  role: 'user',
  blocks: [{ kind: 'text', text }],
});
const a = (text: string): ConversationMessage => ({
  role: 'assistant',
  blocks: [{ kind: 'text', text }],
});

/** assembled 的文本段清单：history 各 text block + prompt（按 \n\n 可拆段）。 */
function segmentList(t: { history: ConversationMessage[]; prompt: string }): string[] {
  const out: string[] = [];
  for (const m of t.history) for (const b of m.blocks) {
    if (b.kind === 'text' && b.text) out.push(b.text);
  }
  if (t.prompt) out.push(...t.prompt.split('\n\n'));
  return out;
}

/** 段多重集恰好覆盖期望集合（丢段/重复都红）。 */
function expectSegmentsOnce(
  t: { history: ConversationMessage[]; prompt: string },
  expected: string[],
): void {
  expect(segmentList(t).slice().sort()).toEqual(expected.slice().sort());
}

describe('foldConsecutiveRoles', () => {
  it('相邻同角色折叠为一条，blocks 原序拼接（段界保留）', () => {
    const out = foldConsecutiveRoles([u('U1'), u('U2'), a('A1'), a('A2'), u('U3')]);
    expect(out.map((m) => m.role)).toEqual(['user', 'assistant', 'user']);
    expect(out[0]!.blocks.map((b) => (b.kind === 'text' ? b.text : ''))).toEqual(['U1', 'U2']);
    expect(out[1]!.blocks.map((b) => (b.kind === 'text' ? b.text : ''))).toEqual(['A1', 'A2']);
  });

  it('不相邻同角色不合并；tool 角色同款折叠', () => {
    const out = foldConsecutiveRoles([u('U1'), a('A1'), u('U2')]);
    expect(out).toHaveLength(3);
    const tools = foldConsecutiveRoles([
      { role: 'tool', blocks: [{ kind: 'text', text: 'T1' }] },
      { role: 'tool', blocks: [{ kind: 'text', text: 'T2' }] },
    ]);
    expect(tools).toHaveLength(1);
    expect(tools[0]!.blocks).toHaveLength(2);
  });

  it('不改入参（新对象/新数组）', () => {
    const input = [u('U1'), u('U2')];
    const out = foldConsecutiveRoles(input);
    expect(input).toHaveLength(2);
    expect(out[0]).not.toBe(input[0]);
  });
});

describe('buildTurnThread — 契约 1：保真（丢段/重复即红）', () => {
  it('常规交替线程：五段各恰好一次，与旧 slice(0,-1) 行为一致', () => {
    const t = buildTurnThread([u('U1'), a('A1'), u('U2'), a('A2'), u('U3')], 'U3');
    expect(t.prompt).toBe('U3');
    expect(t.history.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant']);
    expectSegmentsOnce(t, ['U1', 'A1', 'U2', 'A2', 'U3']);
  });

  it('多 agent 串行轮回归：末位前序回复不丢、prompt 用户消息不重复', () => {
    // 线程 [U, A1]：U 是被提升的用户消息，A1 是上一个 agent 的回复（旧
    // slice(0,-1) 把 A1 整条删掉、U 留在 history 里与 prompt 重复）。
    const t = buildTurnThread([u('U'), a('A1')], 'U');
    expectSegmentsOnce(t, ['U', 'A1']);
    expect(textOf(t.history[0]!)).toBe('A1');
    expect(t.prompt).toBe('U');
  });

  it('多轮线程末位是回复时同样不丢', () => {
    const t = buildTurnThread([u('U1'), a('A1'), u('U2'), a('A2'), a('A3')], 'U2');
    expectSegmentsOnce(t, ['U1', 'A1', 'U2', 'A2', 'A3']);
    expect(t.prompt).toBe('U2');
  });

  it('双连发：更早用户段并入 prompt 前缀，不丢不重复', () => {
    const t = buildTurnThread([u('U1'), u('U2')], 'U2');
    expect(t.history).toHaveLength(0);
    expect(t.prompt).toBe('U1\n\nU2');
    expectSegmentsOnce(t, ['U1', 'U2']);
  });

  it('三连发 + 前序回复：run 整体提升，回复保留', () => {
    const t = buildTurnThread([u('U1'), u('U2'), u('U3'), a('A1')], 'U3');
    expect(t.prompt).toBe('U1\n\nU2\n\nU3');
    expect(textOf(t.history[0]!)).toBe('A1');
    expectSegmentsOnce(t, ['U1', 'U2', 'U3', 'A1']);
  });

  it('派生 prompt（@ 清洗/子任务）：被提升消息视为被替换，run 前缀仍保真', () => {
    const t = buildTurnThread([u('U1'), u('@秘書 U2')], '清洗后指令');
    expect(t.prompt).toBe('U1\n\n清洗后指令');
    // U2 原文被 prompt 替换（与旧 slice(0,-1) 语义一致，@ 噪音不回灌）。
    expect(segmentList(t)).not.toContain('@秘書 U2');
  });

  it('无用户消息线程：全线程保留（旧 slice 会盲丢末段）', () => {
    const t = buildTurnThread([a('A1'), a('A2')], 'X');
    expect(t.prompt).toBe('X');
    expectSegmentsOnce(t, ['A1', 'A2', 'X']);
    expect(t.latestUserText).toBe('');
  });

  it('空线程：prompt 原样', () => {
    const t = buildTurnThread([], 'X');
    expect(t.history).toHaveLength(0);
    expect(t.prompt).toBe('X');
  });
});

describe('buildTurnThread — 契约 2：合法（接口缝严格交替）', () => {
  const threads: ConversationMessage[][] = [
    [],
    [u('U1')],
    [u('U1'), u('U2')],
    [u('U1'), a('A1')],
    [u('U1'), a('A1'), u('U2')],
    [u('U1'), u('U2'), a('A1')],
    [u('U1'), a('A1'), a('A2')],
    [u('U1'), a('A1'), u('U2'), u('U3'), a('A2'), a('A3'), u('U4')],
    [a('A1')],
    [a('A1'), u('U1'), a('A2'), u('U2')],
  ];

  it('history 不以 user 收尾（有 user 轮时），prompt 追加后无相邻同角色', () => {
    for (const msgs of threads) {
      const t = buildTurnThread(msgs, 'PROMPT');
      if (t.history.length > 0 && msgs.some((m) => m.role === 'user')) {
        expect(t.history[t.history.length - 1]!.role).not.toBe('user');
      }
      // history 内部已折叠 → 无相邻同角色。
      for (let k = 1; k < t.history.length; k++) {
        expect(t.history[k]!.role).not.toBe(t.history[k - 1]!.role);
      }
    }
  });

  it('折叠后的 history 段数不增、blocks 全保留', () => {
    const msgs = [u('U1'), u('U2'), a('A1'), a('A2'), u('U3')];
    const t = buildTurnThread(msgs, 'U3');
    expect(t.history.length).toBeLessThanOrEqual(msgs.length - 1);
    const blockCount = t.history.reduce((n, m) => n + m.blocks.length, 0);
    expect(blockCount).toBe(4); // U1 U2 A1 A2，段界不丢
  });
});

describe('buildTurnThread — 契约 3：latestUserText 提升定位', () => {
  it('取最后一条 user 原文（末位是回复时不误判）', () => {
    const t = buildTurnThread([u('U1'), a('A1'), u('U2'), a('A2')], 'P');
    expect(t.latestUserText).toBe('U2');
  });
});
