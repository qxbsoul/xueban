/**
 * run-event-bus-sweep.test.ts — 终态清扫 sweepOpenProcessCards 的 hop 卡收口
 * 与幂等回归（2026-11-07，DAG 每小时优化③）。
 *
 * 背景：hop_card 帧（pending→running→终态）经 gateway run 写进 run 事件日志，
 * settle 的 hop_update patch 也在同一日志。崩溃/kill 时 settle 永远不来 ——
 * SQLite 侧有 sweepOrphanHops 补 aborted，但**重放可见的日志侧**此前无人
 * 收口：回放 hop 卡永卡 pending/「交接中」。sweepOpenProcessCards 现对
 * pending/running 的 hop_card 补 hop_update {status:'aborted'} 终态 patch。
 *
 * 覆盖：
 *   1. 启动 hydration（reconstructFromSnapshot）崩溃恢复 → hop 卡收口、
 *      已 settle 的卡不改写、run 级 error 帧 + canceled；
 *   2. sweep 幂等 —— 清扫补丁只发一条，finish 二次调用不再追加；
 *   3. hydrateOne 懒恢复（O4 路径）同款收口。
 *
 * 2026-11-07 DAG 每小时优化④ — plan 卡任务行收口：行级 settle（plan_update_task）
 * 中断缺席时 running 行回放永转圈（Loader2）。sweep 现按行归并 message 初值 +
 * 补丁取最终状态，仍 'running' 的行补 plan_update_task {status:'failed'}；
 * done/failed/blocked 与 pending 行不动。覆盖同上三条路径。
 *
 * 纯 run-event-bus + timeline-replay，不碰 sqlite/cwd。
 */
import { describe, it, expect } from 'vitest';
import {
  createRunBus,
  type RunEventRecord,
  type RunPersistence,
  type RunSnapshot,
} from '../run-event-bus';
import { replayTimeline } from '../timeline-replay';

/** 一段「进程被 kill 时留下的」事件日志：hop 卡 h1 悬空、h2 已 settle。 */
function crashLog(): RunEventRecord[] {
  return [
    {
      id: 1,
      event: 'message',
      data: {
        id: 'h1',
        kind: 'hop_card',
        turn_id: 'gwturn_x',
        ts: 1_000,
        from_agent: 'a1',
        to_agent: 'a2',
        from_name: '小张',
        to_name: '小李',
        status: 'pending',
      },
      ts: 1_000,
    },
    {
      id: 2,
      event: 'message-patch',
      data: { id: 'h1', op: 'hop_update', patch: { status: 'running' } },
      ts: 1_500,
    },
    {
      id: 3,
      event: 'message',
      data: {
        id: 'h2',
        kind: 'hop_card',
        turn_id: 'gwturn_x',
        ts: 2_000,
        from_agent: 'a2',
        to_agent: 'a3',
        from_name: '小李',
        to_name: '小王',
        status: 'pending',
      },
      ts: 2_000,
    },
    {
      id: 4,
      event: 'message-patch',
      data: {
        id: 'h2',
        op: 'hop_update',
        patch: { status: 'done', output_chars: 42, duration_ms: 800 },
      },
      ts: 2_800,
    },
  ];
}

function crashSnapshot(id: string): RunSnapshot {
  return {
    id,
    status: 'running',
    events: crashLog(),
    nextEventId: 5,
    exitCode: null,
    signal: null,
    createdAt: 1_000,
    updatedAt: 2_800,
  };
}

function makePersist(seed: RunSnapshot[]): RunPersistence & { saved: Map<string, RunSnapshot> } {
  const saved = new Map<string, RunSnapshot>();
  return {
    saved,
    save: (snap) => {
      saved.set(snap.id, JSON.parse(JSON.stringify(snap)) as RunSnapshot);
    },
    remove: (id) => {
      saved.delete(id);
    },
    loadAllSync: () => seed,
    loadOneSync: (id) => saved.get(id) ?? seed.find((s) => s.id === id),
  };
}

/** 统计某卡收到的 hop_update 补丁（按终态 status 过滤可选）。 */
function hopPatches(events: RunEventRecord[], id: string, status?: string): RunEventRecord[] {
  return events.filter((e) => {
    if (e.event !== 'message-patch') return false;
    const p = e.data as { id?: string; op?: string; patch?: { status?: string } };
    return p.id === id && p.op === 'hop_update' && (status === undefined || p.patch?.status === status);
  });
}

describe('sweepOpenProcessCards — hop 卡收口', () => {
  it('启动 hydration：悬空 hop 卡补 aborted，已 settle 的不改写，run 落 canceled', () => {
    const bus = createRunBus({ persist: makePersist([crashSnapshot('s1')]) });
    const run = bus.get('s1')!;
    expect(run.status).toBe('canceled');

    const msgs = replayTimeline(run.events);
    const h1 = msgs.find((m) => m.id === 'h1')!;
    expect(h1).toMatchObject({ kind: 'hop_card', status: 'aborted' });
    if (h1.kind === 'hop_card') {
      expect(h1.detail).toBeTruthy();
      expect(typeof h1.duration_ms === 'number').toBe(true);
    }
    // 已 settle 的 h2 保持生产者的 done/42/800。
    expect(msgs.find((m) => m.id === 'h2')).toMatchObject({
      kind: 'hop_card',
      status: 'done',
      output_chars: 42,
      duration_ms: 800,
    });
    // 悬空卡只补一条 aborted 终态补丁；h2 零补丁。
    expect(hopPatches(run.events, 'h1', 'aborted')).toHaveLength(1);
    expect(hopPatches(run.events, 'h2')).toHaveLength(1);
    // run 级中断错误帧在日志尾部。
    const last = run.events[run.events.length - 1]!;
    expect(last.event).toBe('error');
    expect((last.data as { interrupted_by_restart?: boolean }).interrupted_by_restart).toBe(true);
  });

  it('幂等：清扫补丁不重复，二次 finish（异状态）不追加事件', () => {
    const bus = createRunBus({ persist: makePersist([crashSnapshot('s2')]) });
    const run = bus.get('s2')!;
    const n = run.events.length;
    // hydration 已把 run 落终态 —— finish 是 no-op，不应再扫出第二条补丁。
    bus.finish('s2', 'failed');
    bus.finish('s2', 'succeeded');
    expect(run.events.length).toBe(n);
    expect(run.status).toBe('canceled');
    expect(hopPatches(run.events, 'h1', 'aborted')).toHaveLength(1);
  });

  it('hydrateOne 懒恢复（O4）：同款 hop 卡收口', () => {
    const persist = makePersist([]);
    persist.loadOneSync = (id) => (id === 's3' ? crashSnapshot('s3') : undefined);
    const bus = createRunBus({ persist });
    const run = bus.hydrateOne('s3')!;
    expect(run.status).toBe('canceled');
    const msgs = replayTimeline(run.events);
    expect(msgs.find((m) => m.id === 'h1')).toMatchObject({ kind: 'hop_card', status: 'aborted' });
    expect(msgs.find((m) => m.id === 'h2')).toMatchObject({ kind: 'hop_card', status: 'done' });
    expect(hopPatches(run.events, 'h1', 'aborted')).toHaveLength(1);
    // 懒恢复是只读语义：claimStart 不得重跑。
    expect(bus.claimStart('s3')).toBe(false);
  });
});

/**
 * 一段「进程被 kill 时留下的」计划卡日志：行级 settle（plan_update_task）
 * 缺席/半途 —— 2026-11-07 DAG 每小时优化④（plan 卡任务行收口）。
 */
function planCrashLog(): RunEventRecord[] {
  return [
    {
      id: 1,
      event: 'message',
      data: {
        id: 'p1',
        kind: 'plan_card',
        turn_id: 'gwturn_y',
        ts: 1_000,
        title: '编排计划',
        tasks: [
          // 初值即 running（projector 路径），无任何补丁 → 收口。
          { id: 'node:a1', title: '甲', status: 'running' },
          // running→done 已结算 → 不动。
          { id: 'node:a2', title: '乙', status: 'pending' },
          // 从未派发 → pending 不动（有意不误标）。
          { id: 'node:a3', title: '丙', status: 'pending' },
          // 补丁 running 无终态 → 收口。
          { id: 'node:a4', title: '丁', status: 'pending' },
          // running 后 elapsed 补丁不带 status → 仍悬 → 收口。
          { id: 'node:a5', title: '戊', status: 'pending' },
        ],
      },
      ts: 1_000,
    },
    {
      id: 2,
      event: 'message-patch',
      data: { id: 'p1', op: 'plan_update_task', task_id: 'node:a2', patch: { status: 'running' } },
      ts: 1_200,
    },
    {
      id: 3,
      event: 'message-patch',
      data: {
        id: 'p1',
        op: 'plan_update_task',
        task_id: 'node:a2',
        patch: { status: 'done', elapsed_ms: 800 },
      },
      ts: 1_800,
    },
    {
      id: 4,
      event: 'message-patch',
      data: { id: 'p1', op: 'plan_update_task', task_id: 'node:a4', patch: { status: 'running' } },
      ts: 1_300,
    },
    {
      id: 5,
      event: 'message-patch',
      data: { id: 'p1', op: 'plan_update_task', task_id: 'node:a5', patch: { status: 'running' } },
      ts: 1_400,
    },
    {
      id: 6,
      event: 'message-patch',
      data: { id: 'p1', op: 'plan_update_task', task_id: 'node:a5', patch: { elapsed_ms: 1200 } },
      ts: 1_500,
    },
  ];
}

function planCrashSnapshot(
  id: string,
  events: RunEventRecord[] = planCrashLog(),
  nextEventId = 7,
): RunSnapshot {
  return {
    id,
    status: 'running',
    events,
    nextEventId,
    exitCode: null,
    signal: null,
    createdAt: 1_000,
    updatedAt: 2_800,
  };
}

/** 统计某计划行收到的 plan_update_task 补丁（按终态 status 过滤可选）。 */
function rowPatches(
  events: RunEventRecord[],
  cardId: string,
  taskId: string,
  status?: string,
): RunEventRecord[] {
  return events.filter((e) => {
    if (e.event !== 'message-patch') return false;
    const p = e.data as {
      id?: string;
      op?: string;
      task_id?: string;
      patch?: { status?: string };
    };
    return (
      p.id === cardId &&
      p.op === 'plan_update_task' &&
      p.task_id === taskId &&
      (status === undefined || p.patch?.status === status)
    );
  });
}

describe('sweepOpenProcessCards — plan 卡任务行收口', () => {
  it('启动 hydration：running 行补 failed 终态，done/pending 行不动、elapsed 保留', () => {
    const bus = createRunBus({ persist: makePersist([planCrashSnapshot('ps1')]) });
    const run = bus.get('ps1')!;
    expect(run.status).toBe('canceled');

    const p1 = replayTimeline(run.events).find((m) => m.id === 'p1')!;
    expect(p1.kind).toBe('plan_card');
    if (p1.kind !== 'plan_card') return;
    const byId = new Map(p1.tasks.map((t) => [t.id, t]));
    // a1：message 初值即 running、无补丁 → 补 failed。
    expect(byId.get('node:a1')).toMatchObject({
      status: 'failed',
      note: '回合中断，任务未结算',
    });
    // a2：生产者已结算 done/800 → 原样保留。
    expect(byId.get('node:a2')).toMatchObject({ status: 'done', elapsed_ms: 800 });
    // a3：从未派发 → pending 不动（有意不误标）。
    expect(byId.get('node:a3')).toMatchObject({ status: 'pending' });
    // a4：补丁 running 无终态 → 补 failed。
    expect(byId.get('node:a4')).toMatchObject({ status: 'failed' });
    // a5：running 后 elapsed 补丁不带 status 仍悬 → 补 failed，elapsed 保留。
    expect(byId.get('node:a5')).toMatchObject({ status: 'failed', elapsed_ms: 1200 });

    // 每个悬空行恰一条合成补丁；已结算/未派发行零补丁。
    for (const tid of ['node:a1', 'node:a4', 'node:a5']) {
      expect(rowPatches(run.events, 'p1', tid, 'failed')).toHaveLength(1);
    }
    expect(rowPatches(run.events, 'p1', 'node:a2', 'failed')).toHaveLength(0);
    expect(rowPatches(run.events, 'p1', 'node:a3')).toHaveLength(0);
  });

  it('幂等：日志里已有清扫合成补丁的行不二次补，其余悬空行照收', () => {
    // 模拟上一轮清扫已给 a1 补过 failed（合成补丁也在日志里）；a4/a5 仍悬空。
    const events: RunEventRecord[] = [
      ...planCrashLog(),
      {
        id: 7,
        event: 'message-patch',
        data: {
          id: 'p1',
          op: 'plan_update_task',
          task_id: 'node:a1',
          patch: { status: 'failed', note: '回合中断，任务未结算' },
        },
        ts: 1_900,
      },
    ];
    const bus = createRunBus({ persist: makePersist([planCrashSnapshot('ps2', events, 8)]) });
    const run = bus.get('ps2')!;
    // a1 总共只有那一条 failed 补丁（清扫合成的也含在内）——没有第二条。
    expect(rowPatches(run.events, 'p1', 'node:a1', 'failed')).toHaveLength(1);
    // a4/a5 是悬空行，照收。
    expect(rowPatches(run.events, 'p1', 'node:a4', 'failed')).toHaveLength(1);
    expect(rowPatches(run.events, 'p1', 'node:a5', 'failed')).toHaveLength(1);
  });

  it('hydrateOne 懒恢复（O4）：同款任务行收口', () => {
    const persist = makePersist([]);
    persist.loadOneSync = (id) => (id === 'ps3' ? planCrashSnapshot('ps3') : undefined);
    const bus = createRunBus({ persist });
    const run = bus.hydrateOne('ps3')!;
    expect(run.status).toBe('canceled');
    const p1 = replayTimeline(run.events).find((m) => m.id === 'p1')!;
    if (p1.kind === 'plan_card') {
      const byId = new Map(p1.tasks.map((t) => [t.id, t]));
      expect(byId.get('node:a1')).toMatchObject({ status: 'failed' });
      expect(byId.get('node:a3')).toMatchObject({ status: 'pending' });
    }
    expect(rowPatches(run.events, 'p1', 'node:a1', 'failed')).toHaveLength(1);
  });
});
