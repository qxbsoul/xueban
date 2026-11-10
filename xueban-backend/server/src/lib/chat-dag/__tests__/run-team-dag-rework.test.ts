/**
 * run-team-dag-rework.test.ts — conditional rework 闭环测试（2026-11-05 第二轮）。
 *
 * 覆盖：QA 驳回 → Dev 返工（意见带回）→ 预算上限 → 驳回标记不命中不返工 →
 * rework hop 留痕 → 返工失败不再循环。
 *
 * 注意：override callAgent 时不要引用 mkDeps 内部的 callOrder（作用域外会抛
 * ReferenceError 且被 runNodeWithPolicy 吞成节点 error）——调用计数用测试作用域
 * 的局部变量或 (deps.callAgent as Mock).mock.calls。
 */
import { describe, it, expect, vi } from 'vitest';
import { runTeamDagFanout, type RunTeamDagDeps, type UpstreamMsg } from '../run-team-dag';
import type { TeamRunShape } from '../../team-source';

type CallArgs = { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal };

function mkDeps(over: Partial<RunTeamDagDeps> = {}): RunTeamDagDeps & { posted: { sender_name: string; sender_kind: string; content: string }[] } {
  const posted: { sender_name: string; sender_kind: string; content: string }[] = [];
  const deps: RunTeamDagDeps & { posted: typeof posted } = {
    getAgent: vi.fn(async (id: string) => ({ name: id.toUpperCase(), soul: `soul-${id}` })),
    callAgent: vi.fn(async (): Promise<{ text: string; error?: string }> => ({ text: 'ok' })),
    postMessage: vi.fn(async (m) => { posted.push({ sender_name: m.sender_name, sender_kind: m.sender_kind, content: m.content }); }),
    posted,
    ...over,
  };
  return deps;
}

const callsOf = (deps: RunTeamDagDeps): CallArgs[] =>
  (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c: unknown[]) => c[0] as CallArgs);

/** dev → qa（前向）+ qa ⇢ dev（conditional 评审边）— 经典「QA 驳回 Dev 返工」。 */
const devQa: TeamRunShape = {
  members: ['dev', 'qa'],
  edges: [
    { from: 'dev', to: 'qa', kind: 'sequential' },
    { from: 'qa', to: 'dev', kind: 'conditional', max_retries: 2 },
  ],
  policy_matrix: {},
};

describe('runTeamDagFanout — conditional rework', () => {
  it('QA 产出含驳回标记 → dev 返工，复审通过后停（意见确已带回）', async () => {
    let devRuns = 0;
    let qaRuns = 0;
    const deps = mkDeps({
      callAgent: vi.fn(async ({ agentId, upstream }: CallArgs): Promise<{ text: string; error?: string }> => {
        if (agentId === 'dev') {
          devRuns++;
          const gotCritique = upstream.some((u) => u.text.includes('[驳回意见'));
          return { text: gotCritique ? 'dev:v2-fixed' : 'dev:v1' };
        }
        qaRuns++;
        void upstream;
        // 初审驳回；复审（dev 返工后）通过
        return { text: qaRuns >= 2 ? 'qa:复审通过 LGTM' : 'qa:驳回，缺测试报告 [verdict:reject]' };
      }),
    });
    const r = await runTeamDagFanout(devQa, deps, 'go', { maxReworkRounds: 2 });
    expect(devRuns).toBe(2); // 初次 + 1 轮返工（复审通过，循环提前结束）
    expect(qaRuns).toBe(2); // 初审 + 1 次复审
    expect(r.reworkRuns).toBe(1);
    const calls = callsOf(deps);
    const devSecond = calls.filter((c) => c.agentId === 'dev')[1];
    expect(devSecond).toBeDefined();
    expect(devSecond.upstream.some((u) => u.from === 'qa' && u.text.includes('[驳回意见'))).toBe(true);
    expect(deps.posted.some((p) => p.sender_kind === 'system' && p.content.includes("[rework·返工]"))).toBe(true);
  });

  it('QA 产出无驳回标记 → 不触发返工', async () => {
    const deps = mkDeps({
      callAgent: vi.fn(async ({ agentId }: CallArgs): Promise<{ text: string; error?: string }> => ({ text: agentId === 'dev' ? 'dev:v1' : 'qa:验收通过 LGTM' })),
    });
    const r = await runTeamDagFanout(devQa, deps, 'go');
    expect(r.reworkRuns).toBe(0);
    expect(callsOf(deps).filter((c) => c.agentId === 'dev')).toHaveLength(1);
    expect(deps.posted.some((p) => p.content.includes("[rework·返工]"))).toBe(false);
  });

  it('预算上限：QA 恒驳回 → dev 返工 + qa 复审各 max_retries 轮，然后放行并记录', async () => {
    const deps = mkDeps({
      callAgent: vi.fn(async ({ agentId }: CallArgs): Promise<{ text: string; error?: string }> => ({ text: agentId === 'dev' ? 'dev:attempt' : 'qa:仍不通过，打回重做' })),
    });
    const r = await runTeamDagFanout(devQa, deps, 'go', { maxReworkRounds: 3 });
    // 闭环：每轮 = dev 返工 + qa 复审。edge.max_retries=2 → dev 共 1+2=3 次、qa 共 1+2=3 次
    expect(callsOf(deps).filter((c) => c.agentId === 'dev')).toHaveLength(3);
    expect(callsOf(deps).filter((c) => c.agentId === 'qa')).toHaveLength(3);
    expect(r.reworkRuns).toBe(2);
    expect(deps.posted.some((p) => p.content.includes('返工预算上限'))).toBe(true);
  });

  it('rework hop 留痕：qa→dev 边每次返工一个新 hop，终态 done', async () => {
    const rows: Array<{ id: string; edge: string; status: string; detail: string }> = [];
    const hops = {
      open: (edge: { from: string; to: string }) => {
        const id = `hop-${rows.length + 1}`;
        rows.push({ id, edge: `${edge.from}→${edge.to}`, status: 'pending', detail: '' });
        return id;
      },
      start: (id: string) => { const row = rows.find((r) => r.id === id); if (row) row.status = 'running'; },
      settle: (id: string, status: 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted', opts?: { detail?: string }) => {
        const row = rows.find((r) => r.id === id);
        if (row && !['done', 'failed', 'timeout', 'empty', 'denied', 'aborted'].includes(row.status)) {
          row.status = status;
          row.detail = opts?.detail ?? '';
        }
      },
    };
    const deps = mkDeps({
      hops,
      callAgent: vi.fn(async ({ agentId }: CallArgs): Promise<{ text: string; error?: string }> => ({ text: agentId === 'dev' ? 'dev:v1' : 'qa:驳回 [verdict:reject]' })),
    });
    await runTeamDagFanout(devQa, deps, 'go', { maxReworkRounds: 2 });
    const reworkHops = rows.filter((r) => r.edge === 'qa→dev');
    expect(reworkHops.length).toBeGreaterThanOrEqual(1);
    expect(reworkHops.every((r) => r.status === 'done')).toBe(true);
    expect(reworkHops.every((r) => r.detail.includes('rework round'))).toBe(true);
  });

  it('返工后复审通过 → 循环提前结束（不再耗尽预算）', async () => {
    const deps = mkDeps({
      callAgent: vi.fn(async ({ agentId, upstream }: CallArgs): Promise<{ text: string; error?: string }> => {
        if (agentId === 'dev') {
          const fixed = upstream.some((u) => u.text.includes('[驳回意见'));
          return { text: fixed ? 'dev:v2-fixed' : 'dev:v1' };
        }
        // qa 第一轮驳回；复审（第 2 次调用）时通过
        const qaRuns = callsOf(deps).filter((c) => c.agentId === 'qa').length;
        return { text: qaRuns >= 2 ? 'qa:复审通过 LGTM' : 'qa:驳回 [verdict:reject]' };
      }),
    });
    const r = await runTeamDagFanout(devQa, deps, 'go', { maxReworkRounds: 3 });
    expect(r.reworkRuns).toBe(1);
    // dev: 初次 + 1 返工 = 2；qa: 初审 + 1 复审 = 2（循环提前结束，不再耗预算）
    expect(callsOf(deps).filter((c) => c.agentId === 'dev')).toHaveLength(2);
    expect(callsOf(deps).filter((c) => c.agentId === 'qa')).toHaveLength(2);
    expect(deps.posted.some((p) => p.content.includes('返工预算上限'))).toBe(false);
  });

  it('返工失败（dev 返工时 error）→ 不再循环，hop 按失败结算', async () => {
    let devRuns = 0;
    const rows: Array<{ id: string; edge: string; status: string }> = [];
    const hops = {
      open: (edge: { from: string; to: string }) => {
        const id = `hop-${rows.length + 1}`;
        rows.push({ id, edge: `${edge.from}→${edge.to}`, status: 'pending' });
        return id;
      },
      start: (id: string) => { const row = rows.find((r) => r.id === id); if (row) row.status = 'running'; },
      settle: (id: string, status: 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted') => {
        const row = rows.find((r) => r.id === id);
        if (row && !['done', 'failed', 'timeout', 'empty', 'denied', 'aborted'].includes(row.status)) row.status = status;
      },
    };
    const deps = mkDeps({
      hops,
      callAgent: vi.fn(async ({ agentId, upstream }: CallArgs): Promise<{ text: string; error?: string }> => {
        if (agentId === 'dev') {
          devRuns++;
          if (upstream.some((u) => u.text.includes('[驳回意见'))) return { text: '', error: 'rework crashed' };
          return { text: 'dev:v1' };
        }
        return { text: 'qa:驳回 [verdict:reject]' };
      }),
    });
    const r = await runTeamDagFanout(devQa, deps, 'go', { maxReworkRounds: 2 });
    expect(r.reworkRuns).toBe(1);
    // dev = 初审 1 次 + 返工 2 次（B4 默认重试预算 maxRetries=1，error 重试一次）= 3；
    // 返工最终 error → break，不进复审。
    expect(devRuns).toBe(3);
    const reworkHop = rows.find((r2) => r2.edge === 'qa→dev');
    expect(reworkHop?.status).toBe('failed');
  });
});
