/**
 * routes/__tests__/teams-dag-write-validate.test.ts — D1 (Node 侧写时 DAG 校验)
 *
 * 验证 PUT /api/teams/:id/dag 在落库前强制跑 validateDag:
 *   - 合法 team(members 全可解析 + edges 端点都是 member + 无环)→ 200,写盘。
 *   - 非法 team(edge 指向非 member,或有环)→ 422 + errors[],不写盘。
 *
 * 复用真实 router(动态 import,先 chdir 到 temp cwd,这样 team-yaml.ts /
 * agent-yaml.ts 的 module-level cwd 常量都指向临时目录),用 http.request
 * 打真实端口 —— 与 artifacts.test.ts 同样的 no-supertest 风格,但用 vitest。
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

function writeTeam(dir: string, id: string, yamlBody: string): void {
  fs.writeFileSync(path.join(dir, `${id}.team.yaml`), yamlBody, 'utf-8');
}

const VALID_LAYOUT = {
  dag_layout: {
    mode: 'manual',
    nodes: [
      { id: 'alpha', x: 10, y: 20 },
      { id: 'beta', x: 30, y: 40 },
    ],
    viewport: { zoom: 1, pan_x: 0, pan_y: 0 },
  },
};

let server: http.Server;
let app: Express;
let port = 0;
let tmp = '';
let origCwd = '';
let teamsDir = '';

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-d1-'));
  // chdir BEFORE importing the router so the module-level cwd constants in
  // team-yaml.ts / agent-yaml.ts resolve into our temp tree.
  process.chdir(tmp);

  const agentsDir = path.join(tmp, '.xueban', 'agents');
  teamsDir = path.join(tmp, '.xueban', 'teams');
  fs.mkdirSync(agentsDir, { recursive: true });
  fs.mkdirSync(teamsDir, { recursive: true });
  writeAgent(agentsDir, 'alpha');
  writeAgent(agentsDir, 'beta');

  // valid team: two members, one sequential edge, no cycle
  writeTeam(teamsDir, 'good-team',
    [
      'id: good-team',
      'name: Good Team',
      'members:',
      '  - alpha',
      '  - beta',
      'edges:',
      '  - from: alpha',
      '    to: beta',
      '    kind: sequential',
      '',
    ].join('\n'),
  );

  // invalid team: edge "to" points at a non-member ("ghost")
  writeTeam(teamsDir, 'bad-edge-team',
    [
      'id: bad-edge-team',
      'name: Bad Edge Team',
      'members:',
      '  - alpha',
      '  - beta',
      'edges:',
      '  - from: alpha',
      '    to: ghost',
      '    kind: sequential',
      '  - from: alpha',
      '    to: beta',
      '    kind: sequential',
      '',
    ].join('\n'),
  );

  // invalid team: sequential cycle alpha → beta → alpha
  writeTeam(teamsDir, 'cycle-team',
    [
      'id: cycle-team',
      'name: Cycle Team',
      'members:',
      '  - alpha',
      '  - beta',
      'edges:',
      '  - from: alpha',
      '    to: beta',
      '    kind: sequential',
      '  - from: beta',
      '    to: alpha',
      '    kind: sequential',
      '',
    ].join('\n'),
  );

  const mod = await import('../teams');
  app = express();
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
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
});

describe('PUT /api/teams/:id/dag — 写时 DAG 校验 (D1)', () => {
  it('合法 team → 200 且 dag_layout 写盘(向后兼容)', async () => {
    const r = await req(port, 'PUT', '/api/teams/good-team/dag', VALID_LAYOUT);
    expect(r.status).toBe(200);
    expect(r.body?.data?.team_id).toBe('good-team');
    expect(r.body?.data?.dag_layout?.mode).toBe('manual');
    // file actually changed
    const onDisk = fs.readFileSync(path.join(teamsDir, 'good-team.team.yaml'), 'utf-8');
    expect(onDisk).toContain('dag_layout');
    expect(onDisk).toContain('manual');
  });

  it('edge 指向非 member → 422 + errors,不写盘', async () => {
    const before = fs.readFileSync(path.join(teamsDir, 'bad-edge-team.team.yaml'), 'utf-8');
    const r = await req(port, 'PUT', '/api/teams/bad-edge-team/dag', VALID_LAYOUT);
    expect(r.status).toBe(422);
    expect(r.body?.error?.code).toBe('DAG_INVALID');
    expect(Array.isArray(r.body?.error?.errors)).toBe(true);
    expect(r.body.error.errors.some((e: string) => e.includes('ghost'))).toBe(true);
    // file untouched — still no dag_layout
    const after = fs.readFileSync(path.join(teamsDir, 'bad-edge-team.team.yaml'), 'utf-8');
    expect(after).toBe(before);
  });

  it('sequential 环 → 422 + cycle error,不写盘', async () => {
    const before = fs.readFileSync(path.join(teamsDir, 'cycle-team.team.yaml'), 'utf-8');
    const r = await req(port, 'PUT', '/api/teams/cycle-team/dag', VALID_LAYOUT);
    expect(r.status).toBe(422);
    expect(r.body?.error?.code).toBe('DAG_INVALID');
    expect(r.body.error.errors.some((e: string) => /cycle/i.test(e))).toBe(true);
    const after = fs.readFileSync(path.join(teamsDir, 'cycle-team.team.yaml'), 'utf-8');
    expect(after).toBe(before);
  });

  it('保形校验仍先行:dag_layout 缺失 → 400(早于 DAG 校验)', async () => {
    const r = await req(port, 'PUT', '/api/teams/good-team/dag', {});
    expect(r.status).toBe(400);
    expect(r.body?.error?.code).toBe('INVALID_BODY');
  });
});
