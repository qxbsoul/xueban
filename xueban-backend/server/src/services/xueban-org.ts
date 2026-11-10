import { PYTHON_BACKEND_URL } from '../python-backend';
import { validateXuebanOrgDraft, type XuebanOrgDraft } from '../lib/xueban-org-draft';
import { XuebanChatError } from './xueban-chat';
import { recordUsage } from '../storage/token-usage';

const DRAFT_CHAT_URL = 'https://api.deepseek.com/v1/chat/completions';
const DRAFT_MODEL = 'deepseek-flash';

const SYSTEM_PROMPT = `你是企业组织设计专家。根据业务描述设计可实际执行的 AI 公司组织。
只输出一个 JSON 对象，不要 Markdown、解释或代码围栏。结构必须为：
{"departments":[{"id":"dept_1","name":"...","charter":"..."}],"employees":[{"id":"emp_1","name":"...","title":"...","role":"...","system_prompt":"...","skills":["..."],"tools":["..."],"department_id":"dept_1","manager_id":null,"model_name":"deepseek-flash"}]}
要求：1–12 个部门、1–50 名员工；所有字段必填，manager_id 可为 null；姓名、部门和 id 不重复；department_id 与 manager_id 只能引用草案内 id；上级关系不得循环；role 使用稳定英文 snake_case；system_prompt 用中文写清岗位边界和交付责任；skills 与 tools 为具体字符串数组；model_name 固定为 deepseek-flash。`;

async function responseError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json() as { error?: { message?: string } | string; detail?: string };
    const message = typeof body.error === 'string' ? body.error : body.error?.message || body.detail;
    return message ? `${fallback}：${message.slice(0, 500)}` : fallback;
  } catch {
    return fallback;
  }
}

export async function generateXuebanOrgDraft(businessDescription: string, apiKey: string, companyId = ''): Promise<{ draft: XuebanOrgDraft; model: string; mode: 'live' }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90_000);
  try {
    const response = await fetch(DRAFT_CHAT_URL, {
      method: 'POST',
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: DRAFT_MODEL,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: `请为以下业务设计组织草案：\n${businessDescription}` },
        ],
        temperature: 0.2,
        max_tokens: 12000,
        response_format: { type: 'json_object' },
      }),
      signal: ctrl.signal,
    });
    if (!response.ok) {
      const message = await responseError(response, '组织草案生成失败');
      throw new XuebanChatError(response.status === 401 || response.status === 403 ? 401 : response.status === 429 ? 429 : 502, 'LLM_UPSTREAM_ERROR', message);
    }
    const body = await response.json() as { choices?: Array<{ message?: { content?: string } }>; usage?: { prompt_tokens?: number; completion_tokens?: number; prompt_tokens_details?: { cached_tokens?: number } } };
    if (body.usage) recordUsage({ company_id: companyId, session_id: 'org-draft', model: DRAFT_MODEL,
      input_tokens: body.usage.prompt_tokens, cached_input_tokens: body.usage.prompt_tokens_details?.cached_tokens, output_tokens: body.usage.completion_tokens });
    const content = body.choices?.[0]?.message?.content?.trim();
    if (!content) throw new XuebanChatError(502, 'LLM_EMPTY_RESPONSE', '模型没有返回组织草案');
    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch {
      throw new XuebanChatError(502, 'LLM_INVALID_JSON', '模型未返回严格 JSON，请重试');
    }
    return { draft: validateXuebanOrgDraft(parsed), model: DRAFT_MODEL, mode: 'live' };
  } catch (error) {
    if (error instanceof XuebanChatError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw new XuebanChatError(504, 'LLM_TIMEOUT', '组织草案生成超时，请重试');
    if (error instanceof Error && error.name === 'XuebanOrgDraftValidationError') {
      throw new XuebanChatError(502, 'LLM_INVALID_DRAFT', `模型草案校验失败：${error.message}`);
    }
    throw new XuebanChatError(502, 'LLM_UPSTREAM_ERROR', error instanceof Error ? error.message : '组织草案生成失败');
  } finally {
    clearTimeout(timer);
  }
}

export async function publishXuebanOrgDraft(companyId: string, draft: XuebanOrgDraft, authorization: string): Promise<unknown> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 30_000);
  try {
    const response = await fetch(`${PYTHON_BACKEND_URL}/api/xueban/company/${encodeURIComponent(companyId)}/org/publish`, {
      method: 'POST',
      headers: { Authorization: authorization, 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft }),
      signal: ctrl.signal,
    });
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    if (!response.ok) {
      throw new XuebanChatError(response.status, 'ORG_PUBLISH_FAILED', body?.detail || '组织发布失败，未写入任何草案内容');
    }
    return body;
  } catch (error) {
    if (error instanceof XuebanChatError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw new XuebanChatError(504, 'ORG_PUBLISH_TIMEOUT', '发布请求超时，请刷新组织确认实际状态后重试');
    throw new XuebanChatError(503, 'PYTHON_BACKEND_UNAVAILABLE', '公司业务后端暂时不可用，草案未发布');
  } finally {
    clearTimeout(timer);
  }
}
