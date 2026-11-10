import { Router, type Response as ExpressResponse } from 'express';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { XuebanChatError, xuebanChatCompany } from '../services/xueban-chat';
import { executeScheduledTask } from '../services/xueban-schedule';
import { PYTHON_BACKEND_URL } from '../python-backend';
import { modelConfig } from './xueban-chat';
import { appendXuebanMessage, createXuebanConversation, getXuebanConversation, listXuebanMessages } from '../storage/xueban-chat';
import type { ScheduledRunResult } from '../services/xueban-schedule';

const router = Router();

/** Python 定时任务记录所在目录（Node 的 cwd 是 server/，Python 的 cwd 是仓库根）。 */
function schedulesDir(): string {
  return process.env.XUEBAN_SCHEDULES_DIR
    ?? path.resolve(process.cwd(), '..', '.xueban', 'schedules');
}

interface ScheduleRunRecord {
  run_id: string;
  triggered_at: string;
  status: 'succeeded' | 'failed';
  conversation_id?: string;
  output_summary?: string;
  error?: string;
}

const toRunRecord = (result: ScheduledRunResult): ScheduleRunRecord => ({
  run_id: randomUUID().replace(/-/g, ''),
  triggered_at: new Date().toISOString(),
  status: result.status,
  conversation_id: result.conversationId,
  output_summary: result.outputSummary,
  ...(result.error ? { error: result.error } : {}),
});

/**
 * 把一次兜底执行写回 Python 的调度记录（JSON 文件）。
 * Python 每次读调度都会重新读文件，所以运行历史、已执行次数会立即生效。
 * 找不到文件时静默跳过：说明 Python 不在同一份数据目录，本次只返回运行结果。
 */
async function appendScheduleRun(scheduleId: string, run: ScheduleRunRecord): Promise<void> {
  const file = path.join(schedulesDir(), `${scheduleId}.json`);
  try {
    const raw = await readFile(file, 'utf8');
    const record = JSON.parse(raw) as { runs?: ScheduleRunRecord[]; start_at?: string | null; cron_expression?: string | null; completed?: boolean; next_run_time?: string | null };
    record.runs = [run, ...(record.runs ?? [])].slice(0, 20);
    if (record.start_at && !record.cron_expression) {
      record.completed = true;
      record.next_run_time = null;
    }
    const temp = `${file}.tmp-${process.pid}`;
    await writeFile(temp, JSON.stringify(record, null, 2), 'utf8');
    await rename(temp, file);
  } catch {
    // 数据目录不在本机同一路径时不阻断执行结果。
  }
}

function fail(res: ExpressResponse, error: unknown): void {
  const err = error instanceof XuebanChatError
    ? error
    : new XuebanChatError(500, 'SCHEDULE_RUN_FAILED', error instanceof Error ? error.message : String(error));
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

interface ScheduleDto {
  schedule_id: string;
  company_id: string;
  task_description: string;
  agent_id?: string | null;
  enabled?: boolean;
}

/** 从 Python 读一条属于当前公司的定时任务（Node 不存调度表）。 */
async function loadSchedule(authorization: string, companyId: string, scheduleId: string): Promise<ScheduleDto> {
  const listResponse = await fetch(`${PYTHON_BACKEND_URL}/api/schedules`, { headers: { authorization } });
  if (!listResponse.ok) throw new XuebanChatError(502, 'SCHEDULE_LIST_FAILED', '无法读取定时任务列表，请稍后重试');
  const listed = await listResponse.json() as { data?: ScheduleDto[] };
  const schedule = (listed.data ?? []).find(item => item.schedule_id === scheduleId && item.company_id === companyId);
  if (!schedule) throw new XuebanChatError(404, 'SCHEDULE_NOT_FOUND', '定时任务不存在或不属于当前公司');
  if (!schedule.task_description.trim()) throw new XuebanChatError(422, 'INVALID_SCHEDULE', '该任务没有描述，无法执行');
  return schedule;
}

/**
 * 立即执行的「流式版」第一步：只建对话，不跑模型。
 * 前端随后用正常的聊天流接口在这条对话里发消息，从而能看到总经理思考、
 * 员工流式输出、审批卡片等全部实时状态。
 */
router.post('/:scheduleId/start', (req, res) => {
  void (async () => {
    const scheduleId = String(req.params.scheduleId || '');
    if (!/^[A-Za-z0-9_-]+$/.test(scheduleId)) throw new XuebanChatError(400, 'INVALID_SCHEDULE_ID', '非法的任务 id');
    const company = await xuebanChatCompany(req.headers.authorization || '');
    const schedule = await loadSchedule(req.headers.authorization || '', company.id, scheduleId);
    const conversation = createXuebanConversation(company.id, 'single', `定时任务｜${schedule.task_description}`.slice(0, 40));
    res.json({ data: {
      conversation_id: conversation.id,
      task_description: schedule.task_description,
    } });
  })().catch(error => fail(res, error));
});

/** 流式执行的收尾：读取这条对话的真实结果，写回运行记录。 */
router.post('/:scheduleId/finish', (req, res) => {
  void (async () => {
    const scheduleId = String(req.params.scheduleId || '');
    if (!/^[A-Za-z0-9_-]+$/.test(scheduleId)) throw new XuebanChatError(400, 'INVALID_SCHEDULE_ID', '非法的任务 id');
    const authorization = req.headers.authorization || '';
    const company = await xuebanChatCompany(authorization);
    await loadSchedule(authorization, company.id, scheduleId);
    const conversationId = String(req.body?.conversation_id || '').trim();
    const conversation = conversationId ? getXuebanConversation(conversationId, company.id) : null;
    if (!conversation) throw new XuebanChatError(404, 'CONVERSATION_NOT_FOUND', '执行对话不存在');

    const messages = listXuebanMessages(conversation.id);
    const final = [...messages].reverse().find(message => message.sender_kind === 'ceo'
      && message.channel !== 'workflow' && !String(message.metadata_json ?? '').includes('"dag"'));
    let run: ScheduleRunRecord;
    if (final) {
      run = {
        run_id: randomUUID().replace(/-/g, ''),
        triggered_at: new Date().toISOString(),
        status: 'succeeded',
        conversation_id: conversation.id,
        output_summary: final.content.slice(0, 2000),
      };
    } else {
      const detail = messages.filter(message => message.sender_kind === 'agent' && message.status === 'failed')[0]?.content
        || '本轮没有产出最终成果';
      appendXuebanMessage(conversation.id, {
        sender_kind: 'system', sender_name: '总经理', channel: 'workflow', status: 'failed',
        content: `定时任务执行失败：${detail}`,
      });
      run = {
        run_id: randomUUID().replace(/-/g, ''),
        triggered_at: new Date().toISOString(),
        status: 'failed',
        conversation_id: conversation.id,
        error: detail.slice(0, 300),
        output_summary: `定时任务执行失败：${detail}`.slice(0, 2000),
      };
    }
    await appendScheduleRun(scheduleId, run);
    res.json({ data: run, meta: { streamed: true } });
  })().catch(error => fail(res, error));
});

/**
 * 立即执行定时任务。
 *
 * 正常路径仍然交给 Python（它负责运行历史与执行凭据）；如果 Python 是老实例、
 * 没有注册执行桥（EXECUTOR_UNAVAILABLE / 5xx），Node 直接用同一套 XUEBAN 对话链路兜底，
 * 并把运行记录写回调度文件，避免“任务永远跑不起来”。
 */
router.post('/:scheduleId/run', (req, res) => {
  void (async () => {
    const scheduleId = String(req.params.scheduleId || '');
    if (!/^[A-Za-z0-9_-]+$/.test(scheduleId)) throw new XuebanChatError(400, 'INVALID_SCHEDULE_ID', '非法的任务 id');
    const authorization = req.headers.authorization || '';
    const company = await xuebanChatCompany(authorization);

    const forwarded: Record<string, string> = { authorization };
    for (const header of ['x-deepseek-key', 'x-mimo-key', 'x-tavily-key', 'x-serpapi-key', 'x-github-token']) {
      const value = String(req.headers[header] ?? '').trim();
      if (value) forwarded[header] = value;
    }

    let upstream: globalThis.Response | null = null;
    try {
      upstream = await fetch(`${PYTHON_BACKEND_URL}/api/schedules/${encodeURIComponent(scheduleId)}/run`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', ...forwarded },
        body: '{}',
      });
    } catch {
      upstream = null;
    }
    if (upstream?.status === 200) {
      res.status(200).json(await upstream.json());
      return;
    }
    // 4xx（越权、找不到、参数错）原样返回，不做兜底。
    if (upstream && upstream.status >= 400 && upstream.status < 500) {
      const body = await upstream.text();
      res.status(upstream.status);
      res.setHeader('content-type', upstream.headers.get('content-type') ?? 'application/json');
      res.send(body);
      return;
    }

    const schedule = await loadSchedule(authorization, company.id, scheduleId);

    const result = await executeScheduledTask({
      companyId: company.id,
      description: schedule.task_description,
      // 定时任务统一走总经理调度，历史记录里的 agent_id 不再生效。
      authorization,
      config: modelConfig(req),
    });
    const run = toRunRecord(result);
    await appendScheduleRun(scheduleId, run);
    res.json({ data: run, meta: { fallback: true, reason: upstream ? `Python HTTP ${upstream.status}` : 'Python 不可达' } });
  })().catch(error => fail(res, error));
});

export default router;
