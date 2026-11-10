import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ProviderInput } from '../transport/api-clients';
import { _resetForTests } from '../storage/sqlite';
import {
  appendXuebanMessage,
  createXuebanApproval,
  createXuebanConversation,
  listXuebanApprovals,
  listXuebanMessages,
} from '../storage/xueban-chat';
import { decideXuebanChatApproval, XuebanChatError, runXuebanChatTurn } from '../services/xueban-chat';
import type { XuebanChatStreamEvent } from '../services/xueban-chat-stream';
import xuebanChatRouter from '../routes/xueban-chat';

const originalCwd = process.cwd();
let dir = '';
const config = { provider: 'openai' as const, model: 'gpt-test', apiKey: 'test-key' };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-dag-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes('/api/xueban/auth/me')) {
      return Response.json({ company: { id: 'co1', name: '测试公司' } });
    }
    return Response.json({ items: [
      { id: 'e1', name: '研究', title: '市场研究', status: 'idle' },
      { id: 'e2', name: '文案', title: '内容创作', status: 'idle' },
      { id: 'e3', name: '审核', title: '风控审核', status: 'idle' },
    ] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

describe('公司对话 DAG 与审批', () => {
  it('独立任务真实并行，下游只接收声明上游产出', async () => {
    const first = deferred();
    const second = deferred();
    const started: string[] = [];
    let downstreamPrompt = '';
    const events: XuebanChatStreamEvent[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'parallel', tasks: [
            { id: 'research', agentId: 'e1', title: '独立研究', prompt: '研究市场' },
            { id: 'copy', agentId: 'e2', title: '独立文案', prompt: '撰写文案' },
            { id: 'review', agentId: 'e3', title: '审核研究', prompt: '只审核研究', depends_on: ['research'] },
          ],
        }) };
        return;
      }
      if (input.systemPrompt.includes('汇总教研员')) {
        yield { type: 'text-delta' as const, text: '汇总完成' };
        return;
      }
      if (input.userMessage.includes('研究市场')) {
        started.push('e1');
        await first.promise;
        yield { type: 'text-delta' as const, text: '研究独有成果-RESEARCH' };
      } else if (input.userMessage.includes('撰写文案')) {
        started.push('e2');
        await second.promise;
        yield { type: 'text-delta' as const, text: '文案独有成果-COPY' };
      } else {
        started.push('e3');
        downstreamPrompt = input.userMessage;
        yield { type: 'text-delta' as const, text: '审核成果' };
      }
    };
    const conversation = createXuebanConversation('co1', 'group');
    const running = runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '并行研究、写文案，再审核研究', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    await vi.waitFor(() => expect(started.sort()).toEqual(['e1', 'e2']));
    expect(started).not.toContain('e3');
    const pending = events.filter(event => event.type === 'activity' && event.activity.name === 'employee_task' && event.activity.status === 'pending');
    // 计划不再预铺“等待上游”占位；数据流只随真实执行逐条出现。
    expect(pending).toHaveLength(0);
    first.resolve();
    await vi.waitFor(() => expect(started).toContain('e3'));
    second.resolve();
    const result = await running;
    expect(started).toContain('e3');
    expect(downstreamPrompt).toContain('研究独有成果-RESEARCH');
    expect(downstreamPrompt).not.toContain('文案独有成果-COPY');
    expect(result.plan?.dag.nodes.find(node => node.planner_task_id === 'review')?.depends_on).toHaveLength(1);
  });

  it('循环依赖被阻断且独立分支正常完成', async () => {
    const employeeCalls: string[] = [];
    const events: XuebanChatStreamEvent[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'pipeline', tasks: [
          { id: 'a', agentId: 'e1', title: '循环甲', prompt: '甲', depends_on: ['b'] },
          { id: 'b', agentId: 'e2', title: '循环乙', prompt: '乙', depends_on: ['a'] },
          { id: 'free', agentId: 'e3', title: '独立任务', prompt: '独立执行' },
        ] }) };
      } else {
        employeeCalls.push(input.userMessage);
        yield { type: 'text-delta' as const, text: input.systemPrompt.includes('汇总教研员') ? '部分成果汇总' : '独立成果' };
      }
    };
    const conversation = createXuebanConversation('co1', 'group');
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '测试循环', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    expect(employeeCalls.filter(prompt => prompt.includes('独立执行'))).toHaveLength(1);
    expect(employeeCalls.some(prompt => /分配给你的工作：循环[甲乙]/.test(prompt))).toBe(false);
    expect(events.filter(event => event.type === 'activity' && event.activity.status === 'blocked')).toHaveLength(2);
    expect(result.plan?.dag.nodes.filter(node => node.validation_error === '依赖关系存在循环')).toHaveLength(2);
  });

  it('批准后继续当前任务及下游，重复决定返回冲突', async () => {
    const events: XuebanChatStreamEvent[] = [];
    const employeePrompts: string[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'pipeline', tasks: [
          { id: 'release', agentId: 'e1', title: '对外发布', prompt: '待老板审批后发布方案', requires_approval: true },
          { id: 'review', agentId: 'e2', title: '后续复盘', prompt: '复盘发布', depends_on: ['release'] },
        ] }) };
      } else if (input.systemPrompt.includes('汇总教研员')) {
        yield { type: 'text-delta' as const, text: '审批后成果汇总' };
      } else {
        employeePrompts.push(input.userMessage);
        yield { type: 'text-delta' as const, text: input.userMessage.includes('发布方案') ? '发布方案成果' : '复盘成果' };
      }
    };
    const conversation = createXuebanConversation('co1', 'group');
    const running = runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '审批后发布并复盘', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    await vi.waitFor(() => {
      expect(events.some(event => event.type === 'approval' && event.approval.status === 'pending')).toBe(true);
    });
    const pending = events.find((event): event is Extract<XuebanChatStreamEvent, { type: 'approval' }> => event.type === 'approval');
    expect(employeePrompts).toHaveLength(0);
    const decided = decideXuebanChatApproval({
      approvalId: pending!.approval.id, conversationId: conversation.id, companyId: 'co1', decision: 'approve',
    });
    expect(decided.approval.outcome).toBe('continue');
    await running;
    expect(employeePrompts).toHaveLength(2);
    expect(listXuebanApprovals(conversation.id)[0]).toMatchObject({ status: 'decided', decision: 'approve', outcome: 'continue' });
    expect(() => decideXuebanChatApproval({
      approvalId: pending!.approval.id, conversationId: conversation.id, companyId: 'co1', decision: 'reject',
    })).toThrowError(expect.objectContaining<Partial<XuebanChatError>>({ status: 409, code: 'APPROVAL_ALREADY_DECIDED' }));

    // 审批卡只能有一张：原消息改成“已决定”，另加一条不带审批卡片的时间线消息。
    const approvalCards = listXuebanMessages(conversation.id).filter(message => message.metadata_json?.includes(`"id":"${pending!.approval.id}"`));
    expect(approvalCards).toHaveLength(1);
    expect(JSON.parse(approvalCards[0].metadata_json!)).toMatchObject({ approval: { status: 'decided', decision: 'approve' } });
  });

  it('普通汇报即使被误标、正文提到“不对外发布”，也不会触发审批', async () => {
    const events: XuebanChatStreamEvent[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'expert', tasks: [
            {
              id: 'summary', agentId: 'e1', title: '生成公司阶段小结',
              prompt: '老板要求：生成公司阶段小结。本小结为内部汇报稿，未经老板确认前不对外发布。',
              requires_approval: true,
            },
          ],
        }) };
        return;
      }
      if (input.systemPrompt.includes('汇总教研员')) { yield { type: 'text-delta' as const, text: '汇总完成' }; return; }
      yield { type: 'text-delta' as const, text: '阶段小结正文' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '生成公司阶段小结', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    expect(result.plan?.dag.nodes[0].requires_approval).toBe(false);
    expect(events.some(event => event.type === 'approval')).toBe(false);
    expect(listXuebanApprovals(conversation.id)).toHaveLength(0);
  });

  it('需要审批时卡片展示具体摘要，不泄露岗位系统提示词', async () => {
    const events: XuebanChatStreamEvent[] = [];
    const brief = '将本周市场周报发布到公司官网与公众号，内容为 3 条已核实结论，计划周一 10:00 发布。';
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({
          intent: 'task', strategy: 'expert', tasks: [
            { id: 'publish', agentId: 'e2', title: '对外发布市场周报', prompt: '统一口径后对外发布。', requires_approval: true, approval_brief: brief },
          ],
        }) };
        return;
      }
      if (input.systemPrompt.includes('汇总教研员')) { yield { type: 'text-delta' as const, text: '发布完成' }; return; }
      yield { type: 'text-delta' as const, text: '已整理发布内容' };
    };
    const conversation = createXuebanConversation('co1', 'single');
    const running = runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '把这周的周报发出去', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    await vi.waitFor(() => {
      expect(events.some(event => event.type === 'approval')).toBe(true);
    });
    const pending = events.find((event): event is Extract<XuebanChatStreamEvent, { type: 'approval' }> => event.type === 'approval');
    expect(pending!.approval.content).toContain('发布到公司官网');
    expect(pending!.approval.content).not.toMatch(/你是|老板要求|岗位/);
    decideXuebanChatApproval({ approvalId: pending!.approval.id, conversationId: conversation.id, companyId: 'co1', decision: 'approve' });
    await running;
  });
});

describe('公司对话日志接口', () => {
  it('按公司隔离、按 run 分组并支持游标分页', async () => {
    const co1First = createXuebanConversation('co1', 'group', '公司一日志');
    const co1Second = createXuebanConversation('co1', 'group', '公司一第二页');
    const co2 = createXuebanConversation('co2', 'group', '公司二秘密');
    appendXuebanMessage(co1First.id, { sender_kind: 'ceo', sender_name: '总经理', content: '公司一回复', run_id: 'run-co1' });
    createXuebanApproval({ company_id: 'co1', conversation_id: co1First.id, run_id: 'run-co1', task_id: 'task-1', task_title: '审批发布' });
    appendXuebanMessage(co2.id, { sender_kind: 'ceo', sender_name: '总经理', content: '公司二回复', run_id: 'run-co2' });
    vi.stubGlobal('fetch', vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string> | undefined;
      const token = headers?.Authorization ?? headers?.authorization;
      return Response.json({ company: token === 'Bearer co2' ? { id: 'co2', name: '公司二' } : { id: 'co1', name: '公司一' } });
    }));
    const app = express();
    app.use(express.json());
    app.use('/api/xueban-chat', xuebanChatRouter);

    const firstPage = await request(app).get('/api/xueban-chat/logs?limit=1').set('Authorization', 'Bearer co1').expect(200);
    expect(firstPage.body.items).toHaveLength(1);
    expect(firstPage.body.next_cursor).toBeTruthy();
    const allFirst = await request(app).get('/api/xueban-chat/logs?limit=10').set('Authorization', 'Bearer co1').expect(200);
    const companyOneLog = allFirst.body.items.find((item: { conversation: { id: string } }) => item.conversation.id === co1First.id);
    expect(companyOneLog.runs[0].run_id).toBe('run-co1');
    expect(companyOneLog.runs[0].messages[0].content).toBe('公司一回复');
    expect(companyOneLog.runs[0].approvals[0].task_title).toBe('审批发布');
    expect(JSON.stringify(allFirst.body)).not.toContain('公司二秘密');

    const secondPage = await request(app).get(`/api/xueban-chat/logs?limit=1&cursor=${encodeURIComponent(firstPage.body.next_cursor)}`).set('Authorization', 'Bearer co1').expect(200);
    expect(secondPage.body.items).toHaveLength(1);
    expect(secondPage.body.items[0].conversation.id).not.toBe(firstPage.body.items[0].conversation.id);
    expect([co1First.id, co1Second.id]).toContain(secondPage.body.items[0].conversation.id);

    const otherCompany = await request(app).get('/api/xueban-chat/logs').set('Authorization', 'Bearer co2').expect(200);
    expect(otherCompany.body.items).toHaveLength(1);
    expect(otherCompany.body.items[0].conversation.id).toBe(co2.id);
    expect(JSON.stringify(otherCompany.body)).not.toContain('公司一回复');
  });
});
