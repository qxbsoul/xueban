/**
 * routes/__tests__/groups-event-sse-replay.test.ts — 群 SSE 断线补帧兜底（2026-11-07）。
 *
 * 覆盖 groups-event-bus.ts 的「组内单调 id + 环形缓冲 + 游标补发」与
 * groups-node.ts GET /:id/events 的「id: 行 + Last-Event-ID/?last_event_id 游标
 * 续传 + 先订阅后补帧去重」。Python 侧同日对称改动见
 * tests/test_group_events_replay.py。
 *
 * 隔离：chdir 临时目录 + sqlite._resetForTests()（同 groups-node.test.ts）。
 * SSE 集成用真实端口 + http.request 流式读（仓库无 supertest）。
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import express, { type Express } from 'express';
import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';

import * as sqlite from '../../storage/sqlite';
import { groupsNodeRouter } from '../groups-node';
import {
  publishGroupEvent,
  subscribe,
  unsubscribe,
  getGroupEventsAfter,
  latestSeq,
  subscriberCount,
  resetGroupEventBusForTests,
  REPLAY_BUFFER_MAX,
} from '../groups-event-bus';

// ── SSE 测试工具 ──────────────────────────────────────────────────────────────

interface SseConn {
  raw: () => string;
  waitFor: (pred: (raw: string) => boolean, label: string) => Promise<void>;
  close: () => void;
}

function openSse(port: number, urlPath: string, headers: Record<string, string> = {}): SseConn {
  const chunks: string[] = [];
  const req = http.request(
    { host: '127.0.0.1', port, method: 'GET', path: urlPath, headers: { Accept: 'text/event-stream', ...headers } },
    (res) => {
      res.setEncoding('utf-8');
      res.on('data', (c: string) => chunks.push(c));
    },
  );
  req.end();
  return {
    raw: () => chunks.join(''),
    waitFor: (pred, label) =>
      new Promise((resolve, reject) => {
        const t0 = Date.now();
        const tick = () => {
          if (pred(chunks.join(''))) return resolve();
          if (Date.now() - t0 > 3000) {
            return reject(new Error(`waitFor timeout (${label}); raw=${chunks.join('')}`));
          }
          setTimeout(tick, 5);
        };
        tick();
      }),
    close: () => {
      req.destroy();
    },
  };
}

let app: Express;
let server: http.Server;
let port: number;
let origCwd: string;
let tmpDir: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-groups-sse-'));
  process.chdir(tmpDir);
  sqlite._resetForTests();
  sqlite.getDb();

  app = express();
  app.use(express.json());
  app.use('/api/groups', groupsNodeRouter);
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

beforeEach(() => {
  resetGroupEventBusForTests();
});

async function createGroup(name: string): Promise<string> {
  const r = await fetch(`http://127.0.0.1:${port}/api/groups`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  const body = (await r.json()) as { group_id: string };
  return body.group_id;
}

const msg = (m: string) => ({ type: 'agent.message', data: { content: m } });

// ── 总线单测 ─────────────────────────────────────────────────────────────────

describe('groups-event-bus — seq + replay buffer', () => {
  it('publish 分配组内单调 id，组间独立', () => {
    expect(publishGroupEvent('a', msg('1'))).toBe(0);
    expect(publishGroupEvent('a', msg('2'))).toBe(1);
    expect(publishGroupEvent('b', msg('1'))).toBe(0);
    expect(latestSeq('a')).toBe(1);
  });

  it('事件以副本附 id 发出，不改调用方对象', () => {
    const original = msg('x');
    const seen: Array<{ id?: number }> = [];
    const fn = (e: { id?: number }) => seen.push(e);
    subscribe('g', fn);
    publishGroupEvent('g', original);
    unsubscribe('g', fn);
    expect(original).not.toHaveProperty('id');
    expect(seen[0].id).toBe(0);
  });

  it('无订阅者也入缓冲（断线窗口的帧不丢）', () => {
    publishGroupEvent('g', msg('lost-then-found'));
    expect(getGroupEventsAfter('g', null).map((e) => e.id)).toEqual([0]);
  });

  it('agent.typing 占 id 但不入缓冲', () => {
    publishGroupEvent('g', { type: 'agent.typing', data: {} });
    publishGroupEvent('g', msg('after-typing'));
    expect(latestSeq('g')).toBe(1);
    expect(getGroupEventsAfter('g', null).map((e) => e.id)).toEqual([1]);
  });

  it('getGroupEventsAfter 游标语义：过滤 / null 全量 / 越界旧游标全量 / 水位处空', () => {
    for (let i = 0; i < 4; i++) publishGroupEvent('g', msg(`m${i}`));
    expect(getGroupEventsAfter('g', 1).map((e) => e.id)).toEqual([2, 3]);
    expect(getGroupEventsAfter('g', null).map((e) => e.id)).toEqual([0, 1, 2, 3]);
    // 进程重启后 seq 归零的旧游标（42 > 水位 3）→ 视同无游标全量补发
    expect(getGroupEventsAfter('g', 42).map((e) => e.id)).toEqual([0, 1, 2, 3]);
    expect(getGroupEventsAfter('g', 3)).toEqual([]);
  });

  it('环形缓冲驱逐最旧，seq 不受驱逐影响', () => {
    let seq = -1;
    for (let i = 0; i < REPLAY_BUFFER_MAX + 10; i++) seq = publishGroupEvent('g', msg(`m${i}`));
    const ids = getGroupEventsAfter('g', null).map((e) => e.id);
    expect(ids).toHaveLength(REPLAY_BUFFER_MAX);
    expect(ids[0]).toBe(10);
    expect(seq).toBe(REPLAY_BUFFER_MAX + 10 - 1);
  });

  it('缓冲在退订后保留（断线重连的恢复源）', () => {
    const fn = () => {};
    subscribe('g', fn);
    publishGroupEvent('g', msg('m'));
    unsubscribe('g', fn);
    expect(subscriberCount('g')).toBe(0);
    expect(getGroupEventsAfter('g', null).map((e) => e.id)).toEqual([0]);
  });
});

// ── 端点集成：id: 行 + 游标续传 + 先订阅后补帧去重 ───────────────────────────

describe('GET /:id/events — 断线补帧', () => {
  it('live 帧带 id: 行（Last-Event-ID 恢复锚点）', async () => {
    const gid = await createGroup('LiveIdLine');
    const conn = openSse(port, `/api/groups/${gid}/events`);
    await conn.waitFor((r) => r.includes(': connected'), 'connected');
    publishGroupEvent(gid, msg('hello'));
    await conn.waitFor((r) => r.includes('hello'), 'live frame');
    expect(conn.raw()).toContain('id: 0\nevent: agent.message\ndata: {"content":"hello"}');
    conn.close();
  });

  it('带 Last-Event-ID 重连：只补缺失帧，live 继续', async () => {
    const gid = await createGroup('ReconnectReplay');
    publishGroupEvent(gid, msg('A')); // id 0 —— 客户端已看过

    const first = openSse(port, `/api/groups/${gid}/events`);
    await first.waitFor((r) => r.includes('"content":"A"'), 'first A');
    first.close();

    publishGroupEvent(gid, msg('B')); // id 1 —— 断线窗口
    publishGroupEvent(gid, msg('C')); // id 2 —— 断线窗口

    const second = openSse(port, `/api/groups/${gid}/events`, { 'Last-Event-ID': '0' });
    await second.waitFor((r) => r.includes('"content":"C"'), 'replayed C');
    const raw = second.raw();
    expect(raw).not.toContain('"content":"A"'); // 游标之后才补
    expect(raw.indexOf('id: 1\n')).toBeGreaterThan(-1);
    expect(raw.indexOf('id: 1\n')).toBeLessThan(raw.indexOf('id: 2\n')); // 按序补

    publishGroupEvent(gid, msg('D')); // id 3 —— 重连后 live
    await second.waitFor((r) => r.includes('"content":"D"'), 'live D');
    second.close();
  });

  it('无游标连接（浏览器未设过 Last-Event-ID）→ 全量补发缓冲', async () => {
    const gid = await createGroup('NoCursor');
    publishGroupEvent(gid, msg('A'));
    publishGroupEvent(gid, msg('B'));
    const conn = openSse(port, `/api/groups/${gid}/events`);
    await conn.waitFor((r) => r.includes('"content":"B"'), 'replayed B');
    expect(conn.raw()).toContain('"content":"A"');
    conn.close();
  });

  it('进程重启后旧游标越界 → 全量补发（客户端幂等去重兜底）', async () => {
    const gid = await createGroup('StaleCursor');
    publishGroupEvent(gid, msg('A'));
    const conn = openSse(port, `/api/groups/${gid}/events`, { 'Last-Event-ID': '42' });
    await conn.waitFor((r) => r.includes('"content":"A"'), 'replayed A');
    conn.close();
  });

  it('?last_event_id= 查询参数等效（sseClient 约定）', async () => {
    const gid = await createGroup('QueryCursor');
    publishGroupEvent(gid, msg('A'));
    publishGroupEvent(gid, msg('B'));
    const conn = openSse(port, `/api/groups/${gid}/events?last_event_id=0`);
    await conn.waitFor((r) => r.includes('"content":"B"'), 'replayed B');
    expect(conn.raw()).not.toContain('"content":"A"');
    conn.close();
  });

  it('agent.typing 不补发（瞬态帧），但 id 序列不受影响', async () => {
    const gid = await createGroup('TypingSkip');
    publishGroupEvent(gid, { type: 'agent.typing', data: { agent: 'a' } }); // id 0
    publishGroupEvent(gid, msg('B')); // id 1
    const conn = openSse(port, `/api/groups/${gid}/events`);
    await conn.waitFor((r) => r.includes('"content":"B"'), 'replayed B');
    const raw = conn.raw();
    expect(raw).not.toContain('agent.typing');
    expect(raw).toContain('id: 1\nevent: agent.message');
    conn.close();
  });

  it('补帧期间的 live 帧按水位去重（先订阅后补帧）', async () => {
    const gid = await createGroup('OverlapDedupe');
    publishGroupEvent(gid, msg('A')); // id 0 —— 断线窗口
    // 无游标连接会全量补发缓冲；A 只应出现一次（补发路径），不因订阅重叠重复
    const conn = openSse(port, `/api/groups/${gid}/events`);
    await conn.waitFor((r) => r.includes('"content":"A"'), 'replayed A');
    publishGroupEvent(gid, msg('B')); // id 1 —— 实时
    await conn.waitFor((r) => r.includes('"content":"B"'), 'live B');
    const occurrences = conn.raw().split('"content":"A"').length - 1;
    expect(occurrences).toBe(1);
    conn.close();
  });
});
