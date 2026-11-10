/**
 * groups-chat-intent-card.test.ts
 *
 * WorkBuddy 式意图分发可视化（2026-08-23）：网关 INTENT_PARSE 后补发的
 * `intent` 帧（派发决策：派给谁 + 为什么）。
 *
 * 覆盖：
 *   - buildIntentCard 纯函数：意图原文压空白 + 截断；空 assignments 归一
 *     为 undefined（「无明细」与「无拆解」同一视觉）；
 *   - emitIntentCard NeverRaising：gw=null 不抛、gw.emitMessage 抛错不抛；
 *   - emitIntentCard 正常路径：发出 kind='intent' 的 TimelineMessage，
 *     字段（intent/target_kind/target_name/reason/assignments）齐全。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-intent-card.test.ts
 */
import { describe, it, expect, vi } from 'vitest';
import { buildIntentCard, emitIntentCard } from '../groups-chat';
import type { TimelineMessage } from '../../lib/contracts';

describe('buildIntentCard — intent 帧 payload 组装', () => {
  it('压空白 + 保留 target/reason/assignments', () => {
    const card = buildIntentCard({
      intent: '  帮我\n读一下   这篇论文  ',
      target_kind: 'team',
      target_name: '论文深读小队',
      reason: '编排者拆解为 2 个子任务',
      assignments: [{ agent_id: 'a1', name: '读读', title: '抓 PDF' }],
    });
    expect(card.intent).toBe('帮我 读一下 这篇论文');
    expect(card.target_kind).toBe('team');
    expect(card.target_name).toBe('论文深读小队');
    expect(card.reason).toContain('2 个子任务');
    expect(card.assignments).toHaveLength(1);
  });

  it('意图原文截断到 120 字', () => {
    const card = buildIntentCard({
      intent: 'x'.repeat(200),
      target_kind: 'agent',
      target_name: '读读',
      reason: 'r',
    });
    expect(card.intent).toHaveLength(120);
  });

  it('空 assignments 数组归一为 undefined', () => {
    const card = buildIntentCard({
      intent: 'hi',
      target_kind: 'team',
      target_name: 't',
      reason: 'r',
      assignments: [],
    });
    expect(card.assignments).toBeUndefined();
  });
});

describe('emitIntentCard — NeverRaising', () => {
  it('gw 为 null → 静默不抛', () => {
    expect(() =>
      emitIntentCard(null, { intent: 'x', target_kind: 'team', target_name: 't', reason: 'r' }),
    ).not.toThrow();
  });

  it('gw.emitMessage 抛错 → 吞掉不抛（不影响聊天主链路）', () => {
    const gw = {
      nextId: () => 'gw_s_1',
      turnId: 'gwturn_s',
      emitMessage: () => {
        throw new Error('bus down');
      },
    };
    expect(() =>
      emitIntentCard(gw as never, {
        intent: 'x',
        target_kind: 'agent',
        target_name: 'a',
        reason: 'r',
      }),
    ).not.toThrow();
  });

  it('正常路径：发出 kind=intent 的 TimelineMessage，字段齐全', () => {
    const sent: TimelineMessage[] = [];
    const gw = {
      nextId: vi.fn(() => 'gw_s_1'),
      turnId: 'gwturn_s',
      emitMessage: (m: TimelineMessage) => sent.push(m),
    };
    emitIntentCard(gw as never, {
      intent: '读论文',
      target_kind: 'team',
      target_name: '论文深读小队',
      reason: '编排者拆解为 1 个子任务',
      assignments: [{ agent_id: 'a1', name: '读读', title: '抓 PDF' }],
    });
    expect(sent).toHaveLength(1);
    const m = sent[0];
    expect(m.kind).toBe('intent');
    expect(m.id).toBe('gw_s_1');
    expect(m.turn_id).toBe('gwturn_s');
    if (m.kind === 'intent') {
      expect(m.intent).toBe('读论文');
      expect(m.target_kind).toBe('team');
      expect(m.target_name).toBe('论文深读小队');
      expect(m.reason).toContain('拆解');
      expect(m.assignments?.[0]?.name).toBe('读读');
    }
  });
});
