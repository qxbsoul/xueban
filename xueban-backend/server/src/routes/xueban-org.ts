import { Router, type Response } from 'express';
import { validateXuebanOrgDraft, XuebanOrgDraftValidationError } from '../lib/xueban-org-draft';
import { generateXuebanOrgDraft, publishXuebanOrgDraft } from '../services/xueban-org';
import { xuebanChatCompany, XuebanChatError } from '../services/xueban-chat';

const router = Router();

function fail(res: Response, error: unknown): void {
  if (error instanceof XuebanOrgDraftValidationError) {
    res.status(422).json({ error: { code: 'INVALID_ORG_DRAFT', message: error.message, issues: error.issues } });
    return;
  }
  const err = error instanceof XuebanChatError
    ? error
    : new XuebanChatError(500, 'ORG_INTERNAL_ERROR', error instanceof Error ? error.message : String(error));
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

async function requestCompany(req: { headers: { authorization?: string }; body?: unknown }): Promise<{ id: string; name: string }> {
  const company = await xuebanChatCompany(req.headers.authorization || '');
  const body = req.body && typeof req.body === 'object' ? req.body as Record<string, unknown> : {};
  const companyId = typeof body.company_id === 'string' ? body.company_id.trim() : '';
  if (!companyId) throw new XuebanChatError(422, 'COMPANY_REQUIRED', '请选择要建制的公司');
  if (companyId !== company.id) throw new XuebanChatError(403, 'COMPANY_MISMATCH', '公司归属与登录公司不一致');
  return company;
}

router.post('/draft', (req, res) => {
  void (async () => {
    const company = await requestCompany(req);
    const description = typeof req.body?.business_description === 'string' ? req.body.business_description.trim() : '';
    if (description.length < 10 || description.length > 10000) {
      throw new XuebanChatError(422, 'INVALID_BUSINESS_DESCRIPTION', '业务描述需为 10–10000 个字符');
    }
    const apiKey = (req.header('x-deepseek-key') || req.header('x-mimo-key') || '').trim();
    if (!apiKey) throw new XuebanChatError(401, 'NO_API_KEY', '请先在设置中配置真实模型 API Key');
    const result = await generateXuebanOrgDraft(description, apiKey, company.id);
    // 草案只存在本次 HTTP 响应中，绝不调用部门/员工写接口。
    res.json({ company_id: company.id, ...result });
  })().catch(error => fail(res, error));
});

router.post('/publish', (req, res) => {
  void (async () => {
    const company = await requestCompany(req);
    const draft = validateXuebanOrgDraft(req.body?.draft);
    const result = await publishXuebanOrgDraft(company.id, draft, req.headers.authorization || '');
    res.json(result);
  })().catch(error => fail(res, error));
});

export default router;
