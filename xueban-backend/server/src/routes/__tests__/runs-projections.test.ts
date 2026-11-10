/**
 * runs-projections.test.ts — L6（GAP.md:13）路由集成测试。
 *
 * Run (from server/):  npx vitest run src/routes/__tests__/runs-projections.test.ts
 *
 * 验证 /api/runs/:runId/graph 与 /task-tree 对 Node daemon run 的行为：
 *   1. daemon run（经 registerGatewayRun 播种 + 时间线帧）→ 200 + Python 同形投影
 *   2. run 进行中（未 finish）→ 同样 200，status 'running'，ended_at 为 null
 *   3. 未知 runId → next() 落出 router（测试 app 未挂 proxyFallback → 404），
 *      证明不劫持 Python runtime 的 run
 *
 * Isolation: 与 run-sessions-stream-disposition.test.ts 同模式——run-sessions.ts
 * 模块加载有副作用（mkdir .xueban/*、磁盘 hydrate），先 chdir 到一次性
 * tmp 目录再动态 import，测完恢复 cwd 并清理。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import express from 'express';
import type { AddressInfo } from 'node:net';
import type http from 'node:http';

let runsRouter: typeof import('../runs').default;
let registerGatewayRun: typeof import('../run-sessions').registerGatewayRun;
let server: http.Server;
let base: string;
let origCwd: string;
let tmp: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-runs-proj-'));
  process.chdir(tmp);
  ({ default: runsRouter } = await import('../runs'));
  ({ registerGatewayRun } = await import('../run-sessions'));

  const app = express();
  app.use(express.json());
  app.use('/api/runs', runsRouter);
  // 测试 app 不挂 proxyFallback：落出 router 的请求到这里 → 404，
  // 正好断言「未命中 daemon run 时本路由不接管」。
  app.use((_req, res) => {
    res.status(404).json({ error: 'Not found' });
  });
  await new Promise<void>((resolve) => {
    server = app.listen(0, '127.0.0.1', resolve);
  });
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  process.chdir(origCwd);
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

/** 播种一条 gateway run（= chat 主路径 daemon run 的同款载体），附时间线帧。 */
function seedDaemonRun(finish: 'succeeded' | null): string {
  const handle = registerGatewayRun({ goal: '查一下氢能政策', conversation_id: 'conv-test' });
  handle.emit('message', { id: 'u1', kind: 'user_turn', turn_id: 't1', ts: 100, text: '查一下氢能政策' });
  handle.emit('message', {
    id: 'plan1',
    kind: 'plan_card',
    turn_id: 't1',
    ts: 110,
    title: '执行计划',
    tasks: [{ id: 'node:a', title: '检索', status: 'running' }],
  });
  handle.emit('message', { id: 'tc1', kind: 'tool_call', turn_id: 't1', ts: 120, name: 'web_search', args_summary: '{"q":"氢能"}', status: 'running' });
  handle.emit('message-patch', { id: 'tc1', op: 'tool_finalize', status: 'ok', duration_ms: 30 });
  if (finish) handle.finish(finish);
  return handle.session_id;
}

describe('GET /api/runs/:runId/graph（daemon run）', () => {
  it('已结束 run → 200 + RunGraph 同形投影', async () => {
    const id = seedDaemonRun('succeeded');
    const res = await fetch(`${base}/api/runs/${id}/graph`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as any; // eslint-disable-line @typescript-eslint/no-explicit-any
    expect(body.projection_kind).toBe('run_graph');
    expect(body.run_id).toBe(id);
    expect(body.status).toBe('succeeded');
    expect(body.nodes.map((n: { id: string }) => n.id)).toEqual(['node:a', 'tc1']);
    expect(body.nodes[1]).toMatchObject({ kind: 'tool', status: 'succeeded' });
    expect(body.edges).toHaveLength(1);
    expect(body.edges[0]).toMatchObject({ from_id: 'node:a', to_id: 'tc1', type: 'control_flow' });
  });

  it('进行中的 run → 200，status running，工具仍在 running', async () => {
    const handle = registerGatewayRun({ goal: '流式中', conversation_id: 'conv-test' });
    handle.emit('message', { id: 'tc9', kind: 'tool_call', turn_id: 't1', ts: 10, name: 'bash', args_summary: 'ls', status: 'running' });
    const res = await fetch(`${base}/api/runs/${handle.session_id}/graph`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as any; // eslint-disable-line @typescript-eslint/no-explicit-any
    expect(body.status).toBe('running');
    expect(body.nodes[0]).toMatchObject({ id: 'tc9', status: 'running' });
  });
});

describe('GET /api/runs/:runId/task-tree（daemon run）', () => {
  it('→ 200 + TaskTreeProjection 同形投影（run→task→step）', async () => {
    const id = seedDaemonRun('succeeded');
    const res = await fetch(`${base}/api/runs/${id}/task-tree`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as any; // eslint-disable-line @typescript-eslint/no-explicit-any
    expect(body.projection_kind).toBe('task_tree');
    expect(body.scope.run_id).toBe(id);
    const kinds = body.nodes.map((n: { entity_type: string }) => n.entity_type);
    expect(kinds).toEqual(['run', 'task', 'step']);
    expect(body.nodes[0]).toMatchObject({ id, label: '查一下氢能政策', status: 'succeeded' });
    expect(body.edges.every((e: { edge_type: string }) => e.edge_type === 'belongs_to_run')).toBe(true);
  });
});

describe('非 daemon run → 不接管（落出 router）', () => {
  it('未知 runId 的 graph / task-tree → 404（留给 proxyFallback 转 Python）', async () => {
    const g = await fetch(`${base}/api/runs/no-such-run/graph`);
    expect(g.status).toBe(404);
    const t = await fetch(`${base}/api/runs/no-such-run/task-tree`);
    expect(t.status).toBe(404);
  });
});
