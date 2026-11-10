/**
 * __tests__/team-persistence.test.ts — 组装落库（L2 闭环）单测
 *
 * 覆盖 server/src/team-persistence.ts:
 *   persistAssembledTeam — POST /api/agents × N → POST /api/teams 的顺序、
 *     payload 契约（soul=完整 persona / model / tools / workspace_id）、
 *     部分失败跳过、全败放弃、team 失败归 null、roster 按 node_id 去重。
 *   tapAssemblyTeamPersistence — 事件流 tap：收 <sf:node> roster，complete 前
 *     落库并插入 team-saved 帧；非组装流（无 node）零副作用透传。
 *
 * fetch 全部 mock（vi.stubGlobal），不打真实 Python。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/__tests__/team-persistence.test.ts
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  persistAssembledTeam,
  tapAssemblyTeamPersistence,
  deriveTeamName,
} from '../team-persistence';

const PY = 'http://localhost:8000';

interface FetchCall {
  url: string;
  method: string;
  body: Record<string, unknown>;
}

function parseCall(input: unknown, init?: { method?: string; body?: string }): FetchCall {
  return {
    url: String(input),
    method: init?.method ?? 'GET',
    body: init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : {},
  };
}

/** 造一个 fetch mock：agents 按 name 回 agent_id，teams 回 team record。 */
function stubFetchOk() {
  const calls: FetchCall[] = [];
  let agentSeq = 0;
  const fetchMock = vi.fn(async (input: unknown, init?: { method?: string; body?: string }) => {
    const call = parseCall(input, init);
    calls.push(call);
    if (call.url === `${PY}/api/agents`) {
      agentSeq += 1;
      return {
        ok: true,
        status: 200,
        json: async () => ({ data: { agent_id: `agent-${String(agentSeq).padStart(3, '0')}` } }),
      } as Response;
    }
    if (call.url === `${PY}/api/teams`) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {
            team_id: 'team-abc123',
            name: call.body.name,
            agent_ids: call.body.agent_ids,
            workspace_id: call.body.workspace_id ?? 'default',
          },
        }),
      } as Response;
    }
    throw new Error(`unexpected fetch: ${call.url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('persistAssembledTeam', () => {
  it('happy path: 先建 agents 再建 team，payload 契约正确', async () => {
    const { calls } = stubFetchOk();
    const team = await persistAssembledTeam({
      session_id: 'sess-1',
      goal: '组个论文评审团队',
      workspace_id: 'ws-1',
      roster: [
        { node_id: 'coord', title: '协调员', persona: '我是协调员 persona', model: 'claude-sonnet-4-6', tools: ['read_skill'] },
        { node_id: 'reader', title: '评审员', persona: '我是评审员 persona' },
      ],
    });

    expect(team).toEqual({
      team_id: 'team-abc123',
      name: '组个论文评审团队',
      agent_ids: ['agent-001', 'agent-002'],
      workspace_id: 'ws-1',
    });
    // 顺序：agents × 2 → teams × 1
    expect(calls.map((c) => c.url)).toEqual([
      `${PY}/api/agents`,
      `${PY}/api/agents`,
      `${PY}/api/teams`,
    ]);
    // agent payload：soul=完整 persona，model/tools/workspace_id 透传
    expect(calls[0].method).toBe('POST');
    expect(calls[0].body).toEqual({
      name: '协调员',
      soul: '我是协调员 persona',
      workspace_id: 'ws-1',
      model: 'claude-sonnet-4-6',
      tools: ['read_skill'],
    });
    // 无 model/tools 时不发明字段
    expect(calls[1].body).toEqual({
      name: '评审员',
      soul: '我是评审员 persona',
      workspace_id: 'ws-1',
    });
    // team payload
    expect(calls[2].body).toEqual({
      name: '组个论文评审团队',
      description: '组个论文评审团队',
      agent_ids: ['agent-001', 'agent-002'],
      workspace_id: 'ws-1',
    });
  });

  it('无 workspace_id 时不传该字段（Python 默认值兜底）', async () => {
    const { calls } = stubFetchOk();
    await persistAssembledTeam({
      session_id: 'sess-2',
      goal: 'hello',
      roster: [{ node_id: 'a', title: 'A' }],
    });
    expect(calls[0].body.workspace_id).toBeUndefined();
    expect(calls[1].body.workspace_id).toBeUndefined();
  });

  it('单个 agent 落库失败 → 跳过该成员，team 仍用其余成员创建', async () => {
    const fetchMock = vi.fn(async (input: unknown, init?: { method?: string; body?: string }) => {
      const call = parseCall(input, init);
      if (call.url === `${PY}/api/agents` && call.body.name === '坏成员') {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      if (call.url === `${PY}/api/agents`) {
        return { ok: true, status: 200, json: async () => ({ data: { agent_id: 'agent-ok1' } }) } as Response;
      }
      return {
        ok: true,
        status: 201,
        json: async () => ({ data: { team_id: 'team-x', name: call.body.name, agent_ids: call.body.agent_ids, workspace_id: 'default' } }),
      } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    const team = await persistAssembledTeam({
      session_id: 'sess-3',
      goal: 'g',
      roster: [
        { node_id: 'bad', title: '坏成员' },
        { node_id: 'good', title: '好成员' },
      ],
    });
    expect(team?.agent_ids).toEqual(['agent-ok1']);
  });

  it('全部 agent 落库失败 → 返回 null，且不发 POST /api/teams', async () => {
    const calls: FetchCall[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: { method?: string; body?: string }) => {
      calls.push(parseCall(input, init));
      return { ok: false, status: 500, json: async () => ({}) } as Response;
    }));
    const team = await persistAssembledTeam({
      session_id: 'sess-4',
      goal: 'g',
      roster: [{ node_id: 'a', title: 'A' }],
    });
    expect(team).toBeNull();
    expect(calls.every((c) => c.url === `${PY}/api/agents`)).toBe(true);
  });

  it('team POST 失败 → 返回 null（agents 已建也不抛）', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agents')) {
        return { ok: true, status: 200, json: async () => ({ data: { agent_id: 'agent-1' } }) } as Response;
      }
      return { ok: false, status: 422, json: async () => ({}) } as Response;
    }));
    const team = await persistAssembledTeam({
      session_id: 'sess-5',
      goal: 'g',
      roster: [{ node_id: 'a', title: 'A' }],
    });
    expect(team).toBeNull();
  });

  it('roster 按 node_id 去重（后到覆盖先到），网络异常归 null', async () => {
    const { calls } = stubFetchOk();
    const team = await persistAssembledTeam({
      session_id: 'sess-6',
      goal: 'g',
      roster: [
        { node_id: 'a', title: '旧标题' },
        { node_id: 'a', title: '新标题', persona: '新 persona' },
      ],
    });
    expect(team?.agent_ids).toHaveLength(1);
    expect(calls[0].body.name).toBe('新标题');
    expect(calls[0].body.soul).toBe('新 persona');

    // 网络层 throw → null，不抛回调用方
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('ECONNREFUSED'); }));
    const t2 = await persistAssembledTeam({
      session_id: 'sess-7',
      goal: 'g',
      roster: [{ node_id: 'a', title: 'A' }],
    });
    expect(t2).toBeNull();
  });

  it('空 roster → null，零网络调用', async () => {
    const { fetchMock } = stubFetchOk();
    const team = await persistAssembledTeam({ session_id: 'sess-8', goal: 'g', roster: [] });
    expect(team).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('deriveTeamName', () => {
  it('取 goal 首个非空行，截断 40 字符；空 goal 兜底「新团队」', () => {
    expect(deriveTeamName('')).toBe('新团队');
    expect(deriveTeamName('\n\n  组个全栈小队\n第二行')).toBe('组个全栈小队');
    expect(deriveTeamName('x'.repeat(100))).toHaveLength(40);
  });
});

describe('tapAssemblyTeamPersistence', () => {
  type Ev = { event: string; data: unknown };
  async function* fakeStream(evs: Ev[]): AsyncGenerator<Ev> {
    for (const e of evs) yield e;
  }
  async function collect(stream: AsyncGenerator<Ev>): Promise<Ev[]> {
    const out: Ev[] = [];
    for await (const e of stream) out.push(e);
    return out;
  }

  it('收 node roster → complete 前落库并先插 team-saved 帧', async () => {
    const { calls } = stubFetchOk();
    const out = await collect(
      tapAssemblyTeamPersistence(
        fakeStream([
          { event: 'node', data: { node_id: 'coord', title: '协调员', persona: 'p1', tools_picked: ['read_skill'] } },
          { event: 'node', data: { node_id: 'writer', title: '写手', sub: '写报告' } },
          { event: 'edge', data: { from: 'coord', to: 'writer' } },
          { event: 'complete', data: { session_id: 's' } },
        ]),
        { session_id: 's', goal: '组个写作团队', workspace_id: 'ws-9' },
      ),
    );

    expect(out.map((e) => e.event)).toEqual(['node', 'node', 'edge', 'team-saved', 'complete']);
    const saved = out[3].data as { team_id: string; agent_ids: string[]; workspace_id: string };
    expect(saved.team_id).toBe('team-abc123');
    expect(saved.agent_ids).toEqual(['agent-001', 'agent-002']);
    expect(saved.workspace_id).toBe('ws-9');
    // sub 在无 persona 时补位为 soul
    expect(calls[1].body.soul).toBe('写报告');
    expect(calls[0].body.tools).toEqual(['read_skill']);
  });

  it('同 node_id 重复 emit → upsert（只建一个 agent，用到后到的 persona）', async () => {
    const { calls } = stubFetchOk();
    await collect(
      tapAssemblyTeamPersistence(
        fakeStream([
          { event: 'node', data: { node_id: 'a', title: 'A', persona: 'old' } },
          { event: 'node', data: { node_id: 'a', title: 'A', persona: 'new' } },
          { event: 'complete', data: {} },
        ]),
        { session_id: 's', goal: 'g' },
      ),
    );
    expect(calls.filter((c) => c.url === `${PY}/api/agents`)).toHaveLength(1);
    expect(calls[0].body.soul).toBe('new');
  });

  it('非组装流（无 node 事件）→ 原样透传，零网络调用', async () => {
    const { fetchMock } = stubFetchOk();
    const out = await collect(
      tapAssemblyTeamPersistence(
        fakeStream([
          { event: 'text', data: { text: '你好' } },
          { event: 'complete', data: { session_id: 's' } },
        ]),
        { session_id: 's', goal: 'g' },
      ),
    );
    expect(out.map((e) => e.event)).toEqual(['text', 'complete']);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('落库失败（全 500）→ 不插 team-saved，complete 仍放行', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response));
    const out = await collect(
      tapAssemblyTeamPersistence(
        fakeStream([
          { event: 'node', data: { node_id: 'a', title: 'A' } },
          { event: 'complete', data: {} },
        ]),
        { session_id: 's', goal: 'g' },
      ),
    );
    expect(out.map((e) => e.event)).toEqual(['node', 'complete']);
  });
});
