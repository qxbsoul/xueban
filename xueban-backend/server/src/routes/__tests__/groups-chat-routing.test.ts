/**
 * groups-chat-routing.test.ts — 意图识别 + 策略矩阵路由行为测试（2026-11-07，GrokBot 范式）。
 *
 * 沿用 groups-chat-mention 的 mock 脚手架（内存 backend/dispatcher/gw），验证
 * 「对的人、对的回复」——任何路径都不再原文全员广播：
 *   - 专长路由：任务/问答只派最对口的 1 人（DAG 子图收缩到该节点）；
 *   - 多子句有先后词 → pipeline 链式 depends_on、每人 prompt 自包含；
 *   - 多子句无先后词 → parallel 分工；
 *   - 讨论 → roundtable 2-3 人给观点（不是全员）；全员号召 → all-hands 才全跑；
 *   - 无信号 → 单人不广播；寒暄 → 首位单人接话；
 *   - LLM 编排者计划优先、失败降级启发式；intent 帧 reason 可解释（策略矩阵文案）。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-routing.test.ts
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── mock 重依赖（模块级，import 路由前生效）────────────────────────────────

// team 真源：四工种线性团队 pm→dev→design→ops（专长互不重叠）。
const teamShape = {
  members: ['pm', 'dev', 'design', 'ops'],
  edges: [
    { from: 'pm', to: 'dev', kind: 'sequential' },
    { from: 'dev', to: 'design', kind: 'sequential' },
    { from: 'design', to: 'ops', kind: 'sequential' },
  ],
  policy_matrix: {},
  name: '四工种小队',
};
vi.mock('../../lib/team-source', () => ({
  loadTeamForRun: vi.fn(() => ({ team: teamShape, errors: [] })),
}));

// agent 读取：id→记录（soul = 专长词，供 scoreAgentFit 打分 + dispatcher 反查）。
const agentRecords: Record<string, { agent_id: string; name: string; soul: string }> = {
  pm: { agent_id: 'pm', name: '策划喵', soul: '产品 需求 用户故事 路线图' },
  dev: { agent_id: 'dev', name: '码农鸡', soul: '开发 代码 后端 接口 数据库 sql 性能 bug 修复' },
  design: { agent_id: 'design', name: '设计鹅', soul: '设计 ui 界面 海报 配色' },
  ops: { agent_id: 'ops', name: '运营鹅', soul: '运营 发布 推广 增长' },
};

// groups-backend：群记录 + 消息落库内存化。
const groupRecord = {
  group_id: 'g1',
  agent_ids: ['pm', 'dev', 'design', 'ops'],
  messages: [] as Array<{ content: string; sender_kind: string; sender_name: string }>,
  kind: 'group',
  /** 默认绑团队 → DAG 路径；串行用例改 members=[] 模拟 team 读不到。 */
  team_id: 't-default' as string | null,
};
const postedMessages: Array<{ content: string; sender_name: string; sender_kind: string; agent_id?: string }> = [];
vi.mock('../../storage/groups-backend', () => ({
  getGroupsBackend: () => ({
    getGroup: vi.fn(async (id: string) => ({ ...groupRecord, group_id: id })),
    appendMessage: vi.fn(async (_id: string, body: { content: string; sender_name: string; sender_kind: string; agent_id?: string }) => {
      postedMessages.push(body);
      groupRecord.messages.push(body as never);
      return { ...body, message_id: `m-${postedMessages.length}` };
    }),
  }),
}));

// HTTP 打 Python 的路径（agent 读取走全局 fetch → stub 掉）。
vi.stubGlobal('fetch', vi.fn(async (url: string) => {
  const m = /\/api\/agents\/([^/]+)$/.exec(String(url));
  if (m) {
    const a = agentRecords[decodeURIComponent(m[1])];
    if (a) {
      return new Response(JSON.stringify({ data: a }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }
  return new Response('{}', { status: 404 });
}));
vi.mock('../../python-backend', () => ({ PYTHON_BACKEND_URL: 'http://127.0.0.1:1' }));

// dispatcher：resolveCallable 返回可观测 fake。
const turnPrompts: Array<{ agentId: string; prompt: string }> = [];
vi.mock('../../transport/dispatcher', () => ({
  resolveCallable: (_executor: string) => ({
    turn: async function* (args: { system: string; prompt: string }) {
      // 专长 soul 反查 agentId（测试观测用）。
      const agentId = Object.entries(agentRecords).find(
        ([, a]) => a.soul === args.system,
      )?.[0] ?? 'unknown';
      turnPrompts.push({ agentId, prompt: args.prompt });
      yield { type: 'text-delta', value: `${agentId} 收到` };
    },
  }),
}));

// gateway run：内存 gw + 帧捕获（intent/plan_card 断言用）。
const gwMessages: Array<Record<string, unknown>> = [];
vi.mock('../../lib/gateway-run', () => ({
  startGatewayRun: vi.fn(() => {
    let n = 0;
    return {
      turnId: 'turn-1',
      nextId: () => `gw-${++n}`,
      emitMessage: vi.fn((m: Record<string, unknown>) => {
        gwMessages.push(m);
      }),
      emitPatch: vi.fn(),
      finish: vi.fn(),
    };
  }),
}));

// worktree：非 git 环境短路。
vi.mock('../../lib/worktree-manager', () => ({
  isGitWorkable: vi.fn(async () => false),
  prepareWorktree: vi.fn(async () => null),
  integrateAll: vi.fn(async () => []),
  commitWorktree: vi.fn(async () => null),
}));

// LLM 编排者：整模块 mock（计划对象可控；启发式用例走 cli:claude 不触达）。
vi.mock('../../lib/orchestrator-parse', () => ({
  planWithLlm: vi.fn(async () => null),
}));

import { runFanoutAsync } from '../groups-chat';
import { planWithLlm } from '../../lib/orchestrator-parse';

function intentFrames(): Array<Record<string, unknown>> {
  return gwMessages.filter((m) => m.kind === 'intent');
}
function ranAgents(): string[] {
  return turnPrompts.map((t) => t.agentId);
}
/** 稳定排序后的执行名单（DAG 并行批次内顺序不保证时用）。 */
function ranSorted(): string[] {
  return ranAgents().slice().sort();
}

beforeEach(() => {
  postedMessages.length = 0;
  groupRecord.messages = [];
  turnPrompts.length = 0;
  gwMessages.length = 0;
  (teamShape as { members: string[] }).members = ['pm', 'dev', 'design', 'ops'];
  groupRecord.team_id = 't-default';
  vi.mocked(planWithLlm).mockReset().mockImplementation(async () => null);
});

// ── DAG 路径：专长路由 + 策略矩阵 ──────────────────────────────────────────

describe('runFanout — 策略矩阵路由（DAG 路径）', () => {
  it('任务 → 专家应答：只跑最对口的 dev（子图收缩，不全员广播）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '帮我修复登录页的前端 bug');
    expect(ranAgents()).toEqual(['dev']);
    // 派发卡 reason 可解释：意图 · 策略 · 参与者 · 路由来源。
    const reason = String(intentFrames()[0]?.reason ?? '');
    expect(reason).toContain('策略：专家应答');
    expect(reason).toContain('码农鸡');
    expect(reason).toContain('策略矩阵路由');
  });

  it('问答 → 专家应答：SQL 问题只派 dev，意图标「问答」', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, 'SQL 查询慢是什么原因？');
    expect(ranAgents()).toEqual(['dev']);
    expect(String(intentFrames()[0]?.reason ?? '')).toContain('意图：问答');
  });

  it('多子句有先后词 → pipeline：pm→dev 链式执行、prompt 自包含', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '先帮我整理需求文档，然后实现登录接口');
    expect(ranAgents()).toEqual(['pm', 'dev']);
    expect(turnPrompts[0]!.prompt).toContain('整理需求文档');
    expect(turnPrompts[0]!.prompt).toContain('整体需求');
    expect(turnPrompts[1]!.prompt).toContain('实现登录接口');
    expect(turnPrompts[1]!.prompt).toContain('整体需求');
    // plan_card 标题带策略标识（前端简洁呈现：一行看懂编排策略）。
    const planCard = gwMessages.find((m) => m.kind === 'plan_card');
    expect(String(planCard?.title ?? '')).toContain('流水线拆解');
  });

  it('全员号召 → all-hands：全员参与（此时才整图执行）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '大家好，开始干活');
    expect(ranSorted()).toEqual(['design', 'dev', 'ops', 'pm']);
    expect(String(intentFrames()[0]?.reason ?? '')).toContain('全员参与');
  });

  it('寒暄「hi」→ 首位单人接话，不进 DAG 不广播', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, 'hi');
    expect(ranAgents()).toEqual(['pm']);
  });
});

// ── 串行降级路径（无 team）：策略矩阵同样生效 ───────────────────────────────

describe('runFanout — 策略矩阵路由（串行降级路径）', () => {
  beforeEach(() => {
    // team 读不到（members 空）→ 串行 fan-out。
    (teamShape as { members: string[] }).members = [];
  });

  it('多子句无先后词 → parallel：pm/design 分工，prompt 各含自己的子句', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '帮我整理一下需求文档，帮我设计一张活动海报');
    expect(ranAgents()).toEqual(['pm', 'design']);
    expect(turnPrompts[0]!.prompt).toContain('整理一下需求文档');
    expect(turnPrompts[0]!.prompt).toContain('整体需求');
    expect(turnPrompts[1]!.prompt).toContain('设计一张活动海报');
    const reason = String(intentFrames()[0]?.reason ?? '');
    expect(reason).toContain('策略：并行拆分');
    expect(reason).toContain('工单：');
  });

  it('讨论 → roundtable：2-3 人给观点（第 4 人不参与，不是全员）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '帮我选 A 还是 B');
    expect(ranAgents()).toEqual(['pm', 'dev', 'design']);
    expect(ranAgents()).not.toContain('ops');
    expect(turnPrompts[0]!.prompt).toContain('观点');
    expect(String(intentFrames()[0]?.reason ?? '')).toContain('圆桌讨论');
  });

  it('无信号 → 单人不广播（花名册首位兜底）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '登录页白屏');
    expect(ranAgents()).toEqual(['pm']);
    expect(turnPrompts[0]!.prompt).toBe('登录页白屏');
  });

  it('寒暄串行同样收缩：「谢谢」只回一条', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '谢谢');
    expect(ranAgents()).toEqual(['pm']);
  });
});

// ── LLM 编排者计划：优先采纳、失败降级启发式 ───────────────────────────────

describe('runFanout — LLM 编排者计划', () => {
  const LLM_HEADERS = { 'x-llm-provider': 'zhipu', 'x-llm-key': 'sk-test' };

  it('LLM 计划优先：工单 prompt 原样派发，reason 标「编排者拆解」', async () => {
    vi.mocked(planWithLlm).mockResolvedValueOnce({
      intent: 'task',
      strategy: 'pipeline',
      source: 'llm',
      tasks: [
        { agentId: 'pm', title: '整理需求', prompt: 'LLM工单A：整理需求文档' },
        { agentId: 'dev', title: '实现接口', prompt: 'LLM工单B：实现登录接口', depends_on: ['pm'] },
      ],
    });
    await runFanoutAsync('g1', 'byok:zhipu', undefined, LLM_HEADERS, '先整理需求再做接口');
    expect(vi.mocked(planWithLlm)).toHaveBeenCalledTimes(1);
    expect(ranAgents()).toEqual(['pm', 'dev']);
    expect(turnPrompts[0]!.prompt).toContain('LLM工单A');
    expect(turnPrompts[1]!.prompt).toContain('LLM工单B');
    const reason = String(intentFrames()[0]?.reason ?? '');
    expect(reason).toContain('编排者拆解');
    expect(reason).toContain('流水线拆解');
    // 编排者拿到的是带专长 soul 的完整花名册。
    const arg = vi.mocked(planWithLlm).mock.calls[0]![0] as { agents: Array<{ id: string }>; provider: string; apiKey?: string };
    expect(arg.agents.map((a) => a.id).sort()).toEqual(['design', 'dev', 'ops', 'pm']);
    expect(arg.provider).toBe('zhipu');
    expect(arg.apiKey).toBe('sk-test');
  });

  it('LLM 计划失败 → 启发式策略矩阵兜底（问答仍单人对口）', async () => {
    await runFanoutAsync('g1', 'byok:zhipu', undefined, LLM_HEADERS, 'SQL 查询慢是什么原因？');
    expect(vi.mocked(planWithLlm)).toHaveBeenCalledTimes(1);
    expect(ranAgents()).toEqual(['dev']);
    expect(turnPrompts[0]!.prompt).toContain('SQL');
    expect(String(intentFrames()[0]?.reason ?? '')).toContain('策略矩阵路由');
  });
});
