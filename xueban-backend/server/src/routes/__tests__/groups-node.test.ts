/**
 * routes/__tests__/groups-node.test.ts — 后端合一·阶段1b（2026-06-12）。
 *
 * 起一个最小 express app 挂 groupsNodeRouter（+ DM resolve handler），用真实端口
 * + http.request 打请求（与 teams-dag-write-validate.test.ts 同样的 no-supertest 风格，
 * 因为仓库未装 supertest）。覆盖：建群/list 三过滤/get 404/patch/messages 往返/
 * dm resolve/reactions/pin/user-settings/校验 400/422。
 *
 * 隔离：chdir 到临时目录 + sqlite._resetForTests()，避免污染真实 .xueban
 * （同 storage/groups-store.test.ts）。
 *
 * 断言对照 Python tests/test_groups_api.py + groups.py 的信封/字段/状态码。
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import express, { type Express } from 'express';
import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';

import * as sqlite from '../../storage/sqlite';
import { groupsNodeRouter, handleDmResolve } from '../groups-node';

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
    const data = body !== undefined ? JSON.stringify(body) : undefined;
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
          try {
            parsed = JSON.parse(text);
          } catch {
            parsed = text;
          }
          resolve({ status: res.statusCode ?? 0, body: parsed });
        });
      },
    );
    r.on('error', reject);
    if (data) r.write(data);
    r.end();
  });
}

let app: Express;
let server: http.Server;
let port: number;
let origCwd: string;
let tmpDir: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-groups-node-'));
  process.chdir(tmpDir);
  sqlite._resetForTests();
  sqlite.getDb(); // boot schema in fresh cwd

  app = express();
  app.use(express.json());
  app.use('/api/groups', groupsNodeRouter);
  app.post('/api/chat/dm/:agent_id/resolve', handleDmResolve);

  await new Promise<void>((resolve) => {
    server = app.listen(0, () => {
      port = (server.address() as { port: number }).port;
      resolve();
    });
  });
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  sqlite._resetForTests();
  process.chdir(origCwd);
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

// 每个 describe 内复用同一 db；用例间靠唯一 name/workspace 区隔，避免相互污染。

describe('POST /api/groups — create', () => {
  it('201 + GroupResponse on success', async () => {
    const r = await req(port, 'POST', '/api/groups', {
      template_id: 'academic-paper',
      group_template_id: 'default-group',
      name: 'Test Group',
      agent_ids: ['agent-a', 'agent-b'],
    });
    expect(r.status).toBe(201);
    expect(r.body.name).toBe('Test Group');
    expect(r.body.template_id).toBe('academic-paper');
    expect(r.body.group_id).toMatch(/^[0-9a-f]{32}$/);
    expect(typeof r.body.created_at).toBe('string');
    expect(r.body.agents).toEqual(['agent-a', 'agent-b']);
  });

  it('trims whitespace in name', async () => {
    const r = await req(port, 'POST', '/api/groups', { name: '  Trimmed  ' });
    expect(r.status).toBe(201);
    expect(r.body.name).toBe('Trimmed');
  });

  it('400 on empty name', async () => {
    const r = await req(port, 'POST', '/api/groups', { name: '' });
    expect(r.status).toBe(400);
    expect(r.body.detail).toBe('name is required');
  });

  it('400 on whitespace-only name', async () => {
    const r = await req(port, 'POST', '/api/groups', { name: '   ' });
    expect(r.status).toBe(400);
  });

  it('422 on name > 40 chars', async () => {
    const r = await req(port, 'POST', '/api/groups', { name: 'x'.repeat(41) });
    expect(r.status).toBe(422);
  });

  it('422 on policy_matrix > 200 keys', async () => {
    const policy: Record<string, number> = {};
    for (let i = 0; i < 201; i++) policy[`k${i}`] = i;
    const r = await req(port, 'POST', '/api/groups', {
      name: 'PolicyHeavy',
      policy_matrix: policy,
    });
    expect(r.status).toBe(422);
  });

  it('201 for unknown template_id (template requirement dropped)', async () => {
    const r = await req(port, 'POST', '/api/groups', {
      name: 'NoTemplate',
      template_id: 'no-such-template-xyz',
    });
    expect(r.status).toBe(201);
    expect(r.body.template_id).toBe('no-such-template-xyz');
  });
});

describe('GET /api/groups — list with three filters', () => {
  beforeEach(() => {
    // 清表，独立可控的列表语境。
    sqlite.getDb().exec('DELETE FROM groups; DELETE FROM group_messages;');
  });

  it('default excludes DM and archived', async () => {
    await req(port, 'POST', '/api/groups', { name: 'G1', workspace_id: 'ws1' });
    await req(port, 'POST', '/api/groups', {
      name: 'DM1',
      workspace_id: 'ws1',
      kind: 'dm',
      agent_ids: ['a1'],
    });
    const r = await req(port, 'GET', '/api/groups?workspace_id=ws1');
    expect(r.status).toBe(200);
    expect(r.body.meta.count).toBe(1);
    expect(r.body.data).toEqual(r.body.groups); // 双形态同值
    expect(r.body.data[0].name).toBe('G1');
  });

  it('kind=dm lists only DM', async () => {
    await req(port, 'POST', '/api/groups', { name: 'G1', workspace_id: 'ws2' });
    await req(port, 'POST', '/api/groups', {
      name: 'DM1',
      workspace_id: 'ws2',
      kind: 'dm',
      agent_ids: ['a1'],
    });
    const r = await req(port, 'GET', '/api/groups?workspace_id=ws2&kind=dm');
    expect(r.body.meta.count).toBe(1);
    expect(r.body.data[0].kind).toBe('dm');
  });

  it('kind=all lists both', async () => {
    await req(port, 'POST', '/api/groups', { name: 'G1', workspace_id: 'ws3' });
    await req(port, 'POST', '/api/groups', {
      name: 'DM1',
      workspace_id: 'ws3',
      kind: 'dm',
      agent_ids: ['a1'],
    });
    const r = await req(port, 'GET', '/api/groups?workspace_id=ws3&kind=all');
    expect(r.body.meta.count).toBe(2);
  });

  it('include_archived toggles archived visibility', async () => {
    const c = await req(port, 'POST', '/api/groups', {
      name: 'Arch',
      workspace_id: 'ws4',
    });
    await req(port, 'PATCH', `/api/groups/${c.body.group_id}`, { archived: true });

    const hidden = await req(port, 'GET', '/api/groups?workspace_id=ws4');
    expect(hidden.body.meta.count).toBe(0);

    const shown = await req(
      port,
      'GET',
      '/api/groups?workspace_id=ws4&include_archived=true',
    );
    expect(shown.body.meta.count).toBe(1);
  });
});

describe('GET /api/groups/:id', () => {
  it('returns enveloped record', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'GetMe' });
    const r = await req(port, 'GET', `/api/groups/${c.body.group_id}`);
    expect(r.status).toBe(200);
    expect(r.body.data.name).toBe('GetMe');
    expect(r.body.meta).toEqual({});
    expect(r.body.data.messages).toEqual([]);
  });

  it('404 with GROUP_NOT_FOUND error envelope', async () => {
    const r = await req(port, 'GET', '/api/groups/deadbeef');
    expect(r.status).toBe(404);
    expect(r.body.detail.error.code).toBe('GROUP_NOT_FOUND');
  });
});

describe('PATCH /api/groups/:id', () => {
  it('updates name/announcement/archived and bumps updated_at', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'Before' });
    const r = await req(port, 'PATCH', `/api/groups/${c.body.group_id}`, {
      name: 'After',
      announcement: 'hello',
      archived: true,
    });
    expect(r.status).toBe(200);
    expect(r.body.data.name).toBe('After');
    expect(r.body.data.announcement).toBe('hello');
    expect(r.body.data.archived).toBe(true);
    expect(typeof r.body.data.updated_at).toBe('string');
  });

  it('empty workspace_dir string clears it (→ null)', async () => {
    const c = await req(port, 'POST', '/api/groups', {
      name: 'WD',
      workspace_dir: '/some/path',
    });
    const r = await req(port, 'PATCH', `/api/groups/${c.body.group_id}`, {
      workspace_dir: '',
    });
    expect(r.body.data.workspace_dir).toBeNull();
  });

  it('404 on missing group', async () => {
    const r = await req(port, 'PATCH', '/api/groups/nope', { name: 'X' });
    expect(r.status).toBe(404);
    expect(r.body.detail.error.code).toBe('GROUP_NOT_FOUND');
  });
});

describe('messages round-trip', () => {
  it('POST then GET returns the message', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'MsgGrp' });
    const gid = c.body.group_id;
    const p = await req(port, 'POST', `/api/groups/${gid}/messages`, {
      content: 'hello world',
      sender_name: 'user',
      sender_kind: 'user',
    });
    expect(p.status).toBe(201);
    expect(p.body.content).toBe('hello world');
    expect(p.body.message_id).toMatch(/^[0-9a-f]{32}$/);

    const g = await req(port, 'GET', `/api/groups/${gid}/messages`);
    expect(g.status).toBe(200);
    expect(g.body.group_id).toBe(gid);
    expect(g.body.messages).toHaveLength(1);
    expect(g.body.messages[0].content).toBe('hello world');
  });

  it('422 on empty content', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'EmptyMsg' });
    const r = await req(port, 'POST', `/api/groups/${c.body.group_id}/messages`, {
      content: '',
    });
    expect(r.status).toBe(422);
  });

  it('404 posting to missing group', async () => {
    const r = await req(port, 'POST', '/api/groups/nope/messages', {
      content: 'x',
    });
    expect(r.status).toBe(404);
  });

  it('limit returns last N', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'LimitGrp' });
    const gid = c.body.group_id;
    for (let i = 0; i < 5; i++) {
      await req(port, 'POST', `/api/groups/${gid}/messages`, { content: `m${i}` });
    }
    const r = await req(port, 'GET', `/api/groups/${gid}/messages?limit=2`);
    expect(r.body.messages).toHaveLength(2);
    expect(r.body.messages[1].content).toBe('m4');
  });
});

describe('DM resolve', () => {
  it('creates then finds the same DM (idempotent)', async () => {
    const r1 = await req(
      port,
      'POST',
      '/api/chat/dm/agent-x/resolve?workspace_id=wsdm',
    );
    expect(r1.status).toBe(200);
    expect(r1.body.created).toBe(true);
    expect(r1.body.agent_id).toBe('agent-x');

    const r2 = await req(
      port,
      'POST',
      '/api/chat/dm/agent-x/resolve?workspace_id=wsdm',
    );
    expect(r2.body.created).toBe(false);
    expect(r2.body.group_id).toBe(r1.body.group_id);
  });
});

describe('reactions', () => {
  it('toggles a reaction on a message', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'RxGrp' });
    const gid = c.body.group_id;
    const p = await req(port, 'POST', `/api/groups/${gid}/messages`, {
      content: 'react to me',
    });
    const mid = p.body.message_id;

    const add = await req(
      port,
      'POST',
      `/api/groups/${gid}/messages/${mid}/reactions`,
      { emoji: '👍', user_id: 'u1' },
    );
    expect(add.status).toBe(200);
    expect(add.body.data.reactions['👍']).toEqual(['u1']);

    const remove = await req(
      port,
      'POST',
      `/api/groups/${gid}/messages/${mid}/reactions`,
      { emoji: '👍', user_id: 'u1' },
    );
    expect(remove.body.data.reactions['👍']).toBeUndefined();
  });

  it('404 reacting to missing message', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'RxMiss' });
    const r = await req(
      port,
      'POST',
      `/api/groups/${c.body.group_id}/messages/nope/reactions`,
      { emoji: '👍' },
    );
    expect(r.status).toBe(404);
    expect(r.body.detail.error.code).toBe('MESSAGE_NOT_FOUND');
  });
});

describe('pin', () => {
  it('toggles pinned when omitted; explicit sets', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'PinGrp' });
    const gid = c.body.group_id;
    const p = await req(port, 'POST', `/api/groups/${gid}/messages`, {
      content: 'pin me',
    });
    const mid = p.body.message_id;

    const t1 = await req(port, 'POST', `/api/groups/${gid}/messages/${mid}/pin`, {});
    expect(t1.body.data.pinned).toBe(true);

    const t2 = await req(port, 'POST', `/api/groups/${gid}/messages/${mid}/pin`, {
      pinned: false,
    });
    expect(t2.body.data.pinned).toBe(false);
  });
});

describe('user-settings', () => {
  it('GET defaults then PUT persists', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'USGrp' });
    const gid = c.body.group_id;

    const def = await req(port, 'GET', `/api/groups/${gid}/user-settings`);
    expect(def.status).toBe(200);
    expect(def.body.data.muted).toBe(false);
    expect(def.body.data.show_nickname).toBe(true);

    const put = await req(port, 'PUT', `/api/groups/${gid}/user-settings`, {
      muted: true,
      pinned: true,
      folded: false,
      show_nickname: false,
      my_nickname: 'Me',
    });
    expect(put.body.data.muted).toBe(true);
    expect(put.body.data.my_nickname).toBe('Me');

    const after = await req(port, 'GET', `/api/groups/${gid}/user-settings`);
    expect(after.body.data.muted).toBe(true);
    expect(after.body.data.my_nickname).toBe('Me');
  });

  it('per-user isolation', async () => {
    const c = await req(port, 'POST', '/api/groups', { name: 'USIso' });
    const gid = c.body.group_id;
    await req(port, 'PUT', `/api/groups/${gid}/user-settings?user_id=alice`, {
      muted: true,
    });
    const bob = await req(
      port,
      'GET',
      `/api/groups/${gid}/user-settings?user_id=bob`,
    );
    expect(bob.body.data.muted).toBe(false); // bob 未存过 → 默认
  });

  it('404 user-settings on missing group', async () => {
    const r = await req(port, 'GET', '/api/groups/nope/user-settings');
    expect(r.status).toBe(404);
  });
});
