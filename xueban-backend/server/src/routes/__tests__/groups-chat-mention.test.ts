/**
 * groups-chat-mention.test.ts — @ 提及定向派活路由行为测试（2026-11-05，OpenBot 语义）。
 *
 * 用 vi.mock 把 runFanout 的重依赖（Python 群读取 / team 真源 / dispatcher）替换为
 * 内存实现，直接驱动导出的 runFanoutAsync（测试专用薄导出），验证：
 *   - @ 命中成员 → DAG 路径只跑被点名节点（LLM 拆解跳过）；
 *   - DAG 子图收缩：被点名成员间的边保留，其他人不跑；
 *   - 串行路径（无 team_id）@ 命中 → 只派发被提及者；
 *   - 无 @ / @ 未命中成员 → 现状行为（全员），零回归；
 *   - cleanText：@token 从派发 prompt 中剔除。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-mention.test.ts
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── mock 重依赖（模块级，import 路由前生效）────────────────────────────────

// team 真源：固定线性团队 pm→arch→dev（dev 带 conditional 返工出边由用例自定）。
const teamShape = {
  members: ['pm', 'arch', 'dev'],
  edges: [
    { from: 'pm', to: 'arch', kind: 'sequential' },
    { from: 'arch', to: 'dev', kind: 'sequential' },
  ],
  policy_matrix: {},
  name: '三件套',
};
vi.mock('../../lib/team-source', () => ({
  loadTeamForRun: vi.fn(() => ({ team: teamShape, errors: [] })),
}));

// agent 读取：id→记录。
const agentRecords: Record<string, { agent_id: string; name: string; soul: string }> = {
  pm: { agent_id: 'pm', name: '策划喵', soul: 'soul-pm' },
  arch: { agent_id: 'arch', name: '架构汪', soul: 'soul-arch' },
  dev: { agent_id: 'dev', name: '码农鸡', soul: 'soul-dev' },
};

// groups-backend：群记录 + 消息落库内存化。
const groupRecord = {
  group_id: 'g1',
  agent_ids: ['pm', 'arch', 'dev'],
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
      // soul 形如 soul-<id> → 反查 agentId（测试观测用）。前缀匹配：2026-11-07
      // 起 system = soul + 过经验块（chatLessonsBlock），不能再整串精确相等。
      const agentId = Object.entries(agentRecords).find(
        ([, a]) => a.soul && args.system.startsWith(a.soul),
      )?.[0] ?? 'unknown';
      turnPrompts.push({ agentId, prompt: args.prompt });
      yield { type: 'text-delta', value: `${agentId} 收到` };
    },
  }),
}));

// gateway run：内存 gw。
vi.mock('../../lib/gateway-run', () => ({
  startGatewayRun: vi.fn(() => {
    let n = 0;
    return {
      turnId: 'turn-1',
      nextId: () => `gw-${++n}`,
      emitMessage: vi.fn(),
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

import { runFanoutAsync, isCasualChat } from '../groups-chat';

function postedContents(): string[] {
  return postedMessages.map((m) => m.content);
}

beforeEach(() => {
  postedMessages.length = 0;
  groupRecord.messages = [];
  turnPrompts.length = 0;
});

describe('runFanout — @ 提及定向派活（DAG 路径）', () => {
  it('@架构汪 → 只跑 arch，prompt 为清洗文本，跳过 LLM 拆解', async () => {
    await runFanoutAsync('g1', 'byok:zhipu', undefined, {}, '@架构汪 帮我评审这个架构设计');
    const ran = turnPrompts.map((t) => t.agentId).sort();
    expect(ran).toEqual(['arch']);
    // 清洗文本：@token 被剔除
    expect(turnPrompts[0].prompt).toBe('帮我评审这个架构设计');
    // 派发决策卡 reason 可见（intent 帧经 gw.emitMessage —— mock 掉了，改为验证
    // 系统消息：交接摘要 postMessage 属 DAG 收尾，跳过拆解无「拆解」系统消息）。
    expect(postedContents().some((c) => c.includes('arch 收到') || c.includes('架构汪'))).toBe(true);
  });

  it('@多人 @策划喵 @码农鸡 → 两人都跑，中间节点 arch 不跑', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@策划喵 @码农鸡 一起看下需求');
    const ran = turnPrompts.map((t) => t.agentId).sort();
    expect(ran).toEqual(['dev', 'pm']);
  });

  it('@ 未命中任何成员 → 全员照跑 + system 提示可点名成员（可解释性）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@路人甲 大家好');
    const ran = turnPrompts.map((t) => t.agentId).sort();
    expect(ran).toEqual(['arch', 'dev', 'pm']);
    // 未命中提示：列出可点名成员，帮助用户纠正（2026-11-05 迭代五）。
    const notice = postedContents().find((c) => c.startsWith('[mention]'));
    expect(notice).toBeTruthy();
    expect(notice).toContain('@策划喵');
    expect(notice).toContain('@架构汪');
    expect(notice).toContain('@码农鸡');
  });

  it('无 @ → 全员照跑，不发 [mention] 提示（零噪音）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '大家好，开始干活');
    expect(turnPrompts).toHaveLength(3);
    expect(postedContents().some((c) => c.startsWith('[mention]'))).toBe(false);
  });

  it('@ 命中 → 不发 [mention] 提示', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@架构汪 看下');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['arch']);
    expect(postedContents().some((c) => c.startsWith('[mention]'))).toBe(false);
  });

  it('CJK 无空格直连点名（@码农鸡修一下）→ 定向派发 + prompt 剔 @ 噪音（2026-11-07 边界收口）', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@码农鸡修一下登录');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['dev']);
    expect(turnPrompts[0].prompt).toBe('修一下登录');
    expect(postedContents().some((c) => c.startsWith('[mention]'))).toBe(false);
  });

  it('粘连双点名（@策划喵@码农鸡）→ 两人都跑，不发 [mention] 提示', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@策划喵@码农鸡 一起看下需求');
    const ran = turnPrompts.map((t) => t.agentId).sort();
    expect(ran).toEqual(['dev', 'pm']);
    expect(turnPrompts.map((t) => t.prompt)).toEqual(['一起看下需求', '一起看下需求']);
    expect(postedContents().some((c) => c.startsWith('[mention]'))).toBe(false);
  });
});

describe('runFanout — @ 提及定向派活（串行降级路径）', () => {
  beforeEach(() => {
    // team 读不到（members 空）→ loadTeamForRun.team.members.length===0 → 串行 fan-out。
    (teamShape as { members: string[] }).members = [];
  });

  it('@架构汪 → 只派发 arch，prompt 为清洗文本', async () => {
    (teamShape as { members: string[] }).members = ['pm', 'arch', 'dev'];
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@架构汪 这个类怎么改');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['arch']);
    expect(turnPrompts[0].prompt).toBe('这个类怎么改');
  });

  it('@ 未命中 → 全员串行（现状）', async () => {
    (teamShape as { members: string[] }).members = ['pm', 'arch', 'dev'];
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@不存在 嗨');
    expect(turnPrompts).toHaveLength(3);
  });
});

// ── 寒暄路由（2026-11-07 用户反馈：「hi 后所有 agent 都回复」不合理）────────

describe('isCasualChat — 寒暄判定（纯函数）', () => {
  it('寒暄/应答 → true', () => {
    for (const t of ['hi', 'Hello!', '你好', '你好呀', '早上好', '谢谢', '收到', '晚安~', '嗯']) {
      expect(isCasualChat(t)).toBe(true);
    }
  });

  it('任务/点名/多行/超长 → false（零回归）', () => {
    for (const t of [
      '大家好，开始干活',
      '帮我查一下学情',
      '@架构汪 在吗',
      'hi\n帮我做件事',
      '这是一条比较长的消息，超过寒暄的长度上限就应当走常规派发流程没问题吧',
    ]) {
      expect(isCasualChat(t)).toBe(false);
    }
  });
});

describe('runFanout — 寒暄路由：hi 不再全员广播', () => {
  beforeEach(() => {
    (teamShape as { members: string[] }).members = ['pm', 'arch', 'dev'];
    groupRecord.team_id = 't-default';
  });

  it('「hi」→ 只派成员首位单人接话，不全员广播', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, 'hi');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['pm']);
    expect(postedContents().some((c) => c.startsWith('[mention]'))).toBe(false);
  });

  it('寒暄不进 DAG：团队绑定（DAG 可用）时同样单人接话', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '你好！');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['pm']);
  });

  it('无团队串行群同样收缩：寒暄只回一条', async () => {
    (teamShape as { members: string[] }).members = [];
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '谢谢');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['pm']);
  });

  it('任务消息零回归：全员照跑', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '大家好，开始干活');
    expect(turnPrompts).toHaveLength(3);
  });

  it('寒暄 + @ 点名 → 点名优先，不收缩', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '@架构汪 在吗');
    expect(turnPrompts.map((t) => t.agentId)).toEqual(['arch']);
  });
});
