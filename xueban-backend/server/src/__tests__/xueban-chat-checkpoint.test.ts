import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ProviderChunk, ProviderInput } from '../transport/api-clients';
import { _resetForTests } from '../storage/sqlite';
import { appendXuebanMessage, createXuebanConversation, listXuebanMessages } from '../storage/xueban-chat';
import { runXuebanChatTurn } from '../services/xueban-chat';
import type { XuebanChatStreamEvent } from '../services/xueban-chat-stream';

const originalCwd = process.cwd();
let dir = '';
const config = { provider: 'openai' as const, model: 'gpt-test', apiKey: 'test-key' };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-checkpoint-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [
    { id: 'e1', name: '研究', title: '市场研究', system_prompt: '负责一', status: 'idle' },
    { id: 'e2', name: '文案', title: '内容创作', system_prompt: '负责二', status: 'idle' },
  ] })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

function checkpointIds(events: XuebanChatStreamEvent[]): string[] {
  return events.flatMap(event => {
    if (event.type !== 'checkpoint' || !event.message.metadata_json) return [];
    const metadata = JSON.parse(event.message.metadata_json) as { checkpoint?: { id: string } };
    return metadata.checkpoint ? [metadata.checkpoint.id] : [];
  });
}

describe('员工执行前 Checkpoint 与最近发言编辑', () => {
  it('从第二个员工重做时保留第一员工成果、不重新规划并生成新下游交接', async () => {
    let planningCalls = 0;
    let firstEmployeeCalls = 0;
    let secondEmployeeCalls = 0;
    let latestSecondPrompt = '';
    let latestSummaryPrompt = '';
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        planningCalls += 1;
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'pipeline', tasks: [
          { id: 'research', agentId: 'e1', title: '研究', prompt: '先研究' },
          { id: 'copy', agentId: 'e2', title: '文案', prompt: '再写文案', depends_on: ['research'] },
        ] }) };
      } else if (input.systemPrompt.includes('汇总教研员')) {
        latestSummaryPrompt = input.userMessage;
        yield { type: 'text-delta' as const, text: '最终汇总' };
      } else if (input.systemPrompt.includes('负责一')) {
        firstEmployeeCalls += 1;
        yield { type: 'text-delta' as const, text: '上游-v1' };
      } else {
        secondEmployeeCalls += 1;
        latestSecondPrompt = input.userMessage;
        yield { type: 'text-delta' as const, text: `下游-v${secondEmployeeCalls}` };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    const events: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '先研究再写文案', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event) });
    const checkpoints = checkpointIds(events);
    expect(checkpoints).toHaveLength(2);
    expect(firstEmployeeCalls).toBe(1);
    expect(secondEmployeeCalls).toBe(1);
    const originalUsers = listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'user');

    const redoEvents: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '', config,
      authorization: 'Bearer test', callImpl, checkpointId: checkpoints[1], onEvent: event => redoEvents.push(event) });

    expect(planningCalls).toBe(1);
    expect(firstEmployeeCalls).toBe(1);
    expect(secondEmployeeCalls).toBe(2);
    expect(latestSecondPrompt).toContain('上游-v1');
    expect(latestSummaryPrompt).toContain('下游-v2');
    expect(latestSummaryPrompt).not.toContain('下游-v1');
    expect(redoEvents[0]?.type).toBe('conversation-restored');
    expect(listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'user')).toEqual(originalUsers);
    expect(listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'agent' && message.content === '上游-v1')).toHaveLength(1);
  });

  it('只允许编辑最近老板发言，截断旧轮次且新上下文不包含旧内容', async () => {
    const planningPrompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        planningPrompts.push(input.userMessage);
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'expert', tasks: [
          { agentId: 'e1', title: '执行', prompt: '执行当前目标' },
        ] }) };
      } else {
        yield { type: 'text-delta' as const, text: input.systemPrompt.includes('汇总教研员') ? '新汇总' : '新成果' };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '第一轮旧目标', config, authorization: 'Bearer test', callImpl });
    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '第二轮待修改', config, authorization: 'Bearer test', callImpl });
    const users = listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'user');
    const oldSecondRun = users[1].run_id;
    const oldSecondMessageIds = listXuebanMessages(conversation.id).filter(message => message.run_id === oldSecondRun && message.sender_kind !== 'user').map(message => message.id);
    await expect(runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '非法修改', config,
      authorization: 'Bearer test', callImpl, editMessageId: users[0].id })).rejects.toMatchObject({ code: 'ONLY_LATEST_USER_MESSAGE_EDITABLE' });

    appendXuebanMessage(conversation.id, { sender_kind: 'user', sender_name: '老板', channel: 'workflow', run_id: oldSecondRun, content: '批准：继续执行' });
    const events: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({ conversationId: conversation.id, companyId: 'co1', content: '第二轮最新目标', config,
      authorization: 'Bearer test', callImpl, editMessageId: users[1].id, onEvent: event => events.push(event) });
    const stored = listXuebanMessages(conversation.id);
    expect(stored.filter(message => message.sender_kind === 'user').map(message => message.content)).toEqual(['第一轮旧目标', '第二轮最新目标']);
    expect(stored.some(message => message.run_id === oldSecondRun && message.sender_kind !== 'user')).toBe(true);
    expect(stored.some(message => oldSecondMessageIds.includes(message.id))).toBe(false);
    expect(planningPrompts.at(-1)).toContain('第二轮最新目标');
    expect(planningPrompts.at(-1)).not.toContain('第二轮待修改');
    expect(planningPrompts.at(-1)).toContain('第一轮旧目标');
    expect(events[0]?.type).toBe('conversation-restored');
  });

  it('跨公司、跨对话恢复被拒绝且不修改原对话', async () => {
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'expert', tasks: [{ agentId: 'e1', title: '执行', prompt: '执行' }] }) };
      } else {
        yield { type: 'text-delta' as const, text: '真实结果' };
      }
    };
    const source = createXuebanConversation('co1', 'single');
    const events: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({ conversationId: source.id, companyId: 'co1', content: '生成', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event) });
    const checkpointId = checkpointIds(events)[0];
    const before = listXuebanMessages(source.id);
    const other = createXuebanConversation('co2', 'single');
    await expect(runXuebanChatTurn({ conversationId: other.id, companyId: 'co2', content: '', config,
      authorization: 'Bearer test', callImpl, checkpointId })).rejects.toMatchObject({ code: 'CHECKPOINT_NOT_FOUND' });
    expect(listXuebanMessages(source.id)).toEqual(before);
  });

  it('并行分支回滚：只重跑失败任务，不重复执行已成功的并行同事', async () => {
    let aCalls = 0;
    let bCalls = 0;
    const callImpl = async function* (_provider: string, input: ProviderInput): AsyncGenerator<ProviderChunk> {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'parallel', tasks: [
          { id: 'a', agentId: 'e1', title: '任务A', prompt: '做 A' },
          { id: 'b', agentId: 'e2', title: '任务B', prompt: '做 B' },
        ] }) };
      } else if (input.systemPrompt.includes('汇总教研员')) {
        yield { type: 'text-delta' as const, text: '最终汇总' };
      } else if (input.systemPrompt.includes('负责一')) {
        aCalls += 1;
        if (aCalls === 1) yield { type: 'error' as const, code: 'PROVIDER_ERROR', message: 'A 第一次失败' };
        else yield { type: 'text-delta' as const, text: 'A-第二次成功' };
      } else {
        bCalls += 1;
        yield { type: 'text-delta' as const, text: 'B-成功' };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    const events: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '并行做 A 和 B', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    expect(aCalls).toBe(1);
    expect(bCalls).toBe(1);
    const checkpoints = checkpointIds(events);
    expect(checkpoints).toHaveLength(2);
    const checkpointMessages = listXuebanMessages(conversation.id).filter(message => message.metadata_json?.includes('"checkpoint"'));
    const firstCheckpoint = checkpointMessages.find(message => JSON.parse(message.metadata_json!).checkpoint.task_title === '任务A')!;
    const checkpointA = JSON.parse(firstCheckpoint.metadata_json!).checkpoint.id as string;
    expect(firstCheckpoint.content).toContain('恢复到：执行「任务A」之前');
    expect(firstCheckpoint.content).toContain('本次重跑：「任务A」及其下游');
    expect(firstCheckpoint.content).toContain('任务B');

    const redoEvents: XuebanChatStreamEvent[] = [];
    await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '', config,
      authorization: 'Bearer test', callImpl, checkpointId: checkpointA, onEvent: event => redoEvents.push(event),
    });
    // A 重跑成功；B 的成果从并行分支合并回来，不重复调用。
    expect(aCalls).toBe(2);
    expect(bCalls).toBe(1);
    const agentMessages = listXuebanMessages(conversation.id).filter(message => message.sender_kind === 'agent');
    expect(agentMessages.filter(message => message.sender_name === '文案')).toHaveLength(1);
    expect(agentMessages.filter(message => message.sender_name === '研究' && message.content === 'A-第二次成功')).toHaveLength(1);
  });

  it('总经理规划时模型调用失败会明确失败并标注，不再静默降级为规则分工', async () => {
    const events: XuebanChatStreamEvent[] = [];
    let employeeCalls = 0;
    const callImpl = async function* (_provider: string, input: ProviderInput): AsyncGenerator<ProviderChunk> {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'error' as const, code: 'PROVIDER_ERROR', message: 'bad key' };
        return;
      }
      employeeCalls += 1;
      yield { type: 'text-delta' as const, text: '不该执行' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    await expect(runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '安排行业简报', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    })).rejects.toMatchObject({
      code: 'PROVIDER_ERROR',
      message: expect.stringContaining('教学主管规划失败'),
    });
    expect(employeeCalls).toBe(0);
    const failed = events.find((event): event is Extract<XuebanChatStreamEvent, { type: 'activity' }> => event.type === 'activity'
      && (event as Extract<XuebanChatStreamEvent, { type: 'activity' }>).activity.name === 'manager_planning'
      && (event as Extract<XuebanChatStreamEvent, { type: 'activity' }>).activity.status === 'failed');
    expect(failed?.activity.detail).toContain('gpt-test');
    expect(failed?.activity.detail).toContain('bad key');
  });
});
