/**
 * 记忆路由账号鉴权测试（2026-11-12）。
 *
 * 不走存储层手工传 id，而是走真实中间件：mock Python /auth/me，
 * 用不同登录 Token 访问路由，验证：
 *   - 无 Token → 401；
 *   - Token 解析出的账号才是归属；body 里伪造 owner_id 无效；
 *   - A/B 两个登录身份互相读不到；
 *   - 登出后（/auth/me 401）旧 Token 立即失效，没有缓存窗口。
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
let router: typeof import('../routes/memory-handoff').default;
const originalVitest = process.env.VITEST;

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/memory/handoff', router);
  return a;
}

function stubIdentity() {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    const auth = String((init?.headers as Record<string, string> | undefined)?.Authorization ?? '');
    if (auth === 'Bearer token-a') return Response.json({ user: { id: 'account-a' }, company: { id: 'co-a', name: 'A' } });
    if (auth === 'Bearer token-b') return Response.json({ user: { id: 'account-b' }, company: { id: 'co-b', name: 'B' } });
    return new Response('unauthorized', { status: 401 });
  }));
}

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-memory-routes-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  router = (await import('../routes/memory-handoff')).default;
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

describe('记忆路由鉴权与归属', () => {
  it('没有登录 Token 时 401', async () => {
    delete process.env.VITEST;
    const res = await request(app()).get('/api/memory/handoff/stats');
    expect(res.status).toBe(401);
    process.env.VITEST = originalVitest ?? '1';
  });

  it('body 伪造 owner_id 不生效：归属只由 Token 决定', async () => {
    const write = await request(app())
      .post('/api/memory/handoff/profiles/writeback')
      .set('Authorization', 'Bearer token-a')
      .send({ owner_id: 'account-b', subject_type: 'student', subject_id: 'me', key: 'level', value: 'A 的值' });
    expect(write.status).toBe(200);

    const asA = await request(app())
      .get('/api/memory/handoff/profiles/me?subject_type=student')
      .set('Authorization', 'Bearer token-a');
    const asB = await request(app())
      .get('/api/memory/handoff/profiles/me?subject_type=student')
      .set('Authorization', 'Bearer token-b');
    expect(asA.body.profile[0]?.value).toBe('A 的值');
    expect(asB.body.profile).toEqual([]);
  });

  it('A/B 两个登录身份的交接包互相不可见', async () => {
    const created = await request(app())
      .post('/api/memory/handoff')
      .set('Authorization', 'Bearer token-a')
      .send({ shift_id: 'route-1', from_agent: 'A', to_agent: 'B' });
    expect(created.status).toBe(201);
    const id = created.body.handoff.id;

    const bRead = await request(app())
      .get(`/api/memory/handoff/${id}`)
      .set('Authorization', 'Bearer token-b');
    expect(bRead.status).toBe(404);

    const listB = await request(app())
      .get('/api/memory/handoff')
      .set('Authorization', 'Bearer token-b');
    expect(listB.body.some((x: { id: string }) => x.id === id)).toBe(false);
  });

  it('登出后旧 Token 立即失效（不做账号缓存）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('unauthorized', { status: 401 })));
    const res = await request(app())
      .get('/api/memory/handoff/stats')
      .set('Authorization', 'Bearer token-a');
    expect(res.status).toBe(401);
  });
});
