import { describe, it, expect, vi } from 'vitest';
import { runTeamDagFanout, type RunTeamDagDeps, type UpstreamMsg } from '../run-team-dag';
import type { TeamRunShape } from '../../team-source';

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

describe('runTeamDagFanout', () => {
  it('线性链按层序跑:pm→arch→dev', async () => {
    const deps = mkDeps();
    await runTeamDagFanout(linear, deps, 'user prompt');
    expect(deps.callOrder).toEqual(['pm', 'arch', 'dev']);
  });

  it('每个 agent 的回复经 postMessage 落库(sender_kind=agent)', async () => {
    const deps = mkDeps();
    await runTeamDagFanout(linear, deps, 'go');
    const agentMsgs = deps.posted.filter((p) => p.sender_kind === 'agent');
    expect(agentMsgs.map((m) => m.sender_name)).toEqual(['PM', 'ARCH', 'DEV']);
  });

  it('下游 agent 收到上游产出作为 upstream 上下文', async () => {
    const deps = mkDeps();
    await runTeamDagFanout(linear, deps, 'go');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(calls.find((c) => c.agentId === 'arch').upstream.map((u: { from: string }) => u.from)).toEqual(['pm']);
    expect(calls.find((c) => c.agentId === 'dev').upstream.map((u: { from: string }) => u.from)).toEqual(['arch']);
  });

  it('同层并行:菱形 b,c 在同层都被调用,且都在 a 之后、d 之前', async () => {
    const diamond: TeamRunShape = {
      members: ['a', 'b', 'c', 'd'],
      edges: [{ from: 'a', to: 'b' }, { from: 'a', to: 'c' }, { from: 'b', to: 'd' }, { from: 'c', to: 'd' }].map((e) => ({ ...e, kind: 'sequential' as const })),
      policy_matrix: {},
    };
    const deps = mkDeps();
    await runTeamDagFanout(diamond, deps, 'go');
    expect(deps.callOrder[0]).toBe('a');
    expect(deps.callOrder.slice(1, 3).sort()).toEqual(['b', 'c']);
    expect(deps.callOrder[3]).toBe('d');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(calls.find((c) => c.agentId === 'd').upstream.map((u: { from: string }) => u.from).sort()).toEqual(['b', 'c']);
  });

  it('deny 边:上游产出不喂下游 + 发系统注释', async () => {
    const team: TeamRunShape = { ...linear, policy_matrix: { pm: { arch: 'deny' } } };
    const deps = mkDeps();
    const r = await runTeamDagFanout(team, deps, 'go');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(calls.find((c) => c.agentId === 'arch').upstream).toEqual([]);
    expect(r.deniedEdges).toBe(1);
    expect(deps.posted.some((p) => p.sender_kind === 'system' && /deny|拦/.test(p.content))).toBe(true);
  });

  it('warn 边:上游产出仍喂下游 + 发提醒注释', async () => {
    const team: TeamRunShape = { ...linear, policy_matrix: { pm: { arch: 'warn' } } };
    const deps = mkDeps();
    const r = await runTeamDagFanout(team, deps, 'go');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(calls.find((c) => c.agentId === 'arch').upstream.map((u: { from: string }) => u.from)).toEqual(['pm']);
    expect(r.warnedEdges).toBe(1);
    expect(deps.posted.some((p) => p.sender_kind === 'system' && /warn|提醒/.test(p.content))).toBe(true);
  });

  it('agent 报错 → 发系统错误消息、记 error、不中断后续层', async () => {
    const deps = mkDeps({ callAgent: vi.fn(async ({ agentId }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => (agentId === 'pm' ? { text: '', error: 'boom' } : { text: `${agentId}:ok` })) });
    const r = await runTeamDagFanout(linear, deps, 'go');
    expect(r.perAgent.pm).toBe('error');
    expect(r.perAgent.dev).toBe('ok');
    expect(deps.posted.some((p) => p.sender_kind === 'system' && /boom|出错/.test(p.content))).toBe(true);
  });

  it('agent 空回复 → 记 empty + 系统提示,下游收到失败传播通知(2026-11-05)', async () => {
    const deps = mkDeps({ callAgent: vi.fn(async ({ agentId }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => (agentId === 'pm' ? { text: '   ' } : { text: `${agentId}:ok` })) });
    const r = await runTeamDagFanout(linear, deps, 'go');
    expect(r.perAgent.pm).toBe('empty');
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    // 2026-11-05 失败传播 — pm 空回复不再静默:arch 的 upstream 带 [系统通知]。
    const archUp = calls.find((c) => c.agentId === 'arch').upstream;
    expect(archUp).toHaveLength(1);
    expect(archUp[0].from).toBe('pm');
    expect(archUp[0].text).toContain('[系统通知]');
    expect(archUp[0].text).toContain('空回复');
  });

  it('getAgent 返回 null → 该节点跳过、记 error,不喂下游', async () => {
    const deps = mkDeps({ getAgent: vi.fn(async (id: string) => (id === 'pm' ? null : { name: id, soul: 's' })) });
    const r = await runTeamDagFanout(linear, deps, 'go');
    expect(r.perAgent.pm).toBe('error');
  });

  it('warn 边但上游空回复 → warnedEdges 仍计数 + 失败传播通知(2026-11-05)', async () => {
    const team: TeamRunShape = { ...linear, policy_matrix: { pm: { arch: 'warn' } } };
    const deps = mkDeps({ callAgent: vi.fn(async ({ agentId, upstream }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => (agentId === 'pm' ? { text: '   ' } : { text: `${agentId}:up=${upstream.map((u) => u.from).join('+') || 'none'}` })) });
    const r = await runTeamDagFanout(team, deps, 'go');
    expect(r.warnedEdges).toBe(1);
    const calls = (deps.callAgent as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    // warn 提醒仍发（无产出文案）；2026-11-05 起 arch 的 upstream 另带 [系统通知]。
    expect(deps.posted.some((p) => p.sender_kind === 'system' && /无产出/.test(p.content))).toBe(true);
    const archUp = calls.find((c) => c.agentId === 'arch').upstream;
    expect(archUp).toHaveLength(1);
    expect(archUp[0].text).toContain('[系统通知]');
  });

  it('opts.signal 预先 abort → 不跑任何 agent,全部 perAgent=aborted', async () => {
    const deps = mkDeps();
    const ac = new AbortController(); ac.abort();
    const r = await runTeamDagFanout(linear, deps, 'go', { signal: ac.signal });
    expect(deps.callOrder).toEqual([]);
    expect(r.perAgent).toEqual({ pm: 'aborted', arch: 'aborted', dev: 'aborted' });
  });

  it('节点持续 error + maxRetries 重试 → 最终标 error,且重试了', async () => {
    const calls: string[] = [];
    const deps = mkDeps({
      callAgent: vi.fn(async ({ agentId }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => { calls.push(agentId); return agentId === 'pm' ? { text: '', error: 'boom' } : { text: `${agentId}:ok` }; }),
    });
    const r = await runTeamDagFanout(linear, deps, 'go', { defaultMaxRetries: 2 });
    expect(r.perAgent.pm).toBe('error');
    expect(calls.filter((c) => c === 'pm').length).toBe(3); // 初次 + 2 重试
  });

  it('节点超时 → 标 timeout', async () => {
    const deps = mkDeps({
      callAgent: vi.fn((args: Parameters<RunTeamDagDeps['callAgent']>[0]) => new Promise<{ text: string; error?: string }>((_, reject) => { args.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }); })),
    });
    const r = await runTeamDagFanout(
      { members: ['solo'], edges: [], policy_matrix: {} },
      deps, 'go', { nodeTimeoutMs: 20, defaultMaxRetries: 0 },
    );
    expect(r.perAgent.solo).toBe('timeout');
  });

  it('同层并发不超过 maxConcurrency', async () => {
    const team: TeamRunShape = { members: ['a', 'b', 'c', 'd'], edges: [], policy_matrix: {} };
    let active = 0; let peak = 0;
    const deps = mkDeps({
      callAgent: (async ({ agentId }: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => {
        active++; peak = Math.max(peak, active);
        await new Promise((r) => setTimeout(r, 15));
        active--;
        return { text: `${agentId}:ok` };
      }),
    });
    await runTeamDagFanout(team, deps, 'go', { maxConcurrency: 2 });
    expect(peak).toBeLessThanOrEqual(2);
  });
});
