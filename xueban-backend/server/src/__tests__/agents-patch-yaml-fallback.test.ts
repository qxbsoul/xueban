/**
 * routes/__tests__/agents-patch-yaml-fallback.test.ts — PATCH yaml 模板回退
 *
 * 复现并守护 2026-11-04「保存头像 Agent API error 404」：
 *   yaml 模板 agent（pm / dev / reader…语义 id）不在 Python 存储
 *   （.xueban/agents/agent-*.json）里。此前 PATCH /api/agents/:id 无
 *   Node 路由 → proxyFallback 直转 Python → 404 AGENT_NOT_FOUND。
 *
 * 修复语义（真实 router + 真实文件系统，no-supertest 风格，chdir 到 temp）：
 *   - PATCH Python 不可达（本测试环境无 Python 8000 mock → fetch 抛错）+
 *     id 是 yaml 模板 → 白名单字段（avatar_style / avatar_seed / claw_id /
 *     avatar_color / avatar_image）写 <id>.overrides.json sidecar → 200 +
 *     { data: record }，record 已贴上覆盖值。
 *   - name/soul 等设计时字段 → 422 YAML_TEMPLATE_READONLY（不写盘）。
 *   - GET /:id / GET / 合并语义贴回 overrides（保存后刷新不丢）。
 *   - 非 yaml 模板且 Python 不可达 → 404 AGENT_NOT_FOUND（原 404 语义不变）。
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express, { type Express } from 'express';
import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';

interface HttpResp {
  status: number;
  body: any;
}

function req(
  port: number,
  method: string,
  urlPath: string,
  body?: Record<string, unknown>,
): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : undefined;
    const headers: Record<string, string | number> = {};
    if (data) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(data);
    }
    const r = http.request(
      { host: '127.0.0.1', port, method, path: urlPath, headers },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf-8');
          let parsed: unknown = null;
          try { parsed = JSON.parse(text); } catch { parsed = text; }
          resolve({ status: res.statusCode ?? 0, body: parsed });
        });
      },
    );
    r.on('error', reject);
    if (data) r.write(data);
    r.end();
  });
}

function writeAgent(dir: string, id: string): void {
  fs.writeFileSync(
    path.join(dir, `${id}.agent.yaml`),
    [
      `id: ${id}`,
      `title: ${id.toUpperCase()}`,
      `persona: |`,
      `  You are ${id}.`,
      `model:`,
      `  id: test-model`,
      `tools:`,
      `  allow: []`,
      '',
    ].join('\n'),
    'utf-8',
  );
}

let server: http.Server;
let app: Express;
let port = 0;
let tmp = '';
let origCwd = '';
let agentsDir = '';

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-patch-'));
  // chdir BEFORE importing the router so module-level cwd constants in
  // agent-yaml.ts / agent-overrides.ts resolve into our temp tree.
  process.chdir(tmp);

  agentsDir = path.join(tmp, '.xueban', 'agents');
  fs.mkdirSync(agentsDir, { recursive: true });
  writeAgent(agentsDir, 'pm');
  writeAgent(agentsDir, 'dev');

  const mod = await import('../routes/agents');
  app = express();
  app.use(express.json());
  app.use('/api/agents', mod.default);
  server = app.listen(0);
  port = (server.address() as { port: number }).port;
}, 30_000);

afterAll(() => {
  process.chdir(origCwd);
  server?.close();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('PATCH /api/agents/:id — yaml 模板回退（保存头像 404 修复）', () => {
  it('yaml 模板 agent：头像字段 PATCH 成功并写 sidecar（复现场景回归）', async () => {
    const res = await req(port, 'PATCH', '/api/agents/pm', {
      avatar_style: 'fun-emoji',
      avatar_seed: 'seed-pm-1',
    });
    expect(res.status).toBe(200);
    const record = res.body?.data;
    expect(record?.agent_id).toBe('pm');
    expect(record?.avatar_style).toBe('fun-emoji');
    expect(record?.avatar_seed).toBe('seed-pm-1');

    const sidecar = JSON.parse(
      fs.readFileSync(path.join(agentsDir, 'pm.overrides.json'), 'utf-8'),
    );
    expect(sidecar.patch.avatar_style).toBe('fun-emoji');
    expect(sidecar.patch.avatar_seed).toBe('seed-pm-1');
  });

  it('GET /api/agents 列表合并贴回 overrides（保存后不丢）', async () => {
    const res = await req(port, 'GET', '/api/agents');
    expect(res.status).toBe(200);
    const pm = res.body?.data?.find((a: any) => a.agent_id === 'pm');
    expect(pm?.avatar_style).toBe('fun-emoji');
    expect(pm?.avatar_seed).toBe('seed-pm-1');
  });

  it('GET /api/agents/:id 单个也贴回 overrides', async () => {
    const res = await req(port, 'GET', '/api/agents/pm');
    expect(res.status).toBe(200);
    expect(res.body?.data?.avatar_style).toBe('fun-emoji');
  });

  it('"" 显式清空 → sidecar 存 null，record 回落模板原值', async () => {
    const res = await req(port, 'PATCH', '/api/agents/pm', {
      avatar_style: '',
      avatar_seed: '',
    });
    expect(res.status).toBe(200);
    expect(res.body?.data?.avatar_style).toBeNull();
    const sidecar = JSON.parse(
      fs.readFileSync(path.join(agentsDir, 'pm.overrides.json'), 'utf-8'),
    );
    expect(sidecar.patch.avatar_style).toBeNull();
  });

  it('name/soul 设计时字段 → 422 YAML_TEMPLATE_READONLY，不写盘', async () => {
    const res = await req(port, 'PATCH', '/api/agents/dev', { soul: '新灵魂' });
    expect(res.status).toBe(422);
    expect(res.body?.error?.code).toBe('YAML_TEMPLATE_READONLY');
    expect(fs.existsSync(path.join(agentsDir, 'dev.overrides.json'))).toBe(false);
  });

  it('非 yaml 模板且 Python 不可达 → 404 AGENT_NOT_FOUND（语义不变）', async () => {
    const res = await req(port, 'PATCH', '/api/agents/ghost-agent', {
      avatar_style: 'glass',
    });
    expect(res.status).toBe(404);
    expect(res.body?.error?.code).toBe('AGENT_NOT_FOUND');
  });
});
