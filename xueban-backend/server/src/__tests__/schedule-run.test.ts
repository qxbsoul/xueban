import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const { callProviderMock } = vi.hoisted(() => ({ callProviderMock: vi.fn() }));
vi.mock('../transport/api-clients', async (importOriginal) => ({
  ...await importOriginal<typeof import('../transport/api-clients')>(),
  callProvider: callProviderMock,
}));

import scheduleRunRouter from '../routes/schedule-run';
import { _resetForTests } from '../storage/sqlite';

const app = express();
app.use(express.json());
app.use('/api/schedules', scheduleRunRouter);

const originalCwd = process.cwd();
const originalSchedulesDir = process.env.XUEBAN_SCHEDULES_DIR;
let dir = '';
let schedulesDir = '';
const scheduleId = 'sched-abc';
const scheduleRecord = {
  schedule_id: scheduleId,
  company_id: 'co1',
  group_id: 'co1',
  cron_expression: '0 9 * * *',
  start_at: null,
  agent_id: 'e1',
  task_description: '生成每日市场简报',
  duration_min: 30,
  created_at: '2026-11-08T00:00:00+00:00',
  next_run_time: '2026-11-09T09:00:00+08:00',
  completed: false,
  enabled: true,
  runs: [] as Array<Record<string, unknown>>,
};

function stubFetch(runStatus: number) {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
    if (url.endsWith(`/api/schedules/${scheduleId}/run`)) {
      return runStatus === 200
        ? Response.json({ data: { run_id: 'python-run', status: 'succeeded', conversation_id: 'conv-python' }, meta: {} })
        : Response.json({ detail: { error: 'EXECUTOR_UNAVAILABLE', message: '任务执行器未注册' } }, { status: runStatus });
    }
    if (url.endsWith('/api/schedules')) return Response.json({ data: [scheduleRecord], meta: { count: 1 } });
    if (url.includes('/roster')) {
      return Response.json({ items: [
        { id: 'e1', name: '陈明远', title: 'CEO 助理', status: 'idle', tools: [] },
        { id: 'e2', name: '林研', title: '市场研究员', status: 'idle', tools: [] },
      ] });
    }
    return Response.json({ items: [] });
  }));
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-schedule-run-'));
  schedulesDir = join(dir, 'schedules');
  mkdirSync(schedulesDir, { recursive: true });
  writeFileSync(join(schedulesDir, `${scheduleId}.json`), JSON.stringify(scheduleRecord), 'utf8');
  process.env.XUEBAN_SCHEDULES_DIR = schedulesDir;
  process.chdir(dir);
  _resetForTests();
  delete process.env.MIMO_API_KEY;
  vi.clearAllMocks();
  callProviderMock.mockImplementation(async function* () {
    yield { type: 'text-delta', text: '今日行业简报：需求回暖。' };
    yield { type: 'usage', usage: { input_tokens: 10, output_tokens: 6 } };
    yield { type: 'end' };
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  if (originalSchedulesDir === undefined) delete process.env.XUEBAN_SCHEDULES_DIR;
  else process.env.XUEBAN_SCHEDULES_DIR = originalSchedulesDir;
  rmSync(dir, { recursive: true, force: true });
});

describe('POST /api/schedules/:id/run', () => {
  it('Python 没有执行桥时由 Node 兜底执行，并把运行记录写回调度文件', async () => {
    stubFetch(503);
    const res = await request(app)
      .post(`/api/schedules/${scheduleId}/run`)
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-test')
      .send({})
      .expect(200);
    expect(res.body.data.status).toBe('succeeded');
    expect(res.body.data.conversation_id).toBeTruthy();
    expect(res.body.meta.fallback).toBe(true);

    const record = JSON.parse(readFileSync(join(schedulesDir, `${scheduleId}.json`), 'utf8'));
    expect(record.runs).toHaveLength(1);
    expect(record.runs[0]).toMatchObject({ status: 'succeeded', conversation_id: res.body.data.conversation_id });

    const { getXuebanConversation } = await import('../storage/xueban-chat');
    expect(getXuebanConversation(res.body.data.conversation_id, 'co1')?.title).toContain('定时任务');

    // 定时任务统一走总经理：历史 agent_id 不再收窄花名册，规划时能看到所有员工。
    const planningCall = callProviderMock.mock.calls.find(
      ([, input]) => String((input as { systemPrompt?: string }).systemPrompt ?? '').includes('编排者'),
    );
    const planning = planningCall?.[1] as { systemPrompt?: string; userMessage?: string } | undefined;
    expect(`${planning?.systemPrompt ?? ''}\n${planning?.userMessage ?? ''}`).toContain('林研');
  });

  it('Python 正常时不重复执行，原样透传结果', async () => {
    stubFetch(200);
    const res = await request(app)
      .post(`/api/schedules/${scheduleId}/run`)
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-test')
      .send({})
      .expect(200);
    expect(res.body.data.run_id).toBe('python-run');
    expect(res.body.meta.fallback).toBeUndefined();
    expect(callProviderMock).not.toHaveBeenCalled();
    const record = JSON.parse(readFileSync(join(schedulesDir, `${scheduleId}.json`), 'utf8'));
    expect(record.runs).toHaveLength(0);
  });

  it('模型失败时记录 failed 运行与失败对话', async () => {
    stubFetch(503);
    callProviderMock.mockImplementation(async function* () {
      yield { type: 'error', code: 'PROVIDER_ERROR', message: 'upstream down' };
    });
    const res = await request(app)
      .post(`/api/schedules/${scheduleId}/run`)
      .set('Authorization', 'Bearer co1')
      .set('X-MiMo-Key', 'sk-test')
      .send({})
      .expect(200);
    expect(res.body.data.status).toBe('failed');
    expect(res.body.data.error).toContain('upstream down');
    const record = JSON.parse(readFileSync(join(schedulesDir, `${scheduleId}.json`), 'utf8'));
    expect(record.runs[0].status).toBe('failed');
  });

  it('任务不属于当前公司时拒绝执行', async () => {
    stubFetch(503);
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co2', name: '公司二' } });
      if (url.endsWith(`/api/schedules/${scheduleId}/run`)) return Response.json({ detail: { error: 'EXECUTOR_UNAVAILABLE' } }, { status: 503 });
      if (url.endsWith('/api/schedules')) return Response.json({ data: [{ ...scheduleRecord, company_id: 'co1' }] });
      if (url.includes('/roster')) return Response.json({ items: [{ id: 'e1', name: '陈明远', title: 'CEO 助理', status: 'idle', tools: [] }] });
      return Response.json({ items: [] });
    }));
    const res = await request(app)
      .post(`/api/schedules/${scheduleId}/run`)
      .set('Authorization', 'Bearer co2')
      .set('X-MiMo-Key', 'sk-test')
      .send({})
      .expect(404);
    expect(res.body.error.code).toBe('SCHEDULE_NOT_FOUND');
  });

  it('流式执行：start 建对话，finish 按对话真实结果写运行记录', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
      if (url.endsWith('/api/schedules')) return Response.json({ data: [scheduleRecord] });
      return Response.json({ items: [] });
    }));
    const started = await request(app)
      .post(`/api/schedules/${scheduleId}/start`)
      .set('Authorization', 'Bearer co1')
      .send({})
      .expect(200);
    const conversationId = started.body.data.conversation_id;
    expect(started.body.data.task_description).toBe('生成每日市场简报');
    const { getXuebanConversation, appendXuebanMessage } = await import('../storage/xueban-chat');
    expect(getXuebanConversation(conversationId, 'co1')?.title).toContain('定时任务');

    appendXuebanMessage(conversationId, { sender_kind: 'ceo', sender_name: '总经理', channel: 'chat', content: '简报已完成' });
    const finished = await request(app)
      .post(`/api/schedules/${scheduleId}/finish`)
      .set('Authorization', 'Bearer co1')
      .send({ conversation_id: conversationId })
      .expect(200);
    expect(finished.body.data.status).toBe('succeeded');
    expect(finished.body.data.output_summary).toContain('简报已完成');
    const record = JSON.parse(readFileSync(join(schedulesDir, `${scheduleId}.json`), 'utf8'));
    expect(record.runs[0]).toMatchObject({ status: 'succeeded', conversation_id: conversationId });
  });

  it('流式执行：没有最终答复时记录 failed', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
      if (url.endsWith('/api/schedules')) return Response.json({ data: [scheduleRecord] });
      return Response.json({ items: [] });
    }));
    const started = await request(app)
      .post(`/api/schedules/${scheduleId}/start`)
      .set('Authorization', 'Bearer co1')
      .send({})
      .expect(200);
    const finished = await request(app)
      .post(`/api/schedules/${scheduleId}/finish`)
      .set('Authorization', 'Bearer co1')
      .send({ conversation_id: started.body.data.conversation_id })
      .expect(200);
    expect(finished.body.data.status).toBe('failed');
  });
});
