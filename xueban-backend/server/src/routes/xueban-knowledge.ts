import { Router, type Request, type Response as ExpressResponse } from 'express';
import express from 'express';
import { PYTHON_BACKEND_URL } from '../python-backend';
import { companyOwnsKnowledgePack, linkKnowledgePack, listKnowledgePackIds, unlinkKnowledgePack } from '../storage/xueban-knowledge';
import { xuebanChatCompany, XuebanChatError } from '../services/xueban-chat';
import { embeddingConfig, embeddingHeaders, verifyEmbedding } from '../lib/embedding';
import { callProvider, isProviderId, type ProviderId } from '../transport/api-clients';
import { resolveProviderKey } from '../transport/byokKey';

const router = Router();
const UPLOAD_LIMIT = '64mb';

async function python(path: string, init: RequestInit = {}, timeoutMs = 120_000, extraHeaders: Record<string, string> = {}): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${PYTHON_BACKEND_URL}${path}`, {
      ...init,
      headers: { ...extraHeaders, ...(init.headers ?? {}) },
      signal: controller.signal,
    });
  } catch {
    throw new XuebanChatError(503, 'KNOWLEDGE_BACKEND_UNAVAILABLE', '知识库服务暂时不可用');
  } finally {
    clearTimeout(timer);
  }
}

async function forward(res: ExpressResponse, response: Response): Promise<void> {
  const text = await response.text();
  res.status(response.status).type(response.headers.get('content-type') || 'application/json').send(text);
}

async function ownedPack(req: Request, packId: string): Promise<{ id: string; name: string }> {
  const company = await xuebanChatCompany(req.headers.authorization || '');
  if (!companyOwnsKnowledgePack(company.id, packId)) {
    throw new XuebanChatError(404, 'KNOWLEDGE_PACK_NOT_FOUND', '知识库不存在或不属于当前公司');
  }
  return company;
}

router.post('/embedding/test', (req, res) => {
  void (async () => {
    await xuebanChatCompany(req.headers.authorization || '');
    const config = embeddingConfig(req.headers);
    if (!config) throw new XuebanChatError(422, 'EMBEDDING_CONFIG_REQUIRED', '请填写有效的 Embedding Base URL、模型和 Key');
    res.json({ data: await verifyEmbedding(config) });
  })().catch(error => fail(res, error));
});

router.get('/packs', (req, res) => {
  void (async () => {
    const company = await xuebanChatCompany(req.headers.authorization || '');
    const packs = [];
    for (const packId of listKnowledgePackIds(company.id)) {
      const [packResponse, sourceResponse] = await Promise.all([
        python(`/api/memory-bank/packs/${encodeURIComponent(packId)}`),
        python(`/api/memory-bank/packs/${encodeURIComponent(packId)}/sources`),
      ]);
      if (!packResponse.ok || !sourceResponse.ok) continue;
      const packBody = await packResponse.json() as { data?: Record<string, unknown> };
      const sourceBody = await sourceResponse.json() as { data?: { sources?: unknown[] } };
      packs.push({ ...packBody.data, sources: sourceBody.data?.sources ?? [] });
    }
    res.json({ data: { packs } });
  })().catch(error => fail(res, error));
});

router.post('/packs', (req, res) => {
  void (async () => {
    const company = await xuebanChatCompany(req.headers.authorization || '');
    const name = String(req.body?.name || '').trim();
    if (!name) throw new XuebanChatError(422, 'KNOWLEDGE_NAME_REQUIRED', '请输入知识库名称');
    const response = await python('/api/memory-bank/packs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name.slice(0, 100), description: String(req.body?.description || '').slice(0, 500),
        metadata: { company_id: company.id },
      }),
    });
    const body = await response.json() as { data?: { pack_id?: string } };
    const packId = body.data?.pack_id;
    if (!response.ok || !packId) {
      res.status(response.status).json(body);
      return;
    }
    linkKnowledgePack(company.id, packId);
    res.status(201).json(body);
  })().catch(error => fail(res, error));
});

router.patch('/packs/:id', (req, res) => {
  void (async () => {
    await ownedPack(req, req.params.id);
    const updates: { name?: string; description?: string } = {};
    if (req.body?.name !== undefined) {
      const name = String(req.body.name).trim();
      if (!name) throw new XuebanChatError(422, 'KNOWLEDGE_NAME_REQUIRED', '请输入知识库名称');
      updates.name = name.slice(0, 100);
    }
    if (req.body?.description !== undefined) {
      updates.description = String(req.body.description).slice(0, 500);
    }
    if (updates.name === undefined && updates.description === undefined) {
      throw new XuebanChatError(422, 'KNOWLEDGE_UPDATE_REQUIRED', '请提供要修改的知识库名称或描述');
    }
    const response = await python(`/api/memory-bank/packs/${encodeURIComponent(req.params.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates),
    });
    await forward(res, response);
  })().catch(error => fail(res, error));
});

router.delete('/packs/:id', (req, res) => {
  void (async () => {
    const company = await ownedPack(req, req.params.id);
    const response = await python(`/api/memory-bank/packs/${encodeURIComponent(req.params.id)}`, { method: 'DELETE' });
    if (response.ok) unlinkKnowledgePack(company.id, req.params.id);
    await forward(res, response);
  })().catch(error => fail(res, error));
});

router.delete('/packs/:id/sources/:sourceId', (req, res) => {
  void (async () => {
    await ownedPack(req, req.params.id);
    const response = await python(
      `/api/memory-bank/packs/${encodeURIComponent(req.params.id)}/sources/${encodeURIComponent(req.params.sourceId)}`,
      { method: 'DELETE' },
    );
    await forward(res, response);
  })().catch(error => fail(res, error));
});

router.post('/packs/:id/files', express.raw({ type: 'multipart/form-data', limit: UPLOAD_LIMIT }), (req, res) => {
  void (async () => {
    await ownedPack(req, req.params.id);
    if (!Buffer.isBuffer(req.body) || req.body.length === 0) {
      throw new XuebanChatError(422, 'KNOWLEDGE_FILE_REQUIRED', '请选择要上传的文件');
    }
    const response = await python(`/api/memory-bank/packs/${encodeURIComponent(req.params.id)}/files`, {
      method: 'POST',
      headers: {
        'Content-Type': String(req.headers['content-type'] || ''),
        'Content-Length': String(req.body.length),
      },
      body: req.body,
    }, 120_000, embeddingHeaders(req.headers));
    await forward(res, response);
  })().catch(error => fail(res, error));
});

router.post('/packs/:id/query', (req, res) => {
  void (async () => {
    await ownedPack(req, req.params.id);
    const query = String(req.body?.query || '').trim();
    if (!query) throw new XuebanChatError(422, 'KNOWLEDGE_QUERY_REQUIRED', '请输入要检索的内容');
    const response = await python(`/api/memory-bank/packs/${encodeURIComponent(req.params.id)}/query`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, top_k: Math.min(10, Math.max(1, Number(req.body?.top_k) || 5)) }),
    }, 120_000, embeddingHeaders(req.headers));
    await forward(res, response);
  })().catch(error => fail(res, error));
});

router.post('/extract', express.json({ limit: '24mb' }), (req, res) => {
  void (async () => {
    await xuebanChatCompany(req.headers.authorization || '');
    const dataUrl = String(req.body?.data_url || '');
    if (!dataUrl.startsWith('data:image/')) {
      throw new XuebanChatError(422, 'IMAGE_REQUIRED', '请上传图片后再提取资料');
    }
    const provider = String(req.body?.provider || 'deepseek') as ProviderId;
    if (!isProviderId(provider)) {
      throw new XuebanChatError(422, 'PROVIDER_NOT_SUPPORTED', `不支持的模型供应商：${provider}`);
    }
    const apiKey = resolveProviderKey(provider, req.headers as Record<string, string | string[] | undefined>);
    const hint = String(req.body?.hint || '').slice(0, 2000);
    const systemPrompt = [
      '你是课程资料整理助手。请把图片里的文字、题目、板书或表格整理成可检索的学习资料。',
      '要求：1) 保留原文关键信息与数字，不要编造；2) 用 Markdown 输出，先给一句摘要，再给要点列表；',
      '3) 图片看不清或信息不足时明确写出来，不要猜测。',
    ].join('\n');
    let text = '';
    let failure: { code?: string; message: string } | null = null;
    for await (const chunk of callProvider(provider, {
      systemPrompt,
      userMessage: hint || '请把这张图片整理成课程资料。',
      attachments: [{ kind: 'image', data_url: dataUrl }],
      model: String(req.body?.model || 'deepseek-flash'),
      max_tokens: 2000,
      temperature: 0.2,
      api_key: apiKey,
    })) {
      if (chunk.type === 'text-delta') text += chunk.text;
      if (chunk.type === 'error') { failure = { code: chunk.code, message: chunk.message }; break; }
    }
    if (failure) {
      const status = failure.code === 'NO_API_KEY' ? 401 : 502;
      const raw = failure.message || '图片识别失败';
      const message = /insufficient|balance|402|余额/i.test(raw)
        ? '模型账户余额不足，暂时无法识别图片；请检查密钥或账户余额后重试'
        : raw;
      throw new XuebanChatError(status, failure.code || 'LLM_UPSTREAM_ERROR', message);
    }
    if (!text.trim()) {
      throw new XuebanChatError(502, 'EMPTY_EXTRACTION', '模型没有返回可用内容，请换一张更清晰的图片重试');
    }
    res.json({ data: { text: text.trim(), provider, model: String(req.body?.model || 'deepseek-flash') } });
  })().catch(error => fail(res, error));
});

router.post('/parse', express.raw({ type: 'multipart/form-data', limit: UPLOAD_LIMIT }), (req, res) => {
  void (async () => {
    await xuebanChatCompany(req.headers.authorization || '');
    if (!Buffer.isBuffer(req.body) || req.body.length === 0) {
      throw new XuebanChatError(422, 'KNOWLEDGE_FILE_REQUIRED', '请选择要解析的文件');
    }
    const response = await python('/api/memory-bank/parse', {
      method: 'POST',
      headers: {
        'Content-Type': String(req.headers['content-type'] || ''),
        'Content-Length': String(req.body.length),
      },
      body: req.body,
    }, 120_000);
    await forward(res, response);
  })().catch(error => fail(res, error));
});

function fail(res: ExpressResponse, error: unknown): void {
  const err = error instanceof XuebanChatError
    ? error
    : new XuebanChatError(500, 'KNOWLEDGE_INTERNAL', error instanceof Error ? error.message : String(error));
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

export default router;
