// server/src/__tests__/conversations-move-project.test.ts
// GAP A3 回写（2026-08-23）— 会话归属项目写通路：
//   PATCH /api/conversations/by-group/:gid  { project_id }
//     - 已互链 conversation → 改 project_id（「移入项目」）
//     - project_id null/''  → 移回默认项目（getOrCreateProject('default')）
//     - 群尚无互链 conversation → find-or-create 落目标项目（201）
//     - 目标项目不存在 → 404 PROJECT_NOT_FOUND；缺字段 → 400
//   POST /api/run-sessions body.project_id — 自动创建 conversation 的目标项目
//     （这里只测校验分支：项目不存在 → 400 PROJECT_NOT_FOUND，不起 run）。
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let conversationsRouter: typeof import('../routes/conversations').default;
let runSessionsRouter: express.Router;
let sqlite: typeof import('../storage/sqlite');
let projectsStore: typeof import('../storage/projects');
let conversationsStore: typeof import('../storage/conversations');

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/conversations', conversationsRouter);
  a.use('/api/run-sessions', runSessionsRouter);
  return a;
}

beforeAll(async () => {
  // sqlite 数据文件落在 cwd/.xueban/ — 切到临时目录跑隔离库。
  // run-sessions.ts import 有 mkdir 副作用，同样要在 chdir 之后动态 import。
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-conv-move-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  projectsStore = await import('../storage/projects');
  conversationsStore = await import('../storage/conversations');
  conversationsRouter = (await import('../routes/conversations')).default;
  runSessionsRouter = (await import('../routes/run-sessions')).default;
  // 显式加时：全量并行（CI 2 核）下连续动态 import 五个模块偶尔拖过默认
  // hookTimeout 20s（真实失败 vs 环境慢无法区分）。60s 只放宽等待、不掩盖错误。
}, 60_000);

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('PATCH /api/conversations/by-group/:gid', () => {
  it('已互链 conversation → 改 project_id（移入项目），updated_at 前进', async () => {
    const from = projectsStore.createProject({ name: 'from', workspace_path: path.join(tmp, 'f') });
    const to = projectsStore.createProject({ name: 'to', workspace_path: path.join(tmp, 't') });
    const c = conversationsStore.createConversation(from.project_id, '会话', 'g-move-1');

    const res = await request(app())
      .patch('/api/conversations/by-group/g-move-1')
      .send({ project_id: to.project_id });
    expect(res.status).toBe(200);
    expect(res.body.conversation_id).toBe(c.conversation_id);
    expect(res.body.project_id).toBe(to.project_id);
    expect(res.body.group_id).toBe('g-move-1');
    expect(Date.parse(res.body.updated_at)).toBeGreaterThanOrEqual(Date.parse(c.updated_at));
    expect(conversationsStore.getConversation(c.conversation_id)!.project_id).toBe(to.project_id);
  });

  it('project_id = null → 移回默认项目（default 行自动就位）', async () => {
    const p = projectsStore.createProject({ name: 'p', workspace_path: path.join(tmp, 'p') });
    const c = conversationsStore.createConversation(p.project_id, undefined, 'g-move-2');

    const res = await request(app())
      .patch('/api/conversations/by-group/g-move-2')
      .send({ project_id: null });
    expect(res.status).toBe(200);
    expect(res.body.project_id).toBe('default');
    expect(conversationsStore.getConversation(c.conversation_id)!.project_id).toBe('default');
    expect(projectsStore.getProject('default')).toBeTruthy();
  });

  it('群尚无互链 conversation → find-or-create 落目标项目（201 + group_id 写入）', async () => {
    const p = projectsStore.createProject({ name: 'target', workspace_path: path.join(tmp, 'tg') });

    const res = await request(app())
      .patch('/api/conversations/by-group/g-move-new')
      .send({ project_id: p.project_id });
    expect(res.status).toBe(201);
    expect(res.body.project_id).toBe(p.project_id);
    expect(res.body.group_id).toBe('g-move-new');
    // 互链可反查（B1 getConversationByGroup）。
    expect(conversationsStore.getConversationByGroup('g-move-new')!.conversation_id).toBe(
      res.body.conversation_id,
    );
  });

  it('目标项目不存在 → 404 PROJECT_NOT_FOUND（不改库）', async () => {
    const p = projectsStore.createProject({ name: 'keep', workspace_path: path.join(tmp, 'k') });
    const c = conversationsStore.createConversation(p.project_id, undefined, 'g-move-404');

    const res = await request(app())
      .patch('/api/conversations/by-group/g-move-404')
      .send({ project_id: 'no-such-project' });
    expect(res.status).toBe(404);
    expect(res.body.error.code).toBe('PROJECT_NOT_FOUND');
    expect(conversationsStore.getConversation(c.conversation_id)!.project_id).toBe(p.project_id);
  });

  it('缺 project_id 字段 / 类型错误 → 400 INVALID_PROJECT_ID', async () => {
    const res1 = await request(app()).patch('/api/conversations/by-group/g-x').send({});
    expect(res1.status).toBe(400);
    expect(res1.body.error.code).toBe('INVALID_PROJECT_ID');

    const res2 = await request(app())
      .patch('/api/conversations/by-group/g-x')
      .send({ project_id: 42 });
    expect(res2.status).toBe(400);
    expect(res2.body.error.code).toBe('INVALID_PROJECT_ID');
  });
});

describe('POST /api/run-sessions body.project_id（GAP A3 回写）', () => {
  it('project_id 给了但项目不存在 → 400 PROJECT_NOT_FOUND（不起 run）', async () => {
    const res = await request(app())
      .post('/api/run-sessions')
      .send({ goal: 'hi', project_id: 'no-such-project' });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('PROJECT_NOT_FOUND');
  });

  it('project_id 类型错误 → 400 INVALID_PROJECT_ID', async () => {
    const res = await request(app())
      .post('/api/run-sessions')
      .send({ goal: 'hi', project_id: 42 });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('INVALID_PROJECT_ID');
  });
});
