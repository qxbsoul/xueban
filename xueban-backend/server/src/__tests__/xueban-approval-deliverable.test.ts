/* 人工审批内容与交付成果聚合：验证左侧「人工审批」「交付成果」的数据来源。 */
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
  listCompanyXuebanApprovals,
  listXuebanDeliverableConversations,
  listXuebanMessages,
} from '../storage/xueban-chat';
import { decideXuebanChatApproval, runXuebanChatTurn } from '../services/xueban-chat';
import type { XuebanChatStreamEvent } from '../services/xueban-chat-stream';
import xuebanChatRouter from '../routes/xueban-chat';

const originalCwd = process.cwd();
let dir = '';
const config = { provider: 'openai' as const, model: 'gpt-test', apiKey: 'test-key' };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-deliverable-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '测试公司' } });
    return Response.json({ items: [
      { id: 'e1', name: '研究', title: '市场研究', status: 'idle' },
      { id: 'e2', name: '文案', title: '内容创作', status: 'idle' },
    ] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('人工审批内容', () => {
  it('历史审批没有正文时，用 DAG 里的审批摘要回填', () => {
    const conversation = createXuebanConversation('co1', 'single', '老审批对话');
    appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '总经理', channel: 'workflow', run_id: 'run-old', content: '意图：任务',
      metadata: { dag: { nodes: [{
        id: 'run-old:task:1', title: '发布方案',
        approval_brief: '把方案发布到公司官网并抄送销售，发布时间为周五 10:00。',
        prompt: '你是项目经理，老板要求：把方案发布到官网并抄送销售。',
      }] } },
    });
    createXuebanApproval({
      company_id: 'co1', conversation_id: conversation.id, run_id: 'run-old',
      task_id: 'run-old:task:1', task_title: '发布方案', content: '',
    });
    const listed = listCompanyXuebanApprovals('co1');
    expect(listed[0].content).toBe('把方案发布到公司官网并抄送销售，发布时间为周五 10:00。');
    // 没有摘要的旧数据只回显标题，绝不把岗位系统提示词透出来。
    expect(listed[0].content).not.toContain('你是');
    // 对话里的审批卡片（消息 metadata）也应被补齐
    const withCard = appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '总经理', channel: 'workflow', run_id: 'run-old',
      task_id: 'run-old:task:1', content: '请审批', metadata: { approval: { ...listed[0], content: '' } as never },
    });
    const message = listXuebanMessages(conversation.id).find(item => item.id === withCard.id)!;
    expect(message.metadata_json).toContain('把方案发布到公司官网');
  });

  it('审批落库时保存工单正文，左侧列表按公司跨对话返回', async () => {
    const events: XuebanChatStreamEvent[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'expert', tasks: [
          {
            id: 'release', agentId: 'e1', title: '对外发布',
            prompt: '把下季度投放方案发布到官网，并附上预算明细',
            requires_approval: true,
            approval_brief: '把下季度投放方案发布到公司官网，并附上预算明细，计划周五 10:00 上线。',
          },
        ] }) };
        return;
      }
      yield { type: 'text-delta' as const, text: '发布完成' };
    };
    const conversation = createXuebanConversation('co1', 'single', '投放发布');
    const running = runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '发布下季度投放方案', config,
      authorization: 'Bearer test', callImpl, onEvent: event => events.push(event),
    });
    await vi.waitFor(() => {
      expect(events.some(event => event.type === 'approval')).toBe(true);
    });
    const pending = events.find((event): event is Extract<XuebanChatStreamEvent, { type: 'approval' }> => event.type === 'approval')!;

    // 审批对象必须是老板能看懂的摘要，而不是岗位系统提示词。
    expect(pending.approval.content).toContain('把下季度投放方案发布到公司官网');
    expect(pending.approval.content).toContain('预算明细');
    expect(pending.approval.content).not.toMatch(/你是|老板要求|岗位/);
    const listed = listCompanyXuebanApprovals('co1');
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({ task_title: '对外发布' });
    // 第一轮发言会把对话改名成老板的目标，标题取自真实会话记录。
    expect(listed[0].conversation_title).toBe('发布下季度投放方案');
    expect(listed[0].content).toContain('预算明细');
    // 公司隔离：其他公司看不到。
    expect(listCompanyXuebanApprovals('co2')).toHaveLength(0);

    decideXuebanChatApproval({
      approvalId: pending.approval.id, conversationId: conversation.id, companyId: 'co1', decision: 'approve',
    });
    await running;
    expect(listCompanyXuebanApprovals('co1')[0].status).toBe('decided');
  });
});

describe('交付成果聚合', () => {
  it('以对话为单位：最终成果取总经理正式回答，员工产出为次要记录', () => {
    const first = createXuebanConversation('co1', 'single', '客户联系计划');
    appendXuebanMessage(first.id, { sender_kind: 'ceo', sender_name: '总经理', channel: 'workflow', content: 'DAG 分工（过程）', run_id: 'run-1' });
    appendXuebanMessage(first.id, { sender_kind: 'agent', sender_name: '周数', task_title: '数据分析', content: '员工数据分析成果', status: 'done', channel: 'workflow', run_id: 'run-1' });
    appendXuebanMessage(first.id, { sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: '这是最终交付成果', run_id: 'run-1' });

    const second = createXuebanConversation('co1', 'single', '还没跑完');
    appendXuebanMessage(second.id, { sender_kind: 'user', sender_name: '老板', content: '帮我做一版投放方案' });
    const other = createXuebanConversation('co2', 'single', '别家公司');
    appendXuebanMessage(other.id, { sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: '别家公司成果' });

    const items = listXuebanDeliverableConversations('co1');
    expect(items.map(item => item.id)).toEqual(expect.arrayContaining([first.id, second.id]));
    expect(items.map(item => item.id)).not.toContain(other.id);
    const delivered = items.find(item => item.id === first.id)!;
    expect(delivered.title).toBe('客户联系计划');
    expect(delivered.final?.content).toBe('这是最终交付成果');
    expect(delivered.employees.map(item => item.name)).toEqual(['周数']);
    expect(delivered.employees[0].content).toBe('员工数据分析成果');
    const empty = items.find(item => item.id === second.id)!;
    expect(empty.final).toBeNull();
  });

  it('只有分工没有最终答复时，最终成果必须是空，不能拿分工冒充', () => {
    const conversation = createXuebanConversation('co1', 'single', '只跑了一半');
    appendXuebanMessage(conversation.id, { sender_kind: 'user', sender_name: '老板', content: '做个方案' });
    // 单聊里分工是 workflow；群聊里分工也走 chat，但 metadata 带 dag，同样不算交付。
    appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '总经理', channel: 'workflow',
      content: '意图：任务 · 策略：pipeline\n1. 某员工：某工单', run_id: 'run-half',
    });
    appendXuebanMessage(conversation.id, {
      sender_kind: 'agent', sender_name: '某员工', task_title: '某工单', content: '员工成果', status: 'done', channel: 'workflow', run_id: 'run-half',
    });
    const item = listXuebanDeliverableConversations('co1').find(entry => entry.id === conversation.id)!;
    expect(item.final).toBeNull();
    expect(item.employees).toHaveLength(1);

    const groupChat = createXuebanConversation('co1', 'group', '群聊半截');
    appendXuebanMessage(groupChat.id, { sender_kind: 'user', sender_name: '老板', content: '做个方案' });
    appendXuebanMessage(groupChat.id, {
      sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: 'DAG 分工（chat 通道）',
      metadata: { dag: { nodes: [{ id: 'n1', prompt: 'p' }] } },
    });
    const groupItem = listXuebanDeliverableConversations('co1').find(entry => entry.id === groupChat.id)!;
    expect(groupItem.final).toBeNull();
  });

  it('待审批数量随审批状态更新', async () => {
    const conversation = createXuebanConversation('co1', 'single', '等审批的对话');
    appendXuebanMessage(conversation.id, { sender_kind: 'user', sender_name: '老板', content: '对外发布方案' });
    const approval = createXuebanApproval({
      company_id: 'co1', conversation_id: conversation.id, run_id: 'run-2',
      task_id: 'run-2:task:1', task_title: '对外发布', content: '发布方案正文',
    });
    expect(listXuebanDeliverableConversations('co1')[0].pending_approvals).toBe(1);
    decideXuebanChatApproval({ approvalId: approval.id, conversationId: conversation.id, companyId: 'co1', decision: 'reject' });
    expect(listXuebanDeliverableConversations('co1')[0].pending_approvals).toBe(0);
  });
});

describe('审批与交付接口', () => {
  it('/approvals 与 /deliverables 不被 /:id 吞掉，且按公司隔离', async () => {
    const co1 = createXuebanConversation('co1', 'single', '公司一对话');
    appendXuebanMessage(co1.id, { sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: '公司一最终成果', run_id: 'run-1' });
    createXuebanApproval({
      company_id: 'co1', conversation_id: co1.id, run_id: 'run-1', task_id: 't1',
      task_title: '对外发布', content: '发布方案正文',
    });
    const co2 = createXuebanConversation('co2', 'single', '公司二对话');
    appendXuebanMessage(co2.id, { sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: '公司二最终成果', run_id: 'run-2' });

    vi.stubGlobal('fetch', vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string> | undefined;
      const token = headers?.Authorization ?? headers?.authorization;
      return Response.json({ company: token === 'Bearer co2' ? { id: 'co2', name: '公司二' } : { id: 'co1', name: '公司一' } });
    }));
    const app = express();
    app.use(express.json());
    app.use('/api/xueban-chat', xuebanChatRouter);

    const approvals = await request(app).get('/api/xueban-chat/approvals').set('Authorization', 'Bearer co1').expect(200);
    expect(approvals.body.items).toHaveLength(1);
    expect(approvals.body.items[0]).toMatchObject({ task_title: '对外发布', conversation_title: '公司一对话', content: '发布方案正文' });

    const deliverables = await request(app).get('/api/xueban-chat/deliverables').set('Authorization', 'Bearer co1').expect(200);
    expect(deliverables.body.items).toHaveLength(1);
    expect(deliverables.body.items[0].final.content).toBe('公司一最终成果');
    expect(JSON.stringify(deliverables.body)).not.toContain('公司二最终成果');

    const other = await request(app).get('/api/xueban-chat/deliverables').set('Authorization', 'Bearer co2').expect(200);
    expect(other.body.items.map((item: { title: string }) => item.title)).toEqual(['公司二对话']);
  });
});
