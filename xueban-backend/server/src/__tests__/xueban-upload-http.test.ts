/**
 * 真实 Express + Multer 上传测试（2026-11-12）。
 *
 * 验证：
 *   - 多文件总量超 30MB → 413，且失败请求中已接收附件的 buffer 被释放；
 *   - 单文件超 25MB → 413（Multer fileSize）；
 *   - 小文件可以正常通过 Multer（随后因未登录返回 401）。
 */
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let router: typeof import('../routes/xueban-chat').default;
let storage: typeof import('../routes/xueban-chat').cappedMemoryStorage;

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/xueban-chat', router);
  return a;
}

const MB = 1024 * 1024;
const observedBuffers: Array<Buffer | undefined> = [];

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-upload-http-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  const mod = await import('../routes/xueban-chat');
  router = mod.default;
  storage = mod.cappedMemoryStorage;
  sqlite.getDb();

  const original = storage._removeFile.bind(storage);
  vi.spyOn(storage, '_removeFile').mockImplementation((req, file, callback) => {
    original(req, file, () => {
      observedBuffers.push((file as { buffer?: Buffer }).buffer);
      callback(null);
    });
  });
});

afterAll(() => {
  vi.restoreAllMocks();
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('真实 HTTP 上传', () => {
  it('多文件总量超过 30MB → 413，并释放已接收附件', async () => {
    observedBuffers.length = 0;
    const res = await request(app())
      .post('/api/xueban-chat/any/messages')
      .attach('files', Buffer.alloc(11 * MB), 'a.bin')
      .attach('files', Buffer.alloc(11 * MB), 'b.bin')
      .attach('files', Buffer.alloc(11 * MB), 'c.bin');
    expect(res.status).toBe(413);
    expect(res.body?.error?.code).toBe('UPLOAD_LIMIT');
    expect(observedBuffers.length).toBeGreaterThan(0);
    expect(observedBuffers.every((buffer) => buffer === undefined)).toBe(true);
  });

  it('单文件超过 25MB → 413', async () => {
    const res = await request(app())
      .post('/api/xueban-chat/any/messages')
      .attach('files', Buffer.alloc(26 * MB), 'big.bin');
    expect(res.status).toBe(413);
    expect(res.body?.error?.code).toBe('UPLOAD_LIMIT');
  });

  it('小文件可以通过上传阶段（随后因未登录返回 401）', async () => {
    const res = await request(app())
      .post('/api/xueban-chat/any/messages')
      .attach('files', Buffer.from('hello'), 'a.txt');
    expect(res.status).toBe(401);
  });
});
