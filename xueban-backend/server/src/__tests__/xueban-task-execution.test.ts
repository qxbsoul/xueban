import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const { callProviderMock } = vi.hoisted(() => ({ callProviderMock: vi.fn() }));
vi.mock('../transport/api-clients', async (importOriginal) => ({
  ...await importOriginal<typeof import('../transport/api-clients')>(),
  callProvider: callProviderMock,
}));

import xuebanTaskExecutionRouter from '../routes/xueban-task-execution';
import { _resetForTests } from '../storage/sqlite';

const app = express();
app.use(express.json());
app.use('/api/xueban-internal', xuebanTaskExecutionRouter);

const originalCwd = process.cwd();
let dir = '';
const body = {
  company_id: 'co1',
  provider: 'mimo',
  model: 'mimo-v2.6-pro',
  system: '你是文案',
  user: '写一句真实文案',
};

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-task-execution-'));
  process.chdir(dir);
  _resetForTests();
  delete process.env.MIMO_API_KEY;
  vi.clearAllMocks();
  callProviderMock.mockImplementation(async function* () {
    yield { type: 'text-delta', text: '<sf:thinking>先理解需求</sf:thinking>' };
    yield { type: 'text-delta', text: '真实模型交付' };
    yield { type: 'usage', usage: { input_tokens: 21, output_tokens: 8 } };
    yield { type: 'end' };
  });
  vi.stubGlobal('fetch', vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
    const authorization = new Headers(init?.headers).get('authorization');
    if (authorization !== 'Bearer co1') {
      return Response.json({ error: 'unauthorized' }, { status: 401 });
    }
    if (String(_input).includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
    return Response.json({ items: [{ id: 'e1', name: '研究员', title: '市场研究', status: 'idle', tools: ['web_search'] }] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('POST /api/xueban-internal/tasks/complete', () => {
  it('映射真实 text/thinking/live/model/usage，并转发专用模型密钥头', async () => {
    const res = await request(app)
      .post('/api/xueban-internal/tasks/complete')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send(body)
      .expect(200);

    expect(res.body).toEqual({
      text: '真实模型交付',
      thinking: '先理解需求',
      mode: 'live',
      model: 'mimo-v2.6-pro',
      usage: { input_tokens: 21, output_tokens: 8 },
    });
    expect(callProviderMock).toHaveBeenCalledOnce();
    const [provider, input] = callProviderMock.mock.calls[0];
    expect(provider).toBe('mimo');
    expect(input).toMatchObject({
      systemPrompt: body.system,
      userMessage: body.user,
      model: body.model,
      api_key: 'sk-mimo-task',
    });
    expect(input.api_key).not.toBe('co1');
  });

  it('拒绝跨公司执行，且不调用模型', async () => {
    const res = await request(app)
      .post('/api/xueban-internal/tasks/complete')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send({ ...body, company_id: 'co2' })
      .expect(403);

    expect(res.body.error.code).toBe('COMPANY_MISMATCH');
    expect(callProviderMock).not.toHaveBeenCalled();
  });

  it('公司 Authorization 不会冒充模型 key；没有个人 Key 时使用内置 Key', async () => {
    const res = await request(app)
      .post('/api/xueban-internal/tasks/complete')
      .set('Authorization', 'Bearer co1')
      .send(body)
      .expect(200);

    const input = callProviderMock.mock.calls[0]?.[1] as { api_key?: string } | undefined;
    expect(input?.api_key).toBeTruthy();
    expect(input?.api_key).not.toBe('co1');
    expect(input?.api_key).not.toContain('Bearer');
    expect(JSON.stringify(res.body)).not.toContain('simulated');
  });

  it('模型错误和空输出均 fail closed，不返回 simulated 结果', async () => {
    callProviderMock.mockImplementationOnce(async function* () {
      yield { type: 'error', code: 'PROVIDER_ERROR', message: 'upstream down' };
    });
    const failed = await request(app)
      .post('/api/xueban-internal/tasks/complete')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send(body)
      .expect(502);
    expect(failed.body.error.code).toBe('PROVIDER_ERROR');

    callProviderMock.mockImplementationOnce(async function* () {
      yield { type: 'text-delta', text: '<sf:thinking>只有思考</sf:thinking>' };
      yield { type: 'end' };
    });
    const empty = await request(app)
      .post('/api/xueban-internal/tasks/complete')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send(body)
      .expect(502);
    expect(empty.body.error.code).toBe('LLM_EMPTY_RESPONSE');
    expect(JSON.stringify({ failed: failed.body, empty: empty.body })).not.toContain('simulated');
  });
});

describe('POST /api/xueban-internal/schedules/execute', () => {
  it('真实进入指定员工的总经理编排，并返回可追溯对话', async () => {
    const res = await request(app)
      .post('/api/xueban-internal/schedules/execute')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send({ company_id: 'co1', agent_id: 'e1', task_description: '生成每日市场简报' })
      .expect(200);
    expect(res.body.data.conversation_id).toBeTruthy();
    expect(res.body.data.output_summary).toBe('真实模型交付');
    expect(res.body.data.message_count).toBeGreaterThan(0);
    const { getXuebanConversation, listXuebanMessages } = await import('../storage/xueban-chat');
    expect(getXuebanConversation(res.body.data.conversation_id, 'co1')?.title).toContain('生成每日市场简报');
    expect(listXuebanMessages(res.body.data.conversation_id).some(message => message.sender_kind === 'ceo' && message.channel === 'chat')).toBe(true);
  });

  it('模型执行失败时仍保留工作台对话，并把失败原因交回调度器', async () => {
    callProviderMock.mockImplementation(async function* () {
      yield { type: 'error', code: 'PROVIDER_ERROR', message: 'upstream down' };
    });
    const res = await request(app)
      .post('/api/xueban-internal/schedules/execute')
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-mimo-task')
      .send({ company_id: 'co1', task_description: '生成失败示例' })
      .expect(200);
    expect(res.body.data.status).toBe('failed');
    expect(res.body.data.error).toContain('upstream down');
    expect(res.body.data.conversation_id).toBeTruthy();
    const { getXuebanConversation, listXuebanMessages } = await import('../storage/xueban-chat');
    expect(getXuebanConversation(res.body.data.conversation_id, 'co1')?.title).toContain('定时任务');
    expect(listXuebanMessages(res.body.data.conversation_id).some(message => message.content.includes('定时任务执行失败'))).toBe(true);
  });

  it('其他公司不允许执行；没有配置 Key 时明确拒绝执行', async () => {
    await request(app).post('/api/xueban-internal/schedules/execute')
      .set('Authorization', 'Bearer co1').set('X-MiMo-Key', 'sk-mimo-task')
      .send({ company_id: 'co2', task_description: '越权任务' }).expect(403);
    const missing = await request(app).post('/api/xueban-internal/schedules/execute')
      .set('Authorization', 'Bearer co1')
      .send({ company_id: 'co1', task_description: '没有配置密钥' }).expect(401);
    expect(missing.body.error.code).toBe('NO_API_KEY');
  });
});
