/**
 * 学生记忆管理路由测试（2026-11-12）：鉴权、账号隔离、伪造 owner_id。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let store: typeof import('../storage/student-memory');
let router: typeof import('../routes/student-memory').default;
const originalVitest = process.env.VITEST;

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/student-memory', router);
  return a;
}

function stubIdentity() {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    const auth = String((init?.headers as Record<string, string> | undefined)?.Authorization ?? '');
    if (auth === 'Bearer token-a') return Response.json({ user: { id: 'account-a' }, company: { id: 'co-a' } });
    if (auth === 'Bearer token-b') return Response.json({ user: { id: 'account-b' }, company: { id: 'co-b' } });
    return new Response('unauthorized', { status: 401 });
  }));
}

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-student-memory-routes-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  store = await import('../storage/student-memory');
  router = (await import('../routes/student-memory')).default;
  sqlite.getDb();
  stubIdentity();
});

afterEach(() => {
  vi.unstubAllGlobals();
  stubIdentity();
});

afterAll(() => {
  vi.unstubAllGlobals();
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
  if (originalVitest === undefined) delete process.env.VITEST;
  else process.env.VITEST = originalVitest;
});

describe('学生记忆路由', () => {
  it('无 Token 401', async () => {
    delete process.env.VITEST;
    const res = await request(app()).get('/api/student-memory/memories');
    expect(res.status).toBe(401);
    process.env.VITEST = originalVitest ?? '1';
  });

  it('A 的记忆 B 读不到、删不掉，query 伪造 owner_id 无效', async () => {
    const candidate = store.createCandidate({
      owner_id: 'account-a', category: 'preference', memory_key: 'teaching.explanation_style',
      content: '学生希望先讲思路。', confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: 'm-1', quote: '先讲思路' }],
    });
    const accepted = store.acceptCandidate('account-a', candidate.id, { changedBy: 'system' });
    const id = accepted.memory!.id;

    const listA = await request(app())
      .get('/api/student-memory/memories?owner_id=account-b')
      .set('Authorization', 'Bearer token-a');
    const listB = await request(app())
      .get('/api/student-memory/memories?owner_id=account-a')
      .set('Authorization', 'Bearer token-b');
    expect(listA.body.some((m: { id: string }) => m.id === id)).toBe(true);
    expect(listB.body.some((m: { id: string }) => m.id === id)).toBe(false);

    const detailB = await request(app())
      .get(`/api/student-memory/memories/${id}`)
      .set('Authorization', 'Bearer token-b');
    expect(detailB.status).toBe(404);

    const delB = await request(app())
      .delete(`/api/student-memory/memories/${id}`)
      .set('Authorization', 'Bearer token-b');
    expect(delB.status).toBe(404);
  });

  it('学生可以纠正自己的记忆并保留版本', async () => {
    const memory = store.listMemories({ owner_id: 'account-a' })[0];
    const res = await request(app())
      .post(`/api/student-memory/memories/${memory.id}/correct`)
      .set('Authorization', 'Bearer token-a')
      .send({ content: '学生希望先讲思路，再给提示。', reason: '学生纠正' });
    expect(res.status).toBe(200);
    expect(res.body.version).toBe(2);

    const detail = await request(app())
      .get(`/api/student-memory/memories/${memory.id}`)
      .set('Authorization', 'Bearer token-a');
    expect(detail.body.versions).toHaveLength(1);
    expect(detail.body.versions[0].snapshot.content).toBe(memory.content);
  });
});
