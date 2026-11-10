/**
 * orchestrator-parse.test.ts — INTENT_PARSE 拆解器纯函数 + LLM 调用路径。
 * compose/parse 全纯函数直测；decomposeWithLlm 用注入 callImpl mock
 * （无需真实 key / 网络）。
 */
import { describe, expect, it } from 'vitest';
import type { ProviderChunk } from '../../transport/api-clients';
import {
  composeParsePrompt,
  decomposeWithLlm,
  parseOrchestratorJson,
  parseOrchestratorPlan,
  planWithLlm,
  type OrchestratorAgent,
} from '../orchestrator-parse';

const AGENTS: OrchestratorAgent[] = [
  { id: 'writer', name: 'Writer', soul: '前端文案' },
  { id: 'reviewer', name: 'Reviewer', soul: '审校' },
  { id: 'ops', name: 'Ops', soul: '发布' },
];

describe('composeParsePrompt', () => {
  it('embeds the roster with ids/names/souls and the user text', () => {
    const p = composeParsePrompt('做个落地页', AGENTS);
    expect(p).toContain('id: writer');
    expect(p).toContain('name: Writer');
    expect(p).toContain('做个落地页');
    expect(p).toContain('JSON');
  });
});

describe('parseOrchestratorJson', () => {
  it('parses a clean JSON array', () => {
    const raw = JSON.stringify([
      { agentId: 'writer', title: '写文案', prompt: '写落地页主文案' },
      { agentId: 'reviewer', title: '审校', prompt: '审以上文案', depends_on: ['writer'] },
    ]);
    const tasks = parseOrchestratorJson(raw, AGENTS)!;
    expect(tasks).toHaveLength(2);
    expect(tasks[0]).toMatchObject({ agentId: 'writer', title: '写文案' });
    expect(tasks[1]!.depends_on).toEqual(['writer']);
  });

  it('tolerates ```json fences and surrounding prose', () => {
    const raw = '好的，拆解如下：\n```json\n[{"agentId":"ops","title":"发布","prompt":"发布到生产"}]\n```\n以上。';
    const tasks = parseOrchestratorJson(raw, AGENTS)!;
    expect(tasks).toHaveLength(1);
    expect(tasks[0]!.agentId).toBe('ops');
  });

  it('drops unknown agents and duplicate assignments (first wins)', () => {
    const raw = JSON.stringify([
      { agentId: 'ghost', title: 'x', prompt: 'y' },
      { agentId: 'writer', title: '第一', prompt: 'p1' },
      { agentId: 'writer', title: '第二', prompt: 'p2' },
    ]);
    const tasks = parseOrchestratorJson(raw, AGENTS)!;
    expect(tasks).toHaveLength(1);
    expect(tasks[0]!.title).toBe('第一');
  });

  it('falls back to prompt head when title missing; clamps long titles', () => {
    const raw = JSON.stringify([{ agentId: 'writer', prompt: '这是一段非常长的指令内容'.repeat(3) }]);
    const tasks = parseOrchestratorJson(raw, AGENTS)!;
    expect(tasks[0]!.title.length).toBeLessThanOrEqual(40);
  });

  it('returns null for garbage / empty / non-array', () => {
    expect(parseOrchestratorJson('', AGENTS)).toBeNull();
    expect(parseOrchestratorJson('not json at all', AGENTS)).toBeNull();
    expect(parseOrchestratorJson('{"a":1}', AGENTS)).toBeNull();
    expect(parseOrchestratorJson('[]', AGENTS)).toBeNull();
  });
});

describe('decomposeWithLlm', () => {
  function mockCall(chunks: ProviderChunk[]) {
    return async function* (): AsyncGenerator<ProviderChunk> {
      for (const c of chunks) yield c;
    } as unknown as typeof import('../../transport/api-clients').callProvider;
  }

  it('returns parsed tasks from streamed text-deltas', async () => {
    const json = JSON.stringify([{ agentId: 'writer', title: '写', prompt: '写主文案' }]);
    const tasks = await decomposeWithLlm({
      userText: '做个落地页',
      agents: AGENTS,
      provider: 'anthropic',
      apiKey: 'sk-test',
      callImpl: mockCall([
        { type: 'text-delta', text: '```json\n' },
        { type: 'text-delta', text: json },
        { type: 'text-delta', text: '\n```' },
        { type: 'end' },
      ]),
    });
    expect(tasks).toHaveLength(1);
    expect(tasks![0]!.agentId).toBe('writer');
  });

  it('returns null without an api key (graceful degradation)', async () => {
    expect(
      await decomposeWithLlm({ userText: 'x', agents: AGENTS, provider: 'anthropic' }),
    ).toBeNull();
  });

  it('returns null on provider error chunk', async () => {
    expect(
      await decomposeWithLlm({
        userText: 'x',
        agents: AGENTS,
        provider: 'anthropic',
        apiKey: 'sk-bad',
        callImpl: mockCall([{ type: 'error', message: '401' }]),
      }),
    ).toBeNull();
  });

  it('returns null when the LLM output is unparseable', async () => {
    expect(
      await decomposeWithLlm({
        userText: 'x',
        agents: AGENTS,
        provider: 'anthropic',
        apiKey: 'sk-test',
        callImpl: mockCall([
          { type: 'text-delta', text: '抱歉我无法' },
          { type: 'end' },
        ]),
      }),
    ).toBeNull();
  });
});

// ── {intent, strategy, tasks} 计划对象（2026-11-07 策略矩阵升级）─────────────

describe('parseOrchestratorPlan — 计划对象解析', () => {
  it('解析 {intent,strategy,tasks} 对象并保留元数据', () => {
    const raw = JSON.stringify({
      intent: 'question',
      strategy: 'expert',
      tasks: [{ agentId: 'writer', title: '应答', prompt: '解释这段代码' }],
    });
    const plan = parseOrchestratorPlan(raw, AGENTS)!;
    expect(plan).toMatchObject({ intent: 'question', strategy: 'expert', source: 'llm' });
    expect(plan.tasks).toHaveLength(1);
  });

  it('对象 span 优先于内嵌数组（否则 tasks 数组会截走、丢掉 intent/strategy）', () => {
    const raw =
      '好的，计划如下：\n```json\n' +
      JSON.stringify({
        intent: 'discussion',
        strategy: 'roundtable',
        tasks: [
          { agentId: 'writer', prompt: '观点1' },
          { agentId: 'reviewer', prompt: '观点2' },
        ],
      }) +
      '\n```\n完成。';
    const plan = parseOrchestratorPlan(raw, AGENTS)!;
    expect(plan.intent).toBe('discussion');
    expect(plan.strategy).toBe('roundtable');
    expect(plan.tasks).toHaveLength(2);
  });

  it('旧数组格式 → 合成计划：单任务 expert / 多任务 parallel', () => {
    const one = parseOrchestratorPlan('[{"agentId":"ops","prompt":"发布"}]', AGENTS)!;
    expect(one).toMatchObject({ intent: 'task', strategy: 'expert', source: 'llm' });

    const two = parseOrchestratorPlan(
      '[{"agentId":"writer","prompt":"p1"},{"agentId":"reviewer","prompt":"p2"}]',
      AGENTS,
    )!;
    expect(two).toMatchObject({ intent: 'task', strategy: 'parallel' });
    expect(two.tasks).toHaveLength(2);
  });

  it('intent/strategy 不在枚举内 → 收敛到安全缺省（task / parallel|expert）', () => {
    const raw = JSON.stringify({
      intent: 'bogus',
      strategy: 'zzz',
      tasks: [{ agentId: 'writer', prompt: 'p' }],
    });
    expect(parseOrchestratorPlan(raw, AGENTS)).toMatchObject({
      intent: 'task',
      strategy: 'expert',
    });

    const raw2 = JSON.stringify({
      intent: 'bogus',
      strategy: 'zzz',
      tasks: [
        { agentId: 'writer', prompt: 'p1' },
        { agentId: 'reviewer', prompt: 'p2' },
      ],
    });
    expect(parseOrchestratorPlan(raw2, AGENTS)).toMatchObject({
      intent: 'task',
      strategy: 'parallel',
    });
  });

  it('垃圾输入 / 空工单 → null', () => {
    expect(parseOrchestratorPlan('', AGENTS)).toBeNull();
    expect(parseOrchestratorPlan('随便说点什么', AGENTS)).toBeNull();
    expect(parseOrchestratorPlan('{"intent":"task","strategy":"expert","tasks":[]}', AGENTS)).toBeNull();
    expect(parseOrchestratorPlan('{"intent":"task","tasks":[{"agentId":"ghost","prompt":"p"}]}', AGENTS)).toBeNull();
  });
});

describe('planWithLlm — 计划对象输出', () => {
  function mockCall(chunks: ProviderChunk[]) {
    return async function* (): AsyncGenerator<ProviderChunk> {
      for (const c of chunks) yield c;
    } as unknown as typeof import('../../transport/api-clients').callProvider;
  }

  it('流式输出计划对象 → 完整 ChatPlan（意图+策略+工单）', async () => {
    const json = JSON.stringify({
      intent: 'discussion',
      strategy: 'roundtable',
      tasks: [
        { agentId: 'writer', title: '观点', prompt: '给观点' },
        { agentId: 'reviewer', title: '补充', prompt: '补观点' },
      ],
    });
    const plan = await planWithLlm({
      userText: '帮我选 A 还是 B',
      agents: AGENTS,
      provider: 'anthropic',
      apiKey: 'sk-test',
      callImpl: mockCall([
        { type: 'text-delta', text: '```json\n' },
        { type: 'text-delta', text: json },
        { type: 'text-delta', text: '\n```' },
        { type: 'end' },
      ]),
    });
    expect(plan).toMatchObject({
      intent: 'discussion',
      strategy: 'roundtable',
      source: 'llm',
    });
    expect(plan!.tasks.map((t) => t.agentId)).toEqual(['writer', 'reviewer']);
  });

  it('无 apiKey / 输出不可解析 → null（调用方降级启发式）', async () => {
    expect(
      await planWithLlm({ userText: 'x', agents: AGENTS, provider: 'anthropic' }),
    ).toBeNull();
    expect(
      await planWithLlm({
        userText: 'x',
        agents: AGENTS,
        provider: 'anthropic',
        apiKey: 'sk-test',
        callImpl: mockCall([{ type: 'text-delta', text: '没想好' }, { type: 'end' }]),
      }),
    ).toBeNull();
  });

  it('跨 chunk 增量上抛 reasoning，最终计划不含 sf 标签', async () => {
    const json = JSON.stringify({
      intent: 'task', strategy: 'expert',
      tasks: [{ agentId: 'writer', title: '写文案', prompt: '写主文案' }],
    });
    const deltas: Array<{ delta: string; thinking: string }> = [];
    const plan = await planWithLlm({
      userText: '写文案', agents: AGENTS, provider: 'anthropic', apiKey: 'sk-test',
      callImpl: mockCall([
        { type: 'text-delta', text: '<sf:think' },
        { type: 'text-delta', text: 'ing>先识别' },
        { type: 'text-delta', text: '意图，再' },
        { type: 'text-delta', text: '分工</sf:think' },
        { type: 'text-delta', text: `ing>${json}` },
        { type: 'end' },
      ]),
      onThinkingDelta: (delta, thinking) => deltas.push({ delta, thinking }),
    });
    expect(deltas).toEqual([
      { delta: '先识别', thinking: '先识别' },
      { delta: '意图，再', thinking: '先识别意图，再' },
      { delta: '分工', thinking: '先识别意图，再分工' },
    ]);
    expect(plan).toMatchObject({ thinking: '先识别意图，再分工', source: 'llm' });
    expect(plan?.tasks[0]).toMatchObject({ agentId: 'writer', prompt: '写主文案' });
    expect(JSON.stringify(plan)).not.toContain('sf:thinking');
  });
});
