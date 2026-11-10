import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import xuebanOrgRouter from '../routes/xueban-org';
import { validateXuebanOrgDraft, XuebanOrgDraftValidationError } from '../lib/xueban-org-draft';

const app = express();
app.use(express.json());
app.use('/api/xueban-org', xuebanOrgRouter);

const draft = {
  departments: [{ id: 'dept_strategy', name: '策略部', charter: '负责市场策略与品牌规划' }],
  employees: [
    {
      id: 'emp_lead', name: '林策略', title: '策略负责人', role: 'strategy_lead',
      system_prompt: '负责理解学情并制定可交付的学习建议。',
      skills: ['市场研究'], tools: ['web_search'], department_id: 'dept_strategy', manager_id: null, model_name: 'mimo-v2.6-pro',
    },
    {
      id: 'emp_writer', name: '陈文案', title: '内容文案', role: 'copywriter',
      system_prompt: '根据策略交付真实可用的文案，不执行对外发布。',
      skills: ['文案写作'], tools: ['knowledge_search'], department_id: 'dept_strategy', manager_id: 'emp_lead', model_name: 'mimo-v2.6-pro',
    },
  ],
};
const fetchMock = vi.fn();

function authAnd(modelResponse?: Response): void {
  fetchMock.mockImplementation(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
    if (url.includes('api.deepseek.com')) return modelResponse ?? Response.json({ choices: [{ message: { content: JSON.stringify(draft) } }] });
    throw new Error(`unexpected fetch: ${url}`);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

describe('组织草案校验', () => {
  it('规范化有效草案且没有存储依赖', () => {
    const result = validateXuebanOrgDraft(draft);
    expect(result.employees[1].manager_id).toBe('emp_lead');
    expect(result.employees[0].skills).toEqual(['市场研究']);
  });

  it.each([
    ['重复员工', { ...draft, employees: [draft.employees[0], { ...draft.employees[1], id: 'emp_other', name: '林策略' }] }],
    ['缺失部门依赖', { ...draft, employees: [{ ...draft.employees[0], department_id: 'missing' }] }],
    ['循环上级', { ...draft, employees: [{ ...draft.employees[0], manager_id: 'emp_writer' }, draft.employees[1]] }],
    ['空员工', { ...draft, employees: [] }],
  ])('拒绝%s', (_name, value) => {
    expect(() => validateXuebanOrgDraft(value)).toThrow(XuebanOrgDraftValidationError);
  });
});

describe('POST /api/xueban-org/draft', () => {
  it('使用独立模型 key 调真实端点，只返回草案且不写组织', async () => {
    authAnd();
    const res = await request(app).post('/api/xueban-org/draft')
      .set('Authorization', 'Bearer company-token').set('X-DeepSeek-Key', 'sk-real-deepseek')
      .send({ company_id: 'co1', business_description: '为七年级数学提供学情诊断与个性化学习建议' })
      .expect(200);
    expect(res.body).toMatchObject({ company_id: 'co1', mode: 'live', model: 'deepseek-flash', draft });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const modelCall = fetchMock.mock.calls.find(call => String(call[0]).includes('api.deepseek.com'));
    expect(new Headers(modelCall?.[1]?.headers).get('Authorization')).toBe('Bearer sk-real-deepseek');
    expect(JSON.stringify(modelCall?.[1]?.body)).toContain('response_format');
    expect(fetchMock.mock.calls.some(call => /departments|employees|publish/.test(String(call[0])))).toBe(false);
  });

  it('没有真实模型 key 时 fail closed', async () => {
    authAnd();
    const res = await request(app).post('/api/xueban-org/draft')
      .set('Authorization', 'Bearer company-token')
      .send({ company_id: 'co1', business_description: '为七年级数学提供学情诊断与个性化学习建议' })
      .expect(401);
    expect(res.body.error.code).toBe('NO_API_KEY');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('模型返回非 JSON 或无效依赖时不生成伪草案', async () => {
    authAnd(Response.json({ choices: [{ message: { content: '不是 JSON' } }] }));
    const invalidJson = await request(app).post('/api/xueban-org/draft')
      .set('Authorization', 'Bearer company-token').set('X-DeepSeek-Key', 'sk-real-deepseek')
      .send({ company_id: 'co1', business_description: '为七年级数学提供学情诊断与个性化学习建议' })
      .expect(502);
    expect(invalidJson.body.error.code).toBe('LLM_INVALID_JSON');

    authAnd(Response.json({ choices: [{ message: { content: JSON.stringify({ ...draft, employees: [{ ...draft.employees[0], manager_id: 'ghost' }] }) } }] }));
    const invalidDraft = await request(app).post('/api/xueban-org/draft')
      .set('Authorization', 'Bearer company-token').set('X-DeepSeek-Key', 'sk-real-deepseek')
      .send({ company_id: 'co1', business_description: '为七年级数学提供学情诊断与个性化学习建议' })
      .expect(502);
    expect(invalidDraft.body.error.code).toBe('LLM_INVALID_DRAFT');
  });
});

describe('POST /api/xueban-org/publish', () => {
  it('校验后把同一份确认草案交给 Python 事务接口', async () => {
    fetchMock.mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '公司一' } });
      if (url.endsWith('/api/xueban/company/co1/org/publish')) return Response.json({ ok: true, created: { departments: 1, employees: 2 } });
      throw new Error(`unexpected fetch: ${url}`);
    });
    const res = await request(app).post('/api/xueban-org/publish')
      .set('Authorization', 'Bearer company-token').send({ company_id: 'co1', draft }).expect(200);
    expect(res.body.created).toEqual({ departments: 1, employees: 2 });
    const publishCall = fetchMock.mock.calls.find(call => String(call[0]).endsWith('/org/publish'));
    expect(JSON.parse(String(publishCall?.[1]?.body)).draft).toEqual(draft);
  });

  it('草案无效时不调用生产发布接口', async () => {
    authAnd();
    const res = await request(app).post('/api/xueban-org/publish')
      .set('Authorization', 'Bearer company-token')
      .send({ company_id: 'co1', draft: { ...draft, departments: [] } }).expect(422);
    expect(res.body.error.code).toBe('INVALID_ORG_DRAFT');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
