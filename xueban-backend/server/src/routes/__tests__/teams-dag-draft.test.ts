/**
 * routes/__tests__/teams-dag-draft.test.ts — POST /api/teams/dag-draft
 *
 * 自然语言 → DAG 草稿（不落库）。LLM 走 vi.mock 的 callProvider（无网络）：
 *   - 400：prompt 缺失/空
 *   - 502：无任何 BYOK key（LLM_NO_KEY）/ LLM 报错（LLM_FAILED）
 *   - 200：合法草稿直出
 *   - 422 重试：首稿成环 → 回喂错误重试 → 二稿合法 → 200（callProvider × 2）
 *   - 422：两稿都不合法 → DAG_DRAFT_INVALID + errors
 *
 * 与 teams-dag-write-validate.test.ts 同风格：chdir 到 temp cwd（agent-yaml 的
 * module-level cwd 常量指向空目录）+ http.request 打真实端口。
 * PYTHON_BACKEND_URL 指到不可达端口，roster 解析确定性走兜底路径。
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import express, { type Express } from 'express';
import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';
import type { ProviderChunk } from '../../transport/api-clients';
import { PROVIDER_ENV_VAR } from '../../transport/api-clients';

const { callProviderMock } = vi.hoisted(() => ({ callProviderMock: vi.fn() }));

vi.mock('../../transport/api-clients', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../transport/api-clients')>();
  return { ...orig, callProvider: (...args: unknown[]) => callProviderMock(...args) };
});

interface HttpResp {
  status: number;
  body: any;
}

function post(
  port: number,
  urlPath: string,
  body?: Record<string, unknown>,
  headers: Record<string, string> = {},
): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : undefined;
    const h: Record<string, string | number> = { ...headers };
    if (data) {
      h['Content-Type'] = 'application/json';
      h['Content-Length'] = Buffer.byteLength(data);
    }
    const r = http.request({ host: '127.0.0.1', port, method: 'POST', path: urlPath, headers: h }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8');
        let parsed: unknown = null;
        try { parsed = JSON.parse(text); } catch { parsed = text; }
        resolve({ status: res.statusCode ?? 0, body: parsed });
      });
    });
    r.on('error', reject);
    if (data) r.write(data);
    r.end();
  });
}

const genText = (text: string) =>
  async function* (): AsyncGenerator<ProviderChunk> {
    yield { type: 'text-delta', text };
    yield { type: 'end' };
  };

const VALID_DRAFT = {
  nodes: [
    { id: 'n1', agentId: 'alpha', name: 'Alpha' },
    { id: 'n2', agentId: 'beta', name: 'Beta' },
  ],
  edges: [{ source: 'n1', target: 'n2', mode: 'approve' }],
};

const CYCLIC_DRAFT = {
  nodes: [
    { id: 'n1', agentId: 'alpha' },
    { id: 'n2', agentId: 'beta' },
  ],
  edges: [
    { source: 'n1', target: 'n2', mode: 'direct' },
    { source: 'n2', target: 'n1', mode: 'direct' },
  ],
};

const BYOK_HEADERS = { 'x-llm-provider': 'zhipu', 'x-llm-key': 'sk-test' };

let server: http.Server;
let port = 0;
let tmp = '';
let origCwd = '';
let savedEnv: Record<string, string | undefined> = {};

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-dag-draft-'));
  // chdir BEFORE importing the router：agent-yaml / team-yaml 的 module-level
  // cwd 常量指向空目录，roster 兜底为裸 id；settings 也读不到 byok 配置。
  process.chdir(tmp);
  fs.mkdirSync(path.join(tmp, '.xueban', 'agents'), { recursive: true });

  // roster 的 Python fetch 不可达 → 确定性兜底；LLM 已被 mock，不受影响。
  process.env.PYTHON_BACKEND_URL = 'http://127.0.0.1:9';
  // 清掉所有 provider 环境变量，无 key 用例才确定（开发机可能全局配了 key）。
  for (const envName of Object.values(PROVIDER_ENV_VAR)) {
    savedEnv[envName] = process.env[envName];
    delete process.env[envName];
  }

  const mod = await import('../teams');
  const app: Express = express();
  app.use(express.json());
  app.use('/api/teams', mod.default);
  server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  port = typeof addr === 'object' && addr ? addr.port : 0;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
  process.chdir(origCwd);
  delete process.env.PYTHON_BACKEND_URL;
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
});

describe('POST /api/teams/dag-draft', () => {
  it('prompt 缺失/空 → 400 INVALID_BODY', async () => {
    const r1 = await post(port, '/api/teams/dag-draft', {});
    expect(r1.status).toBe(400);
    expect(r1.body?.error?.code).toBe('INVALID_BODY');
    const r2 = await post(port, '/api/teams/dag-draft', { prompt: '   ' });
    expect(r2.status).toBe(400);
  });

  it('无任何 BYOK key → 502 LLM_NO_KEY', async () => {
    const r = await post(port, '/api/teams/dag-draft', { prompt: '先需求后开发' });
    expect(r.status).toBe(502);
    expect(r.body?.error?.code).toBe('LLM_NO_KEY');
    expect(callProviderMock).not.toHaveBeenCalled();
  });

  it('合法草稿 → 200 + draft（不落库，roster 传了 agentIds 也只做提示）', async () => {
    callProviderMock.mockReset();
    callProviderMock.mockImplementation(genText(JSON.stringify(VALID_DRAFT)));
    const r = await post(
      port,
      '/api/teams/dag-draft',
      { prompt: 'alpha 做完给 beta 审批', agentIds: ['alpha', 'beta'] },
      BYOK_HEADERS,
    );
    expect(r.status).toBe(200);
    expect(r.body?.data?.draft?.nodes).toHaveLength(2);
    expect(r.body?.data?.draft?.edges?.[0]?.mode).toBe('approve');
    expect(callProviderMock).toHaveBeenCalledTimes(1);
    // provider 从 X-LLM-Provider 头选出
    expect(callProviderMock.mock.calls[0]![0]).toBe('zhipu');
  });

  it('首稿成环 → 回喂错误重试 → 二稿合法 → 200（LLM 调两次）', async () => {
    callProviderMock.mockReset();
    callProviderMock
      .mockImplementationOnce(genText(JSON.stringify(CYCLIC_DRAFT)))
      .mockImplementationOnce(genText(JSON.stringify(VALID_DRAFT)));
    const r = await post(
      port,
      '/api/teams/dag-draft',
      { prompt: '双向协作', agentIds: ['alpha', 'beta'] },
      BYOK_HEADERS,
    );
    expect(r.status).toBe(200);
    expect(callProviderMock).toHaveBeenCalledTimes(2);
    const retryPrompt = String(callProviderMock.mock.calls[1]![1].userMessage);
    expect(retryPrompt).toContain('cycle');
  });

  it('两稿都不合法 → 422 DAG_DRAFT_INVALID + errors', async () => {
    callProviderMock.mockReset();
    callProviderMock.mockImplementation(genText(JSON.stringify(CYCLIC_DRAFT)));
    const r = await post(
      port,
      '/api/teams/dag-draft',
      { prompt: 'x', agentIds: ['alpha', 'beta'] },
      BYOK_HEADERS,
    );
    expect(r.status).toBe(422);
    expect(r.body?.error?.code).toBe('DAG_DRAFT_INVALID');
    expect(Array.isArray(r.body?.error?.errors)).toBe(true);
    expect(r.body.error.errors.some((e: string) => /cycle/i.test(e))).toBe(true);
    expect(callProviderMock).toHaveBeenCalledTimes(2);
  });

  it('LLM 报错 → 502 LLM_FAILED（不重试）', async () => {
    callProviderMock.mockReset();
    callProviderMock.mockImplementation(async function* (): AsyncGenerator<ProviderChunk> {
      yield { type: 'error', message: 'upstream down', code: 'NETWORK_ERROR' };
    });
    const r = await post(port, '/api/teams/dag-draft', { prompt: 'x' }, BYOK_HEADERS);
    expect(r.status).toBe(502);
    expect(r.body?.error?.code).toBe('LLM_FAILED');
    expect(callProviderMock).toHaveBeenCalledTimes(1);
  });

  it('LLM 输出非 JSON → 重试一次仍不行 → 422', async () => {
    callProviderMock.mockReset();
    callProviderMock.mockImplementation(genText('我不会输出 JSON'));
    const r = await post(port, '/api/teams/dag-draft', { prompt: 'x' }, BYOK_HEADERS);
    expect(r.status).toBe(422);
    expect(r.body?.error?.code).toBe('DAG_DRAFT_INVALID');
    expect(callProviderMock).toHaveBeenCalledTimes(2);
  });
});
