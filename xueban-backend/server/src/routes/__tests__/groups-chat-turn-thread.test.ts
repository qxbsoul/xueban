/**
 * groups-chat-turn-thread.test.ts — whole-thread 回放保真接线契约（2026-11-07）。
 *
 * 「thread 丢段即测试红」在派发路径上的落点：callable.turn 收到的
 * (history, prompt) 必须覆盖持久化线程每个文本段恰好一次。
 *
 * 回归背景：旧 `history.slice(0, -1)` 盲删末位——串行 fan-out 第 2 个 agent 的
 * 线程末位是第 1 个 agent 的回复，被整条删掉（后发言者看不到先发言者，与
 * 「Reload each round so later agents see earlier replies」注释相反），用户消息
 * 反而留在 history 里与 prompt 重复。纯函数契约见 lib/__tests__/turn-thread.test.ts。
 *
 * 真实 SQLite（临时 cwd 隔离）+ 其余重依赖 mock（groups-chat-lessons-system 同款）。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-turn-thread.test.ts
 */
import { describe, it, expect, vi, beforeAll, afterAll, beforeEach } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

// ── mock 重依赖（模块级，import 路由前生效）────────────────────────────────

const teamShape = {
  name: '双人组',
  members: ['pm', 'arch'] as string[],
  edges: [{ from: 'pm', to: 'arch', kind: 'sequential' as const }],
  policy_matrix: {},
};
vi.mock('../../lib/team-source', () => ({
  loadTeamForRun: vi.fn(() => ({ team: teamShape, errors: [] })),
}));

const agentRecords: Record<string, { agent_id: string; name: string; soul: string }> = {
  pm: { agent_id: 'pm', name: '总经理', soul: 'soul-pm' },
  arch: { agent_id: 'arch', name: '架构汪', soul: 'soul-arch' },
};

const groupRecord = {
  group_id: 'g1',
  agent_ids: ['pm'] as string[],
  messages: [] as Array<{ content: string; sender_kind: string; sender_name: string; timestamp?: string }>,
  kind: 'dm' as string,
  team_id: null as string | null,
};
const postedMessages: Array<{ content: string; sender_name: string; sender_kind: string; agent_id?: string }> = [];
vi.mock('../../storage/groups-backend', () => ({
  getGroupsBackend: () => ({
    // 共享 messages 数组引用：appendMessage 的落库即后续 reload 可见（每轮
    // reload「so later agents see earlier replies」的语义前提）。
    getGroup: vi.fn(async (id: string) => ({ ...groupRecord, group_id: id })),
    appendMessage: vi.fn(async (_id: string, body: { content: string; sender_name: string; sender_kind: string; agent_id?: string }) => {
      postedMessages.push(body);
      groupRecord.messages.push({
        content: body.content,
        sender_kind: body.sender_kind,
        sender_name: body.sender_name,
      });
      return { ...body, message_id: `m-${postedMessages.length}` };
    }),
  }),
}));

// agent 读取走全局 fetch（pyGet 非群路径）。
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

// dispatcher：可观测 fake，捕获 system/prompt/history 原样；回复带调用序水印
// （第 2 个 agent 的 history 里应能看到第 1 个 agent 的回复水印）。
const turnCalls: Array<{ system: string; prompt: string; history: unknown[]; replyText: string }> = [];
vi.mock('../../transport/dispatcher', () => ({
  resolveCallable: () => ({
    turn: async function* (args: { system: string; prompt: string; history: unknown[] }) {
      const replyText = `回复水印-${turnCalls.length + 1}`;
      turnCalls.push({ system: args.system, prompt: args.prompt, history: args.history, replyText });
      yield { type: 'text-delta', value: replyText };
    },
  }),
}));

// gateway run：内存 gw。
vi.mock('../../lib/gateway-run', () => ({
  startGatewayRun: vi.fn(() => {
    let n = 0;
    return {
      sessionId: 'gw-s1',
      conversationId: 'c1',
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

import { runFanoutAsync } from '../groups-chat';
import type { ConversationMessage } from '../../lib/conversation-types';

let tmp: string;
let origCwd: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-chat-turn-thread-'));
  process.chdir(tmp);
  const sqlite = await import('../../storage/sqlite');
  sqlite._resetForTests();
  sqlite.getDb();
});

afterAll(() => {
  process.chdir(origCwd);
  try { rmSync(tmp, { recursive: true, force: true }); } catch { /* best-effort */ }
});

beforeEach(() => {
  turnCalls.length = 0;
  postedMessages.length = 0;
  groupRecord.messages.length = 0;
  groupRecord.kind = 'dm';
  groupRecord.team_id = null;
  groupRecord.agent_ids = ['pm'];
});

/** turn 的 history 文本段清单（按序）。 */
function historyTexts(history: unknown[]): string[] {
  const out: string[] = [];
  for (const m of history as ConversationMessage[]) {
    for (const b of m.blocks) {
      if (b.kind === 'text' && b.text) out.push(b.text);
    }
  }
  return out;
}

describe('串行 fan-out（DM / 无 team 群）— whole-thread 回放保真', () => {
  it('策略收缩为单 agent：用户原文在 history + prompt 中恰好一次', async () => {
    groupRecord.kind = 'group';
    groupRecord.agent_ids = ['pm', 'arch'];
    groupRecord.messages.push(
      { content: '帮我复盘下昨天的投放', sender_kind: 'user', sender_name: 'boss' },
    );
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '帮我复盘下昨天的投放');

    expect(turnCalls).toHaveLength(1);
    const [only] = turnCalls;
    expect(only!.prompt).toBe('帮我复盘下昨天的投放');
    expect(historyTexts(only!.history)).not.toContain('帮我复盘下昨天的投放');
    const segments = [...historyTexts(only!.history), only!.prompt];
    expect(segments.filter((s) => s === '帮我复盘下昨天的投放')).toHaveLength(1);
  });

  it('双连发（用户连发两条、中间无回复）：prompt 并入前段，无相邻 user 重复', async () => {
    groupRecord.messages.push(
      { content: '先看下天气', sender_kind: 'user', sender_name: 'boss' },
      { content: '顺便看看新闻', sender_kind: 'user', sender_name: 'boss' },
    );
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '顺便看看新闻');

    expect(turnCalls).toHaveLength(1);
    // 两段都进 prompt（较早在前）；history 为空——不丢不重复，接口缝严格交替。
    expect(turnCalls[0]!.prompt).toBe('先看下天气\n\n顺便看看新闻');
    expect(turnCalls[0]!.history).toHaveLength(0);
  });
});

describe('DAG 路径（team 群）— 快照线程不盲删末段', () => {
  it('末位是用户消息：提升不重复；前序回复保留（丢段即红）', async () => {
    groupRecord.kind = 'group';
    groupRecord.team_id = 't1';
    groupRecord.agent_ids = ['pm', 'arch'];
    groupRecord.messages.push(
      { content: '第一轮问题', sender_kind: 'user', sender_name: 'boss' },
      { content: '上一轮的作答', sender_kind: 'agent', sender_name: '总经理' },
      { content: '第二轮问题', sender_kind: 'user', sender_name: 'boss' },
    );
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '第二轮问题');

    expect(turnCalls).toHaveLength(1);
    const call = turnCalls[0]!;
    const segments = [...historyTexts(call.history), ...call.prompt.split('\n\n')].filter(Boolean);
    expect(historyTexts(call.history)).toContain('上一轮的作答');
    expect(historyTexts(call.history)).not.toContain('第二轮问题');
    expect(historyTexts(call.history)).toContain('第一轮问题');
    expect(segments.filter((s) => s === '第二轮问题')).toHaveLength(1);
    expect(call.prompt).toContain('第二轮问题');
  });
});
