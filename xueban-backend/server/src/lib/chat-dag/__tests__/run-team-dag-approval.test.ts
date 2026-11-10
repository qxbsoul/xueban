/**
 * run-team-dag-approval.test.ts — approve 边人工审批门测试（2026-11-05）。
 *
 * 覆盖 run-team-dag.ts 的 deps.approval / onNodeBlocked 路径：
 *   - 带 approve 入边的节点：执行前挂起 request；approved → 照常执行
 *   - rejected → 节点标 'blocked'，不调 callAgent、不落产出
 *   - blocked 上游 → 出边下游收到 [系统通知] 未通过人工审批（失败传播）
 *   - blocked → 出边 hop 结算 denied（收尾兜底）
 *   - approval 不注入 → approve 边按 direct 处理（旧版行为，零回归）
 *   - 无 approve 入边的节点不触发 request
 */

import { describe, it, expect, vi, type Mock } from 'vitest';
import { runTeamDagFanout, type RunTeamDagDeps, type UpstreamMsg } from '../run-team-dag';
import type { TeamRunShape } from '../../team-source';

type ApprovalReqArgs = { nodeId: string; nodeName: string; preview: string; signal: AbortSignal };

function mkApprovalRequest(
  impl: (a: ApprovalReqArgs) => Promise<'approved' | 'rejected'>,
): Mock<(a: ApprovalReqArgs) => Promise<'approved' | 'rejected'>> {
  return vi.fn(impl);
}

function mkDeps(over: Partial<RunTeamDagDeps> = {}): RunTeamDagDeps & {
  posted: { sender_name: string; sender_kind: string; content: string }[];
  callOrder: string[];
} {
  const posted: { sender_name: string; sender_kind: string; content: string }[] = [];
  const callOrder: string[] = [];
  const deps: RunTeamDagDeps & { posted: typeof posted; callOrder: string[] } = {
    getAgent: vi.fn(async (id: string) => ({ name: id.toUpperCase(), soul: `soul-${id}` })),
    callAgent: vi.fn(async ({ agentId, upstream }: { agentId: string; upstream: UpstreamMsg[] }) => {
      callOrder.push(agentId);
      return { text: `${agentId}:reply(up=${upstream.map((u) => u.from).join('+') || 'none'})` };
    }),
    postMessage: vi.fn(async (m) => {
      posted.push({ sender_name: m.sender_name, sender_kind: m.sender_kind, content: m.content });
    }),
    posted, callOrder,
    ...over,
  };
  return deps;
}

/** pm →(approve)→ dev 两人链。 */
const approveChain: TeamRunShape = {
  members: ['pm', 'dev'],
  edges: [{ from: 'pm', to: 'dev', kind: 'approve' }],
  policy_matrix: {},
};

describe('runTeamDagFanout — approve 边人工审批门', () => {
  it('approved → 挂起后放行，节点照常执行', async () => {
    const request = mkApprovalRequest(async () => 'approved');
    const deps = mkDeps({ approval: { request } });
    const r = await runTeamDagFanout(approveChain, deps, 'do it');
    expect(r.perAgent).toEqual({ pm: 'ok', dev: 'ok' });
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0]![0].nodeId).toBe('dev');
    expect(request.mock.calls[0]![0].nodeName).toBe('DEV');
    // pm 没被审批（无 approve 入边）
    expect(deps.callOrder).toEqual(['pm', 'dev']);
    // 挂起/放行系统消息都在
    expect(deps.posted.some((m) => m.content.includes('等待人工审批'))).toBe(true);
    expect(deps.posted.some((m) => m.content.includes('已获批准'))).toBe(true);
  });

  it('rejected → 节点 blocked，不执行不落产出，postMessage 有拒绝通知', async () => {
    const onNodeBlocked = vi.fn();
    const request = mkApprovalRequest(async () => 'rejected');
    const deps = mkDeps({ approval: { request }, onNodeBlocked });
    const r = await runTeamDagFanout(approveChain, deps, 'do it');
    expect(r.perAgent).toEqual({ pm: 'ok', dev: 'blocked' });
    expect(deps.callOrder).toEqual(['pm']); // dev 没跑
    expect(onNodeBlocked).toHaveBeenCalledWith('dev');
    expect(deps.posted.some((m) => m.content.includes('执行被拒绝'))).toBe(true);
    expect(deps.posted.some((m) => m.sender_name === 'DEV')).toBe(false);
  });

  it('blocked 上游失败传播 — dev blocked 时其出边下游收到审批拒绝系统通知', async () => {
    // pm →(approve)→ dev →(sequential)→ qa
    const team: TeamRunShape = {
      members: ['pm', 'dev', 'qa'],
      edges: [
        { from: 'pm', to: 'dev', kind: 'approve' },
        { from: 'dev', to: 'qa', kind: 'sequential' },
      ],
      policy_matrix: {},
    };
    const request = mkApprovalRequest(async (a) =>
      a.nodeId === 'dev' ? 'rejected' : 'approved');
    const deps = mkDeps({ approval: { request } });
    await runTeamDagFanout(team, deps, 'go');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    const qaUp = calls.find((c) => c.agentId === 'qa')?.upstream as UpstreamMsg[];
    expect(qaUp.some((u) => u.from === 'dev' && u.text.includes('未通过人工审批'))).toBe(true);
  });

  it('blocked → 出边 hop 收尾兜底结算 denied', async () => {
    const rows: Array<{ edge: string; status: string; detail: string }> = [];
    const hops = {
      open: (e: { from: string; to: string }) => {
        rows.push({ edge: `${e.from}→${e.to}`, status: 'pending', detail: '' });
        return `hop-${rows.length}`;
      },
      start: () => {},
      settle: (id: string, status: 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted', opts?: { detail?: string }) => {
        const row = rows[Number(id.slice(4)) - 1]!;
        if (row.status === 'pending') {
          row.status = status;
          row.detail = opts?.detail ?? '';
        }
      },
    };
    const request = mkApprovalRequest(async () => 'rejected');
    const deps = mkDeps({ hops, approval: { request } });
    const r = await runTeamDagFanout(approveChain, deps, 'go');
    expect(r.perAgent.dev).toBe('blocked');
    // pm→dev 边在 dev 收 upstream 时已按 done 正常结算（上游产出正常传递）；
    // 「审批被拒」的 denied 结算落在 dev→下游 的出边上。本链 dev 无出边 →
    // 验证点改为：denied 语义只出现在带出边的 blocked 节点（见上一用例）。
    expect(rows.find((x) => x.edge === 'pm→dev')).toMatchObject({ status: 'done' });
  });

  it('blocked 且带出边 → 出边收尾兜底结算 denied（审计可辨）', async () => {
    const rows: Array<{ edge: string; status: string; detail: string }> = [];
    const hops = {
      open: (e: { from: string; to: string }) => {
        rows.push({ edge: `${e.from}→${e.to}`, status: 'pending', detail: '' });
        return `hop-${rows.length}`;
      },
      start: () => {},
      settle: (id: string, status: 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted', opts?: { detail?: string }) => {
        const row = rows[Number(id.slice(4)) - 1]!;
        if (row.status === 'pending') {
          row.status = status;
          row.detail = opts?.detail ?? '';
        }
      },
    };
    const team: TeamRunShape = {
      members: ['pm', 'dev', 'qa'],
      edges: [
        { from: 'pm', to: 'dev', kind: 'approve' },
        { from: 'dev', to: 'qa', kind: 'sequential' },
      ],
      policy_matrix: {},
    };
    const request = mkApprovalRequest(async (a) =>
      a.nodeId === 'dev' ? 'rejected' : 'approved');
    const deps = mkDeps({ hops, approval: { request } });
    const r = await runTeamDagFanout(team, deps, 'go');
    expect(r.perAgent.dev).toBe('blocked');
    expect(rows.find((x) => x.edge === 'dev→qa')).toMatchObject({
      status: 'denied',
      detail: expect.stringContaining('审批被拒'),
    });
  });

  it('approval 不注入 → approve 边按 direct 处理（零回归）', async () => {
    const deps = mkDeps();
    const r = await runTeamDagFanout(approveChain, deps, 'go');
    expect(r.perAgent).toEqual({ pm: 'ok', dev: 'ok' });
    expect(deps.posted.every((m) => !m.content.includes('审批'))).toBe(true);
  });

  it('只有 approve 入边的节点触发 request；纯 sequential 节点不触发', async () => {
    const team: TeamRunShape = {
      members: ['a', 'b', 'c'],
      edges: [
        { from: 'a', to: 'b', kind: 'sequential' },
        { from: 'b', to: 'c', kind: 'approve' },
      ],
      policy_matrix: {},
    };
    const request = mkApprovalRequest(async () => 'approved');
    const deps = mkDeps({ approval: { request } });
    await runTeamDagFanout(team, deps, 'go');
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0]![0].nodeId).toBe('c');
  });
});
