import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ProviderChunk, ProviderInput } from '../transport/api-clients';
import { _resetForTests, getDb } from '../storage/sqlite';
import {
  createXuebanConversation,
  deleteXuebanConversation,
  getXuebanConversation,
  listXuebanMessages,
} from '../storage/xueban-chat';
import { runXuebanChatTurn } from '../services/xueban-chat';
import { companyOwnsKnowledgePack, linkKnowledgePack } from '../storage/xueban-knowledge';
import { acceptCandidate, createCandidate } from '../storage/student-memory';

const originalCwd = process.cwd();
let dir = '';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-chat-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/api/xueban/auth/me')) {
      return Response.json({ company: { id: 'co1', name: '测试公司' } });
    }
    return Response.json({ items: [
      { id: 'e1', name: '文案', title: '内容运营', system_prompt: '负责文案', status: 'idle' },
      { id: 'e2', name: '分析', title: '数据分析师', system_prompt: '负责数据', status: 'idle' },
      { id: 'e3', name: '视觉', title: '视觉设计师', system_prompt: '负责视觉', status: 'idle' },
    ] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

const config = { provider: 'openai' as const, model: 'gpt-test', apiKey: 'test-key' };

describe('公司真实聊天', () => {
  it('单聊任务也调集相关员工、汇总成果并在下一轮携带上下文', async () => {
    const prompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'pipeline', tasks: [
            { agentId: 'e1', title: '撰写文案', prompt: '先写推广文案' },
            { agentId: 'e2', title: '数据复盘', prompt: '基于文案做数据复盘', depends_on: ['e1'] },
          ],
        }) };
        return;
      }
      prompts.push(input.userMessage);
      if (input.systemPrompt.includes('汇总教研员')) {
        yield { type: 'text-delta' as const, text: '总经理最终汇总' };
      } else {
        yield { type: 'text-delta' as const, text: input.userMessage.includes('数据复盘') ? '员工分析成果' : '员工文案成果' };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    const first = await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '先写推广文案，再结合数据分析复盘', config, authorization: 'Bearer test', callImpl });
    expect(first.plan?.assignments.map(item => item.agent_id)).toEqual(['e1', 'e2']);
    expect(first.messages.filter(message => message.sender_kind === 'agent').map(message => message.sender_name)).toEqual(['文案', '分析']);
    expect(first.messages[0].channel).toBe('workflow');
    expect(new Set(first.messages.map(message => message.run_id)).size).toBe(1);
    expect(first.messages[0].metadata_json).toContain('撰写文案');
    expect(first.messages.filter(message => message.sender_kind === 'agent').every(message => message.channel === 'workflow' && message.status === 'done' && message.task_id && message.task_title)).toBe(true);
    expect(first.messages.at(-1)?.content).toBe('总经理最终汇总');
    expect(first.messages.at(-1)?.channel).toBe('chat');
    expect(prompts[1]).toContain('员工文案成果');

    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '继续完善', config, authorization: 'Bearer test', callImpl });
    expect(prompts[3]).toContain('先写推广文案，再结合数据分析复盘');
    expect(prompts[3]).toContain('总经理最终汇总');
    expect(listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'agent')).toHaveLength(4);
  });

  it('总经理只选择相关员工，并按顺序交接真实产出', async () => {
    const prompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'pipeline', tasks: [
            { agentId: 'e1', title: '撰写文案', prompt: '先写获客文案' },
            { agentId: 'e2', title: '分析文案', prompt: '基于文案给出数据复盘', depends_on: ['e1'] },
          ],
        }) };
        return;
      }
      prompts.push(input.userMessage);
      yield { type: 'text-delta' as const, text: prompts.length === 1 ? '文案实际成果-unique' : '分析实际成果' };
    };
    const conversation = createXuebanConversation('co1', 'group');
    const result = await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '写文案并分析', config, authorization: 'Bearer test', callImpl });
    expect(result.plan?.assignments.map((item) => item.agent_id)).toEqual(['e1', 'e2']);
    expect(result.messages.filter((message) => message.sender_kind === 'agent')).toHaveLength(2);
    expect(prompts[1]).toContain('文案实际成果-unique');
    expect(listXuebanMessages(conversation.id).some((message) => message.sender_name === '视觉')).toBe(false);
  });

  it('公司知识包隔离，检索原文进入员工与总经理上下文', async () => {
    linkKnowledgePack('co1', 'pack-1');
    expect(companyOwnsKnowledgePack('co1', 'pack-1')).toBe(true);
    expect(companyOwnsKnowledgePack('co2', 'pack-1')).toBe(false);
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/api/memory-bank/packs/pack-1/query')) {
        return Response.json({ data: { hits: [{ text: '北极星活动暗号是蓝山-7429', source: '活动规则.txt', score: 1 }] } });
      }
      return Response.json({ items: [{ id: 'e1', name: '文案', title: '内容运营', status: 'idle' }] });
    }));
    const prompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      prompts.push(input.userMessage);
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'single', tasks: [{ agentId: 'e1', title: '回答暗号', prompt: '根据资料回答' }] }) };
      } else {
        yield { type: 'text-delta' as const, text: '暗号是蓝山-7429' };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    const events: Array<{ type: string; activity?: { name: string; status: string } }> = [];
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '活动暗号是什么', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
      embeddingHeaders: { 'X-Embedding-Key': 'request-only-key', 'X-Embedding-Model': 'test-embedding' },
    });
    expect(prompts.length).toBeGreaterThanOrEqual(3);
    expect(prompts.every(prompt => prompt.includes('蓝山-7429'))).toBe(true);
    expect(prompts.every(prompt => prompt.includes('活动规则.txt'))).toBe(true);
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/memory-bank/packs/pack-1/query'), expect.objectContaining({
      headers: expect.objectContaining({ 'X-Embedding-Key': 'request-only-key', 'X-Embedding-Model': 'test-embedding' }),
    }));
    expect(events.filter(event => event.type === 'activity' && event.activity?.name === 'knowledge_search').map(event => event.activity?.status)).toEqual(['running', 'done']);
    expect(events.some(event => event.type === 'activity' && event.activity?.name === 'manager_planning')).toBe(true);
    // 分工派发不再是独立数据流：学生只看任务与结果。
    expect(events.some(event => event.type === 'activity' && event.activity?.name === 'employee_dispatch')).toBe(false);
    const storedActivities = listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'system' && message.metadata_json?.includes('"activity"'));
    expect(storedActivities.length).toBeGreaterThanOrEqual(2);
    expect(storedActivities.every(message => message.channel === 'workflow' && message.metadata_json?.includes('"activity"'))).toBe(true);
  });

  it('所有教研员都失败时仍会给出面向学生的收尾答复', async () => {
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'single', tasks: [{ agentId: 'e1', title: '写学习计划', prompt: '写学习计划' }] }) };
        return;
      }
      yield { type: 'error', code: 'PROVIDER_ERROR', message: 'upstream down' } satisfies ProviderChunk;
    };
    const conversation = createXuebanConversation('co1', 'single');
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '给我一份学习计划',
      config, authorization: 'Bearer test', callImpl,
    });
    const last = result.messages.at(-1)!;
    expect(last.sender_kind).toBe('ceo');
    expect(last.status).toBe('done');
    expect(last.content).toContain('这轮');
    expect(last.content).toContain('重试');
  });

  it('新会话按账号召回长期记忆并注入规划上下文', async () => {
    const candidate = createCandidate({
      owner_id: 'acc-recall', category: 'preference', memory_key: 'teaching.explanation_style',
      title: '讲题偏好', content: '学生希望先讲思路再给答案。', confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: 'm-1', quote: '先讲思路' }],
    });
    acceptCandidate('acc-recall', candidate.id, { auto: true });

    const prompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      prompts.push(`${input.systemPrompt}\n${input.userMessage}`);
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'single', tasks: [{ agentId: 'e1', title: '讲解', prompt: '讲解定义域' }] }) };
        return;
      }
      yield { type: 'text-delta' as const, text: '好的，我们先讲思路。' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', ownerId: 'acc-recall',
      content: '数学函数定义域怎么学', config, authorization: 'Bearer test', callImpl,
    });
    expect(prompts[0]).toContain('学生长期学习记忆');
    expect(prompts[0]).toContain('先讲思路再给答案');

    // 另一个账号不应该看到这条记忆
    const conversationB = createXuebanConversation('co1', 'single');
    prompts.length = 0;
    await runXuebanChatTurn({
      conversationId: conversationB.id, companyId: 'co1', ownerId: 'acc-other',
      content: '数学函数定义域怎么学', config, authorization: 'Bearer test', callImpl,
    });
    expect(prompts[0]).not.toContain('先讲思路再给答案');
  });

  it('规划期间持续发送累计 thinking，且在 done 前完成跨 chunk 更新', async () => {
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        const json = JSON.stringify({
          intent: 'task', strategy: 'expert',
          tasks: [{ agentId: 'e1', title: '撰写文案', prompt: '写推广文案' }],
        });
        yield { type: 'text-delta' as const, text: '<sf:think' };
        yield { type: 'text-delta' as const, text: 'ing>先分析' };
        yield { type: 'text-delta' as const, text: '目标，再选择' };
        yield { type: 'text-delta' as const, text: `文案</sf:thinking>${json}` };
        return;
      }
      yield { type: 'text-delta' as const, text: '实际成果' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    const events: Array<{
      type: string;
      activity?: { id: string; name: string; status: string; thinking?: string };
    }> = [];
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '写推广文案', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    const planning = events.filter(event => event.type === 'activity' && event.activity?.name === 'manager_planning');
    const runningThinking = planning.filter(event => event.activity?.status === 'running' && event.activity.thinking);
    const doneIndex = planning.findIndex(event => event.activity?.status === 'done');
    expect(runningThinking.map(event => event.activity?.thinking)).toEqual([
      '先分析', '先分析目标，再选择', '先分析目标，再选择文案',
    ]);
    expect(new Set(planning.map(event => event.activity?.id)).size).toBe(1);
    expect(doneIndex).toBeGreaterThan(-1);
    expect(planning.slice(0, doneIndex).filter(event => event.activity?.thinking)).toHaveLength(3);
    expect(planning[doneIndex]?.activity?.thinking).toBe('先分析目标，再选择文案');
    expect(result.messages[0]?.thinking).toBe('先分析目标，再选择文案');
    expect(JSON.stringify(result.plan)).not.toContain('sf:thinking');
    expect(result.messages[0]?.content).not.toContain('sf:thinking');
  });

  it('模型输入/输出 token 写入公司级用量流水，供数据看板统计', async () => {
    const callImpl = async function* () {
      yield { type: 'text-delta' as const, text: '你好，老板' };
      yield { type: 'usage' as const, usage: { input_tokens: 321, output_tokens: 123, cache_read_input_tokens: 21 } };
    };
    const conversation = createXuebanConversation('co1', 'single');
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '你好', config,
      authorization: 'Bearer test', callImpl,
    });
    const rows = getDb().prepare(
      'SELECT company_id, session_id, model, input_tokens, cached_input_tokens, output_tokens FROM token_usage',
    ).all();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      company_id: 'co1',
      session_id: conversation.id,
      model: 'gpt-test',
      input_tokens: 321,
      cached_input_tokens: 21,
      output_tokens: 123,
    });
  });

  it('总经理规划与员工执行的真实 token 都记入同一公司流水', async () => {
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'expert', tasks: [{ agentId: 'e1', title: '撰写文案', prompt: '写推广文案' }],
        }) };
        yield { type: 'usage' as const, usage: { input_tokens: 100, output_tokens: 20 } };
        return;
      }
      yield { type: 'text-delta' as const, text: input.systemPrompt.includes('汇总教研员') ? '总经理汇总' : '员工成果' };
      yield { type: 'usage' as const, usage: { input_tokens: 200, output_tokens: 50 } };
    };
    const conversation = createXuebanConversation('co1', 'single');
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '写推广文案', config,
      authorization: 'Bearer test', callImpl,
    });
    const rows = getDb().prepare(
      'SELECT company_id, session_id, model, input_tokens, output_tokens FROM token_usage ORDER BY input_tokens',
    ).all() as Array<{ company_id: string; session_id: string; model: string; input_tokens: number; output_tokens: number }>;
    expect(rows).toHaveLength(3);
    expect(rows.every(row => row.company_id === 'co1' && row.session_id === conversation.id && row.model === 'gpt-test')).toBe(true);
    expect(rows.map(row => row.input_tokens)).toEqual([100, 200, 200]);
    expect(rows.map(row => row.output_tokens)).toEqual([20, 50, 50]);
  });

  it('员工输出上限足以容纳思考与正文（不再被 2600 截断）', async () => {
    const budgets: number[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'expert', tasks: [{ agentId: 'e1', title: '撰写文案', prompt: '写推广文案' }],
        }) };
        return;
      }
      budgets.push(Number(input.max_tokens));
      yield { type: 'text-delta' as const, text: '成果' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '写推广文案', config,
      authorization: 'Bearer test', callImpl,
    });
    // 员工 + 总经理汇总两次调用都必须拿到足够大的输出预算。
    expect(budgets).toHaveLength(2);
    expect(budgets.every(budget => budget >= 8192)).toBe(true);
  });

  it('模型只返回思考没有正文时，失败原因说明清楚并保留思考原文', async () => {
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'expert', tasks: [{ agentId: 'e1', title: '撰写文案', prompt: '写推广文案' }],
        }) };
        return;
      }
      yield { type: 'text-delta' as const, text: '<sf:thinking>先读数据，再算分层……' };
      yield { type: 'text-delta' as const, text: '</sf:thinking>' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '写推广文案', config,
      authorization: 'Bearer test', callImpl,
    });
    const failed = result.messages.find(message => message.sender_kind === 'agent');
    expect(failed?.status).toBe('failed');
    expect(failed?.content).toContain('只输出了思考过程');
    expect(failed?.thinking).toBe('先读数据，再算分层……');
    const stored = listXuebanMessages(conversation.id).find(message => message.sender_kind === 'agent');
    expect(stored?.thinking).toBe('先读数据，再算分层……');
  });

  it('旧消息表先补字段再创建轮次索引', () => {
    getDb().exec(`CREATE TABLE xueban_chat_messages (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_kind TEXT NOT NULL,
      sender_name TEXT NOT NULL, agent_id TEXT, content TEXT NOT NULL,
      channel TEXT NOT NULL DEFAULT 'chat', created_at TEXT NOT NULL
    )`);
    expect(() => listXuebanMessages('legacy')).not.toThrow();
    const columns = getDb().prepare('PRAGMA table_info(xueban_chat_messages)').all() as Array<{ name: string }>;
    expect(columns.map(column => column.name)).toEqual(expect.arrayContaining(['run_id', 'task_id', 'thinking', 'metadata_json']));
  });

  it('删除按公司隔离且级联删除消息', () => {
    const conversation = createXuebanConversation('co1', 'single');
    expect(getXuebanConversation(conversation.id, 'co2')).toBeNull();
    expect(deleteXuebanConversation(conversation.id, 'co2')).toBe(false);
    expect(deleteXuebanConversation(conversation.id, 'co1')).toBe(true);
    expect(listXuebanMessages(conversation.id)).toEqual([]);
  });
});
