/**
 * hop-broadcast.test.ts — hop 生命周期实时播报收口测试（2026-11-07）。
 *
 * 覆盖 createHopBroadcaster（从 groups-chat 内联包装器抽出）：
 *   - start → hop_update {status:'running'}「交接中」中间态上屏（修 pending→终态跳变）
 *   - 帧先于补丁：名字解析期间到达的 patch 入队，hop_card 帧发出后按序 flush
 *   - 按 hopId 索引：conditional rework 同边多 hop 各自成卡、patch 不串卡
 *   - best-effort：emit 抛错不冒泡，base（SQLite 落库）照常调用
 */

import { describe, it, expect, vi } from 'vitest';
import { createHopBroadcaster, type HopBroadcasterDeps } from '../hop-broadcast';
import type { HopRecorder } from '../run-team-dag';
import type { MessagePatch, TimelineMessage } from '../../contracts';

/** 内存 base 记录器 — 形状对齐生产 storage/agent-hops.ts 的 open/start/settle。 */
function mkBase(): HopRecorder & { calls: string[]; rows: Map<string, { from: string; to: string; status: string }> } {
  const calls: string[] = [];
  const rows = new Map<string, { from: string; to: string; status: string }>();
  let n = 0;
  return {
    calls,
    rows,
    open: (edge) => {
      const id = `hop-${++n}`;
      calls.push(`open:${id}`);
      rows.set(id, { from: edge.from, to: edge.to, status: 'pending' });
      return id;
    },
    start: (id) => {
      calls.push(`start:${id}`);
      const r = rows.get(id);
      if (r) r.status = 'running';
    },
    settle: (id, status) => {
      calls.push(`settle:${id}:${status}`);
      const r = rows.get(id);
      if (r && !['done', 'failed', 'timeout', 'empty', 'denied', 'aborted'].includes(r.status)) r.status = status;
    },
  };
}

type Emitted =
  | { type: 'msg'; msg: TimelineMessage }
  | { type: 'patch'; patch: MessagePatch };

function mkDeps(over: Partial<HopBroadcasterDeps> = {}) {
  const emitted: Emitted[] = [];
  let n = 0;
  const deps: HopBroadcasterDeps & { emitted: Emitted[] } = {
    emitted,
    nextId: () => `card-${++n}`,
    turnId: 'turn-1',
    emitMessage: (msg) => emitted.push({ type: 'msg', msg }),
    emitPatch: (patch) => emitted.push({ type: 'patch', patch }),
    resolveName: async (id) => id.toUpperCase(),
    ...over,
  };
  return deps;
}

/** 可手动 resolve 的名字解析器（模拟 pyGet 慢返回，制造 patch 先到场景）。 */
function deferredNames() {
  const pending: Array<() => void> = [];
  return {
    resolveName: (id: string) =>
      new Promise<string>((res) => {
        pending.push(() => res(id.toUpperCase()));
      }),
    flush: () => {
      while (pending.length) pending.shift()!();
    },
  };
}

/** 宏任务 tick — 排空名字解析链上的全部微任务（单次 Promise.resolve 不够）。 */
const tick = () => new Promise<void>((r) => setTimeout(r, 0));

/** 取单条播报事件的 hop_update 信封（id/op/patch 收窄到 hop_update 变体）。 */
function hopEnvelope(e: Emitted): Extract<MessagePatch, { op: 'hop_update' }> {
  return (e as Extract<Emitted, { type: 'patch' }>).patch as Extract<MessagePatch, { op: 'hop_update' }>;
}

/** 取 hop_update patch 的 status 序列（忽略其他事件）。 */
function patchStatuses(emitted: Emitted[]): string[] {
  return emitted.filter((e) => e.type === 'patch').map((e) => hopEnvelope(e).patch.status as string);
}

describe('createHopBroadcaster — hop 生命周期播报', () => {
  it('open 发 pending 全量帧，start→running「交接中」中间态，settle→终态', async () => {
    const base = mkBase();
    const deps = mkDeps();
    const hops = createHopBroadcaster(base, deps);

    const id = hops.open({ from: 'pm', to: 'dev' });
    hops.start(id);
    hops.settle(id, 'done', { outputChars: 42, detail: '' });
    await tick();

    expect(base.calls).toEqual([`open:${id}`, `start:${id}`, `settle:${id}:done`]);
    // 帧：pending 全量（名字快照）
    const msg = deps.emitted.find((e) => e.type === 'msg')!.msg as Extract<TimelineMessage, { kind: 'hop_card' }>;
    expect(msg).toMatchObject({
      kind: 'hop_card',
      from_agent: 'pm',
      to_agent: 'dev',
      from_name: 'PM',
      to_name: 'DEV',
      status: 'pending',
      turn_id: 'turn-1',
    });
    // 补丁序列：running（交接中）→ done（终态），中间态不再缺席
    expect(patchStatuses(deps.emitted)).toEqual(['running', 'done']);
    const done = hopEnvelope(deps.emitted[deps.emitted.length - 1]);
    expect(done.patch).toMatchObject({ status: 'done', output_chars: 42 });
    expect(typeof done.patch.duration_ms).toBe('number');
    expect(done.patch.duration_ms!).toBeGreaterThanOrEqual(0);
  });

  it('帧未发（名字解析中）先 settle → patch 入队，帧先出、patch 后出', async () => {
    const base = mkBase();
    const names = deferredNames();
    const deps = mkDeps({ resolveName: names.resolveName });
    const hops = createHopBroadcaster(base, deps);

    const id = hops.open({ from: 'a', to: 'b' });
    hops.start(id);
    hops.settle(id, 'failed', { detail: 'boom' });
    // 名字还没回来：帧和 patch 都未发（patch 在队列里）
    expect(deps.emitted).toHaveLength(0);

    names.flush();
    await tick();

    // 顺序不变量：hop_card 帧必须先于任何 hop_update patch（前端 reducer / 回放依赖）
    expect(deps.emitted.map((e) => e.type)).toEqual(['msg', 'patch', 'patch']);
    expect(patchStatuses(deps.emitted)).toEqual(['running', 'failed']);
    const failed = hopEnvelope(deps.emitted[2]);
    expect(failed.patch).toMatchObject({ status: 'failed', detail: 'boom' });
  });

  it('同边多 hop（rework 每轮新开）→ 各自 cardId，patch 不串卡', async () => {
    const base = mkBase();
    const deps = mkDeps();
    const hops = createHopBroadcaster(base, deps);

    const round1 = hops.open({ from: 'qa', to: 'dev' });
    hops.start(round1);
    hops.settle(round1, 'failed', { detail: 'rework round 1' });
    const round2 = hops.open({ from: 'qa', to: 'dev' });
    hops.start(round2);
    hops.settle(round2, 'done', { outputChars: 7 });
    await tick();

    const msgs = deps.emitted.filter((e) => e.type === 'msg').map((e) => (e.msg as { id: string }).id);
    expect(new Set(msgs).size).toBe(2); // 两张独立卡
    const patches = deps.emitted.filter((e) => e.type === 'patch') as Array<Extract<Emitted, { type: 'patch' }>>;
    // round1 的终态打在 round1 卡上（旧按边 key 索引会串到 round2）
    const byCard = new Map<string, string[]>();
    for (const p of patches) {
      const { id, patch } = hopEnvelope(p);
      byCard.set(id, [...(byCard.get(id) ?? []), patch.status as string]);
    }
    expect(byCard.get(msgs[0])).toEqual(['running', 'failed']);
    expect(byCard.get(msgs[1])).toEqual(['running', 'done']);
  });

  it('resolveName 拒绝 → 回落 agentId，帧照发、队列照 flush', async () => {
    const base = mkBase();
    const deps = mkDeps({ resolveName: async () => Promise.reject(new Error('py down')) });
    const hops = createHopBroadcaster(base, deps);

    const id = hops.open({ from: 'x', to: 'y' });
    hops.settle(id, 'denied', { detail: 'policy deny' });
    await tick();

    const msg = deps.emitted.find((e) => e.type === 'msg')!.msg as Extract<TimelineMessage, { kind: 'hop_card' }>;
    expect(msg.from_name).toBe('x');
    expect(msg.to_name).toBe('y');
    expect(patchStatuses(deps.emitted)).toEqual(['denied']);
  });

  it('emit 抛错不冒泡、base 落库照常（best-effort 不阻塞主链路）', () => {
    const base = mkBase();
    const deps = mkDeps({
      emitMessage: () => {
        throw new Error('sse gone');
      },
      emitPatch: () => {
        throw new Error('sse gone');
      },
    });
    const hops = createHopBroadcaster(base, deps);

    expect(() => {
      const id = hops.open({ from: 'a', to: 'b' });
      hops.start(id);
      hops.settle(id, 'aborted', { detail: '外部中止' });
    }).not.toThrow();
    // SQLite 侧（base）三次回调都落了
    expect(base.calls).toHaveLength(3);
    expect(base.rows.get(base.calls[0].slice('open:'.length))!.status).toBe('aborted');
  });

  it('未经 open 的 hopId → 播报静默降级、base 照常', () => {
    const base = mkBase();
    const deps = mkDeps();
    const hops = createHopBroadcaster(base, deps);

    expect(() => {
      hops.start('stranger');
      hops.settle('stranger', 'done', { outputChars: 1 });
    }).not.toThrow();
    expect(deps.emitted).toHaveLength(0);
    expect(base.calls).toEqual(['start:stranger', 'settle:stranger:done']);
  });

  it('无 outputChars/detail 的终态 patch 不带缺席字段（不被 undefined 覆盖）', async () => {
    const base = mkBase();
    const deps = mkDeps();
    const hops = createHopBroadcaster(base, deps);

    const id = hops.open({ from: 'a', to: 'b' });
    hops.start(id);
    hops.settle(id, 'timeout');
    await tick();

    const last = hopEnvelope(deps.emitted[deps.emitted.length - 1]);
    expect('output_chars' in last.patch).toBe(false);
    expect('detail' in last.patch).toBe(false);
    expect(typeof last.patch.duration_ms).toBe('number');
  });

  it('名字解析慢但不 reject：start 已在队列里等，帧发出后 running 补丁立即跟上', async () => {
    const base = mkBase();
    const names = deferredNames();
    const deps = mkDeps({ resolveName: names.resolveName });
    const hops = createHopBroadcaster(base, deps);

    const id = hops.open({ from: 'a', to: 'b' });
    hops.start(id);
    names.flush();
    await tick();
    // 帧出来之后再 settle —— 直发不入队
    hops.settle(id, 'done', { outputChars: 3 });
    await tick();

    expect(deps.emitted.map((e) => e.type)).toEqual(['msg', 'patch', 'patch']);
    expect(patchStatuses(deps.emitted)).toEqual(['running', 'done']);
  });
});
