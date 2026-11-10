/**
 * groups-chat-stream-strip.test.ts — B3 过程卡流式标签剥离接线测试（2026-11-07）。
 *
 * 背景：B3 过程卡把 text-delta 逐片点亮进卡体，模型（zhipu reasoning 包装 /
 * 模型模仿 sf: 协议）会在正文流里写 `<sf:thinking>`/`<sf-thinking>` 标签，
 * 原样 append 会让流式卡体「原始标签与正文混排」。groups-chat 现用
 * createThinkingTagStream 在 emitPatch 前逐 chunk 剥定界符（思考内容保留，
 * 与结构化 thinking-delta 通道对齐）。
 *
 * 用 vi.mock 驱动 runFanoutAsync（同 groups-chat-mention.test.ts 模式），
 * fake callable 把标签**切断在 chunk 边界**吐出，验证：
 *   - 串行路径：thinking_append_body 各片拼接无 `<sf` 残留、思考内容保留；
 *   - DAG 路径（@ 命中单节点）：同上；
 *   - 最终聊天回复仍走 stripThinking 整段剥除（气泡只留正文）。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-stream-strip.test.ts
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── mock 重依赖（模块级，import 路由前生效）────────────────────────────────

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

const agentRecords: Record<string, { agent_id: string; name: string; soul: string }> = {
  pm: { agent_id: 'pm', name: '策划喵', soul: 'soul-pm' },
  arch: { agent_id: 'arch', name: '架构汪', soul: 'soul-arch' },
  dev: { agent_id: 'dev', name: '码农鸡', soul: 'soul-dev' },
};

const groupRecord = {
  group_id: 'g1',
  agent_ids: ['pm'] as string[],
  messages: [] as Array<{ content: string; sender_kind: string; sender_name: string }>,
  kind: 'group',
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

// dispatcher：fake callable 按 streamScript 吐片（标签故意切断在 chunk 边界）。
type StreamChunk = { type: 'text-delta'; value: string };
let streamScript: StreamChunk[] = [];
vi.mock('../../transport/dispatcher', () => ({
  resolveCallable: () => ({
    turn: async function* () {
      for (const c of streamScript) yield c;
    },
  }),
}));

// gateway run：内存 gw，逐帧记录供断言。
interface RecordedPatch { id: string; op: string; chunk?: string }
interface RecordedMsg { id: string; kind: string; label?: string; status?: string }
const gwPatches: RecordedPatch[] = [];
const gwMessages: RecordedMsg[] = [];
vi.mock('../../lib/gateway-run', () => ({
  startGatewayRun: vi.fn(() => {
    let n = 0;
    return {
      turnId: 'turn-1',
      nextId: () => `gw-${++n}`,
      emitMessage: vi.fn((m: RecordedMsg) => {
        gwMessages.push(m);
      }),
      emitPatch: vi.fn((p: RecordedPatch) => {
        gwPatches.push(p);
      }),
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

/** 标签切断在 chunk 边界的流式脚本：开标签 / 闭标签 / 连字符形态都被切开。 */
const NASTY_STREAM: StreamChunk[] = [
  { type: 'text-delta', value: '<sf:think' },
  { type: 'text-delta', value: 'ing>让我想想</sf:thinking>你好' },
  { type: 'text-delta', value: '，世界<sf-thinking>边想边写</sf-thin' },
  { type: 'text-delta', value: 'king>。完毕' },
];

/** 定界符剥掉后应留在卡体的内容（思考内容保留）——与 chunk 切法无关。 */
const EXPECTED_CARD_BODY = '让我想想你好，世界边想边写。完毕';
/** stripThinking 整段剥除后应落库的聊天回复（气泡只留正文）。 */
const EXPECTED_REPLY = '你好，世界。完毕';

/** 取本轮 thinking 卡的所有 append 片拼接。 */
function cardBody(): string {
  const card = gwMessages.find((m) => m.kind === 'thinking');
  expect(card).toBeTruthy();
  return gwPatches
    .filter((p) => p.op === 'thinking_append_body' && p.id === card!.id)
    .map((p) => p.chunk ?? '')
    .join('');
}

function agentReply(): string {
  const m = postedMessages.find((x) => x.sender_kind === 'agent');
  expect(m).toBeTruthy();
  return m!.content;
}

beforeEach(() => {
  postedMessages.length = 0;
  groupRecord.messages = [];
  groupRecord.agent_ids = ['pm'];
  groupRecord.team_id = 't-default';
  teamShape.members = ['pm', 'arch', 'dev'];
  gwPatches.length = 0;
  gwMessages.length = 0;
  streamScript = NASTY_STREAM.map((c) => ({ ...c }));
});

describe('B3 过程卡流式标签剥离 — 串行路径', () => {
  it('跨 chunk 切断的定界符不闪现、思考内容保留、回复仍整段剥除', async () => {
    (teamShape as { members: string[] }).members = []; // team 读不到 → 串行
    await runFanoutAsync('g1', 'byok:zhipu', undefined, {}, '干活');
    // 卡体：逐片均无 `<sf` 残片（含半截标签），拼接 = 剥定界符后的内容。
    const card = gwMessages.find((m) => m.kind === 'thinking');
    const appends = gwPatches.filter((p) => p.op === 'thinking_append_body' && p.id === card!.id);
    for (const p of appends) expect(p.chunk ?? '').not.toContain('<sf');
    expect(cardBody()).toBe(EXPECTED_CARD_BODY);
    // 聊天回复：stripThinking 整段剥除（含思考内容），只留正文。
    expect(agentReply()).toBe(EXPECTED_REPLY);
  });
});

describe('B3 过程卡流式标签剥离 — DAG 路径', () => {
  it('@ 命中单节点：卡体剥定界符、回复整段剥除（与串行一致）', async () => {
    await runFanoutAsync('g1', 'byok:zhipu', undefined, {}, '@策划喵 干活');
    const card = gwMessages.find((m) => m.kind === 'thinking');
    const appends = gwPatches.filter((p) => p.op === 'thinking_append_body' && p.id === card!.id);
    for (const p of appends) expect(p.chunk ?? '').not.toContain('<sf');
    expect(cardBody()).toBe(EXPECTED_CARD_BODY);
    expect(agentReply()).toBe(EXPECTED_REPLY);
  });
});
