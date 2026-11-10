import { Router, type Response } from 'express';
import { callProvider, isProviderId, PROVIDERS_NO_KEY, type ProviderChunk } from '../transport/api-clients';
import { resolveProviderBaseUrl, resolveProviderKey } from '../transport/byokKey';
import { xuebanChatCompany, XuebanChatError } from '../services/xueban-chat';
import { executeScheduledTask } from '../services/xueban-schedule';
import { modelConfig } from './xueban-chat';
import { ThinkingDeltaParser } from '../services/xueban-chat-stream';
import type { TokenUsage } from '../lib/conversation-types';
import { recordUsage } from '../storage/token-usage';

const router = Router();

function fail(res: Response, error: unknown): void {
  const err = error instanceof XuebanChatError
    ? error
    : new XuebanChatError(500, 'TASK_EXECUTION_INTERNAL', error instanceof Error ? error.message : String(error));
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

/**
 * Python 公司状态机的唯一模型出口。这里只做公司鉴权和真实模型补全，
 * 不创建/修改项目、任务、审批或账本。
 */
router.post('/tasks/complete', (req, res) => {
  void (async () => {
    const authorization = req.headers.authorization || '';
    const company = await xuebanChatCompany(authorization);
    const companyId = String(req.body?.company_id || '').trim();
    if (!companyId || companyId !== company.id) {
      throw new XuebanChatError(403, 'COMPANY_MISMATCH', '任务公司归属与登录公司不一致');
    }

    const provider = String(req.body?.provider || '');
    const model = String(req.body?.model || '').trim();
    const systemPrompt = String(req.body?.system || '');
    const userMessage = String(req.body?.user || '').trim();
    if (!isProviderId(provider) || !model || !userMessage) {
      throw new XuebanChatError(422, 'INVALID_TASK_COMPLETION', 'provider、model 和 user 必须有效');
    }
    const apiKey = resolveProviderKey(provider, req.headers);
    if (!apiKey && !PROVIDERS_NO_KEY.has(provider)) {
      throw new XuebanChatError(401, 'NO_API_KEY', '请先在设置中配置当前模型的 API Key');
    }

    const parser = new ThinkingDeltaParser('xueban-task');
    const usage: TokenUsage = {};
    let failure: Extract<ProviderChunk, { type: 'error' }> | undefined;
    for await (const chunk of callProvider(provider, {
      systemPrompt,
      userMessage,
      model,
      api_key: apiKey,
      base_url: resolveProviderBaseUrl(provider),
      max_tokens: 4096,
      temperature: 0.2,
    })) {
      if (chunk.type === 'text-delta') parser.push(chunk.text);
      if (chunk.type === 'usage') {
        Object.assign(usage, chunk.usage);
        recordUsage({ company_id: companyId, session_id: String(req.body?.task_id || ''), model,
          input_tokens: chunk.usage.input_tokens, cached_input_tokens: chunk.usage.cache_read_input_tokens, output_tokens: chunk.usage.output_tokens });
      }
      if (chunk.type === 'error') {
        failure = chunk;
        break;
      }
    }
    if (failure) {
      const status = failure.code === 'NO_API_KEY' ? 401 : failure.code === 'RATE_LIMITED' ? 429 : 502;
      throw new XuebanChatError(status, failure.code ?? 'LLM_UPSTREAM_ERROR', failure.message);
    }
    const text = parser.finish();
    if (!text) throw new XuebanChatError(502, 'LLM_EMPTY_RESPONSE', '模型没有返回有效内容');

    res.json({ text, thinking: parser.thinking, mode: 'live', model, usage });
  })().catch((error) => fail(res, error));
});

/** Python APScheduler 的真实执行入口；服务会话只用于校验公司归属。 */
router.post('/schedules/execute', (req, res) => {
  void (async () => {
    const authorization = req.headers.authorization || '';
    const company = await xuebanChatCompany(authorization);
    const companyId = String(req.body?.company_id || '').trim();
    const description = String(req.body?.task_description || '').trim();
    if (!companyId || companyId !== company.id) throw new XuebanChatError(403, 'COMPANY_MISMATCH', '定时任务公司归属与执行身份不一致');
    if (!description) throw new XuebanChatError(422, 'INVALID_SCHEDULE', '定时任务描述不能为空');
    const result = await executeScheduledTask({
      companyId,
      description,
      // 历史调度记录可能还带 agent_id；定时任务统一由总经理调度，这里不再按岗位过滤。
      authorization,
      config: modelConfig(req),
    });
    res.json({ data: {
      conversation_id: result.conversationId,
      status: result.status,
      ...(result.error ? { error: result.error } : {}),
      output_summary: result.outputSummary,
      message_count: result.messageCount,
    } });
  })().catch(error => fail(res, error));
});

export default router;
