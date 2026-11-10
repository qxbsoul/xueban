/**
 * groups-chat-lessons-system.test.ts — 记忆闭环接线（2026-11-07）。
 *
 * 走查缺口：memory_lessons 写入侧（chat 回合失败/中断 → recordRunLesson，域
 * 'chat-gateway'）早已接通，但 groups-chat 两条派发路径的 system 只有 soul，
 * 沉淀的经验永远读不回来（recallTopK 在聊天面从未生效）。本文件锁接线：
 *   - chatLessonsBlock：召回域合并（agent 专属 + chat-gateway + 通用）、去重、
 *     跨域不串（promptBlock 语义本体由
 *     src/__tests__/memory-lessons-prompt-block.test.ts 锁）
 *   - 串行路径（DM / 无 team 群）：callable.turn 的 system = soul + 经验块
 *   - DAG 路径（team 群）：callAgent 的 system 同款，且各 agent 只带自己的域
 *
 * 真实 SQLite（临时 cwd 隔离）+ 其余重依赖 mock（groups-chat-mention 同款）。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-lessons-system.test.ts
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
  messages: [
    { content: '帮我复盘下昨天的投放', sender_kind: 'user', sender_name: 'boss', timestamp: '2026-11-07T00:00:00Z' },
  ] as Array<{ content: string; sender_kind: string; sender_name: string; timestamp: string }>,
  kind: 'dm' as string,
  team_id: null as string | null,
};
const postedMessages: Array<{ content: string; sender_name: string; sender_kind: string; agent_id?: string }> = [];
vi.mock('../../storage/groups-backend', () => ({
  getGroupsBackend: () => ({
    getGroup: vi.fn(async (id: string) => ({ ...groupRecord, group_id: id })),
    appendMessage: vi.fn(async (_id: string, body: { content: string; sender_name: string; sender_kind: string; agent_id?: string }) => {
      postedMessages.push(body);
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

// dispatcher：可观测 fake，捕获 system/prompt 原样。
const turnCalls: Array<{ system: string; prompt: string }> = [];
vi.mock('../../transport/dispatcher', () => ({
  resolveCallable: () => ({
    turn: async function* (args: { system: string; prompt: string }) {
      turnCalls.push({ system: args.system, prompt: args.prompt });
      yield { type: 'text-delta', value: 'ok' };
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

import { runFanoutAsync, chatLessonsBlock } from '../groups-chat';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let lessons: typeof import('../../storage/memory-lessons');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-chat-lessons-'));
  process.chdir(tmp);
  const sqlite = await import('../../storage/sqlite');
  sqlite._resetForTests();
  sqlite.getDb();
  lessons = await import('../../storage/memory-lessons');

  // 四条经验：通用 / pm 专属 / 聊天回合沉淀 / 无关域（绝不该进任何 agent 的块）
  lessons.remember({ content: '通用-交付必须附证据链', scope_key: '', kind: 'lesson', score: 0.9 });
  lessons.remember({ content: '专属-总经理汇报先说结论', scope_key: 'pm', kind: 'preference', score: 0.8 });
  lessons.remember({ content: '聊天教训-复盘别超范围', scope_key: 'chat-gateway', kind: 'lesson', score: 0.85 });
  lessons.remember({ content: '无关-写手域经验', scope_key: 'writer', kind: 'lesson', score: 0.99 });
});

afterAll(() => {
  process.chdir(origCwd);
  try { rmSync(tmp, { recursive: true, force: true }); } catch { /* best-effort */ }
});

beforeEach(() => {
  turnCalls.length = 0;
  postedMessages.length = 0;
  // 还原群形态（各用例按需改）
  groupRecord.kind = 'dm';
  groupRecord.team_id = null;
  groupRecord.agent_ids = ['pm'];
});

describe('chatLessonsBlock — 召回域合并', () => {
  it('agent 专属 + 聊天域 + 通用都在，无关域不进，通用去重', () => {
    const block = chatLessonsBlock('pm');
    expect(block).toContain('## 过往经验（自动沉淀，供参考）');
    expect(block).toContain('通用-交付必须附证据链');
    expect(block).toContain('专属-总经理汇报先说结论');
    expect(block).toContain('聊天教训-复盘别超范围');
    expect(block).not.toContain('无关-写手域经验');
    expect(block.split('通用-交付必须附证据链').length - 1).toBe(1);
  });

  it('无专属沉淀的 agent → 通用 + 聊天域（专属不串给别的 agent）', () => {
    const block = chatLessonsBlock('arch');
    expect(block).toContain('通用-交付必须附证据链');
    expect(block).toContain('聊天教训-复盘别超范围');
    expect(block).not.toContain('专属-总经理汇报先说结论');
  });
});

describe('串行路径（DM）— system = soul + 经验块', () => {
  it('DM 单 agent：turn 的 system 含 soul 与三类经验', async () => {
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '帮我复盘下昨天的投放');
    expect(turnCalls).toHaveLength(1);
    const sys = turnCalls[0].system;
    expect(sys.startsWith('soul-pm')).toBe(true);
    expect(sys).toContain('## 过往经验（自动沉淀，供参考）');
    expect(sys).toContain('通用-交付必须附证据链');
    expect(sys).toContain('专属-总经理汇报先说结论');
    expect(sys).toContain('聊天教训-复盘别超范围');
    expect(sys).not.toContain('无关-写手域经验');
    // prompt 仍是用户原文（经验挂 system，不污染用户消息）
    expect(turnCalls[0].prompt).toBe('帮我复盘下昨天的投放');
  });
});

describe('DAG 路径（team 群）— callAgent 同款注入', () => {
  it('策略收缩后的单 agent：system = 选中 agent 的 soul + 自己域经验块', async () => {
    groupRecord.kind = 'group';
    groupRecord.team_id = 't1';
    groupRecord.agent_ids = ['pm', 'arch'];
    await runFanoutAsync('g1', 'cli:claude', undefined, {}, '一起复盘昨天的投放');
    expect(turnCalls).toHaveLength(1);
    const pmSys = turnCalls[0]!.system;
    expect(pmSys.startsWith('soul-pm')).toBe(true);
    expect(pmSys).toContain('专属-总经理汇报先说结论');
    expect(pmSys).toContain('通用-交付必须附证据链');
    expect(pmSys).toContain('聊天教训-复盘别超范围');
    expect(pmSys).not.toContain('无关-写手域经验');
  });
});
