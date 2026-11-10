// server/src/__tests__/projects-with-sessions.test.ts
// GAP A3（2026-08-22）— GET /api/projects?withSessions=1：项目 + 关联会话列表。
// 数据模型复用 conversations.project_id（15.16 既有外键），session.group_id
// 互链 Python 群/DM，前端 NeoRail 按它把会话/群聊归到项目分组。
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let projectsRouter: typeof import('../routes/projects').default;
let sqlite: typeof import('../storage/sqlite');
let projectsStore: typeof import('../storage/projects');
let conversationsStore: typeof import('../storage/conversations');

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/projects', projectsRouter);
  return a;
}

beforeAll(async () => {
  // sqlite 数据文件落在 cwd/.xueban/ — 切到临时目录跑隔离库。
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-projects-ws-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  projectsStore = await import('../storage/projects');
  conversationsStore = await import('../storage/conversations');
  projectsRouter = (await import('../routes/projects')).default;
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('GET /api/projects?withSessions=1', () => {
  it('无 query 时保持原 shape（无 sessions 字段）', async () => {
    projectsStore.createProject({ name: 'plain', workspace_path: path.join(tmp, 'p0') });
    const res = await request(app()).get('/api/projects');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body[0].project_id).toBeTruthy();
    expect(res.body[0].sessions).toBeUndefined();
  });

  it('withSessions=1 → 每项含 id 别名 + sessions[]（含 group_id 互链）', async () => {
    const p = projectsStore.createProject({
      name: 'with-conv',
      workspace_path: path.join(tmp, 'p1'),
    });
    const c = conversationsStore.createConversation(p.project_id, '主会话');
    // conversations.group_id ↔ Python group（B1 统一会话内核），直接落库模拟。
    sqlite
      .getDb()
      .prepare(`UPDATE conversations SET group_id = ? WHERE conversation_id = ?`)
      .run('g-dm-1', c.conversation_id);

    const res = await request(app()).get('/api/projects?withSessions=1');
    expect(res.status).toBe(200);
    const row = (res.body as Array<{ id: string; project_id: string; name: string; sessions: Array<{ conversation_id: string; title: string | null; group_id: string | null }> }>)
      .find((r) => r.project_id === p.project_id);
    expect(row).toBeTruthy();
    expect(row!.id).toBe(p.project_id);
    expect(row!.name).toBe('with-conv');
    expect(row!.sessions).toHaveLength(1);
    expect(row!.sessions[0].conversation_id).toBe(c.conversation_id);
    expect(row!.sessions[0].title).toBe('主会话');
    expect(row!.sessions[0].group_id).toBe('g-dm-1');
  });

  it('无会话的项目 → sessions 为空数组；项目仍出现', async () => {
    const p = projectsStore.createProject({
      name: 'empty-proj',
      workspace_path: path.join(tmp, 'p2'),
    });
    const res = await request(app()).get('/api/projects?withSessions=true');
    expect(res.status).toBe(200);
    const row = (res.body as Array<{ project_id: string; sessions: unknown[] }>).find(
      (r) => r.project_id === p.project_id,
    );
    expect(row).toBeTruthy();
    expect(row!.sessions).toEqual([]);
  });
});
