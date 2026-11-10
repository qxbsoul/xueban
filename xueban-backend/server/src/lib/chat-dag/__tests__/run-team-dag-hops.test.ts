/**
 * run-team-dag-hops.test.ts — DAG 执行器交接记录 + 失败传播测试（2026-11-05）。
 *
 * 覆盖 run-team-dag.ts 新增的 deps.hops（HopRecorder）路径：
 *   - hop 生命周期：open（起边）→ start（节点起跑）→ settle（done/denied/failed...）
 *   - 失败传播：上游 failed/empty/timeout 时下游 upstream 注入 [系统通知]
 *   - 收尾兜底：下游没跑成/没轮到的边不留悬空 hop
 *   - hops 不注入 → 行为与旧版一致（零回归）
 */

import { describe, it, expect, vi } from 'vitest';
import { runTeamDagFanout, type RunTeamDagDeps, type HopRecorder, type UpstreamMsg } from '../run-team-dag';
import type { TeamRunShape } from '../../team-source';

/** 内存 hop 记录器 — 形状对齐生产 storage/agent-hops.ts。 */
function mkHopRecorder(): HopRecorder & {
  rows: Array<{ id: string; edge: string; status: string; detail: string; outputChars: number }>;
} {
  const rows: Array<{ id: string; edge: string; status: string; detail: string; outputChars: number }> = [];
  const byId = new Map<string, { edge: string }>();
  return {
    rows,
    open: (edge) => {
      const id = `hop-${rows.length + 1}`;
      byId.set(id, { edge: `${edge.from}→${edge.to}` });
      rows.push({ id, edge: `${edge.from}→${edge.to}`, status: 'pending', detail: '', outputChars: 0 });
      return id;
    },
    start: (id) => {
      const row = rows.find((r) => r.id === id);
      if (row) row.status = 'running';
    },
    settle: (id, status, opts) => {
      const row = rows.find((r) => r.id === id);
      if (row && row.status !== 'done' && !['failed', 'timeout', 'empty', 'denied', 'aborted'].includes(row.status)) {
        row.status = status;
        row.detail = opts?.detail ?? '';
        row.outputChars = opts?.outputChars ?? 0;
      }
    },
  };
}

function mkDeps(over: Partial<RunTeamDagDeps> = {}): RunTeamDagDeps & { posted: { sender_name: string; sender_kind: string; content: string }[]; callOrder: string[] } {
  const posted: { sender_name: string; sender_kind: string; content: string }[] = [];
  const callOrder: string[] = [];
  const deps: RunTeamDagDeps & { posted: typeof posted; callOrder: string[] } = {
    getAgent: vi.fn(async (id: string) => ({ name: id.toUpperCase(), soul: `soul-${id}` })),
    callAgent: vi.fn(async ({ agentId, upstream }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => { callOrder.push(agentId); return { text: `${agentId}:reply(up=${upstream.map((u) => u.from).join('+') || 'none'})` }; }),
    postMessage: vi.fn(async (m) => { posted.push({ sender_name: m.sender_name, sender_kind: m.sender_kind, content: m.content }); }),
    posted, callOrder,
    ...over,
  };
  return deps;
}

const linear: TeamRunShape = {
  members: ['pm', 'arch', 'dev'],
  edges: [{ from: 'pm', to: 'arch', kind: 'sequential' }, { from: 'arch', to: 'dev', kind: 'sequential' }],
  policy_matrix: {},
};

describe('runTeamDagFanout — hops 交接记录', () => {
  it('线性链：每条边一个 hop，done + outputChars', async () => {
    const hops = mkHopRecorder();
    const deps = mkDeps({ hops });
    await runTeamDagFanout(linear, deps, 'go');
    expect(hops.rows).toHaveLength(2); // pm→arch, arch→dev
    expect(hops.rows.map((r) => r.edge).sort()).toEqual(['arch→dev', 'pm→arch']);
    expect(hops.rows.every((r) => r.status === 'done')).toBe(true);
    expect(hops.rows.find((r) => r.edge === 'pm→arch')!.outputChars).toBe('arch:reply(up=pm)'.length);
  });

  it('deny 边 → hop 结算为 denied', async () => {
    const hops = mkHopRecorder();
    const team: TeamRunShape = { ...linear, policy_matrix: { pm: { arch: 'deny' } } };
    await runTeamDagFanout(team, mkDeps({ hops }), 'go');
    expect(hops.rows.find((r) => r.edge === 'pm→arch')!.status).toBe('denied');
    expect(hops.rows.find((r) => r.edge === 'arch→dev')!.status).toBe('done');
  });

  it('上游 error → 边结算 failed + 下游收到 [系统通知] 失败传播', async () => {
    const hops = mkHopRecorder();
    const deps = mkDeps({
      hops,
      callAgent: vi.fn(async ({ agentId, upstream }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) =>
        agentId === 'pm' ? { text: '', error: 'boom' } : { text: `${agentId}:up=${upstream.map((u) => u.from).join('+')}` }),
    });
    const r = await runTeamDagFanout(linear, deps, 'go');
    expect(r.perAgent.pm).toBe('error');
    // 失败传播：arch 的 upstream 里有 pm 的 [系统通知]
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    const archUp = calls.find((c) => c.agentId === 'arch').upstream as UpstreamMsg[];
    expect(archUp.some((u) => u.from === 'pm' && u.text.includes('[系统通知]'))).toBe(true);
    expect(archUp.some((u) => u.text.includes('执行失败'))).toBe(true);
    // hop：pm→arch 结算 failed（带原因），arch→dev 仍 done
    expect(hops.rows.find((r2) => r2.edge === 'pm→arch')!.status).toBe('failed');
    expect(hops.rows.find((r2) => r2.edge === 'pm→arch')!.detail).toContain('boom');
    expect(hops.rows.find((r2) => r2.edge === 'arch→dev')!.status).toBe('done');
  });

  it('上游空回复 → 边结算 empty + 下游收到系统通知', async () => {
    const hops = mkHopRecorder();
    const deps = mkDeps({
      hops,
      callAgent: vi.fn(async ({ agentId, upstream }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) =>
        agentId === 'pm' ? { text: '   ' } : { text: `${agentId}:ok` }),
    });
    await runTeamDagFanout(linear, deps, 'go');
    expect(hops.rows.find((r) => r.edge === 'pm→arch')!.status).toBe('empty');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    const archUp = calls.find((c) => c.agentId === 'arch').upstream as UpstreamMsg[];
    expect(archUp.some((u) => u.text.includes('空回复'))).toBe(true);
  });

  it('上游超时 → 边结算 timeout + 系统通知超时文案', async () => {
    const hops = mkHopRecorder();
    const deps = mkDeps({
      hops,
      callAgent: vi.fn(async ({ agentId, signal }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }): Promise<{ text: string; error?: string }> => {
        if (agentId !== 'pm') return { text: `${agentId}:ok` };
        return new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('timeout')), { once: true });
        });
      }),
    });
    const r = await runTeamDagFanout(linear, deps, 'go', { nodeTimeoutMs: 20, defaultMaxRetries: 0 });
    expect(r.perAgent.pm).toBe('timeout');
    expect(hops.rows.find((r2) => r2.edge === 'pm→arch')!.status).toBe('timeout');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    const archUp = calls.find((c) => c.agentId === 'arch').upstream as UpstreamMsg[];
    expect(archUp.some((u) => u.text.includes('超时未响应'))).toBe(true);
  });

  it('预先 abort → 全部边收尾兜底结算，不留悬空', async () => {
    const hops = mkHopRecorder();
    const deps = mkDeps({ hops });
    const ac = new AbortController();
    ac.abort();
    const r = await runTeamDagFanout(linear, deps, 'go', { signal: ac.signal });
    expect(r.aborted).toBe(true);
    expect(hops.rows).toHaveLength(2);
    expect(hops.rows.every((r2) => ['aborted', 'failed', 'timeout', 'empty', 'denied'].includes(r2.status))).toBe(true);
  });

  it('hops 不注入 → 行为与旧版一致（无记录路径零回归）', async () => {
    const deps = mkDeps();
    const r = await runTeamDagFanout(linear, deps, 'go');
    expect(r.perAgent).toEqual({ pm: 'ok', arch: 'ok', dev: 'ok' });
    expect(r.aborted).toBe(false);
  });
});
