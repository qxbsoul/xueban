// server/src/routes/__tests__/fs-write.test.ts
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import fsRouter from '../fs-browse';

let root: string;
beforeAll(() => {
  root = mkdtempSync(path.join(tmpdir(), 'sf-write-'));
  mkdirSync(path.join(root, 'sub'));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

// /write 走 json body；/upload 走 route 级 express.raw（multipart）。
function app() {
  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use('/api/fs', fsRouter);
  return a;
}

describe('POST /api/fs/write', () => {
  it('writes a new file inside root (absolute path)', async () => {
    const target = path.join(root, 'hello.md');
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: target, content: '# 你好', root });
    expect(res.status).toBe(200);
    expect(res.body.data.path).toBe(target);
    expect(readFileSync(target, 'utf8')).toBe('# 你好');
  });

  it('resolves relative path against root and creates parent dirs', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: path.join('sub', 'deep', 'note.txt'), content: 'abc', root });
    expect(res.status).toBe(200);
    expect(readFileSync(path.join(root, 'sub', 'deep', 'note.txt'), 'utf8')).toBe('abc');
  });

  it('400 TRAVERSAL when path contains .. (relative escape)', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: '../evil.txt', content: 'x', root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('TRAVERSAL');
    expect(existsSync(path.join(root, '..', 'evil.txt'))).toBe(false);
  });

  it('403 FORBIDDEN when absolute path escapes root', async () => {
    const outside = path.join(root, '..', `sf-outside-${Date.now()}.txt`);
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: outside, content: 'x', root });
    expect(res.status).toBe(403);
    expect(res.body.error.code).toBe('FORBIDDEN');
    expect(existsSync(outside)).toBe(false);
  });

  it('400 NO_ROOT when root missing', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: path.join(root, 'a.txt'), content: 'x' });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_ROOT');
  });

  it('400 NO_PATH when path missing', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ content: 'x', root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_PATH');
  });

  it('400 BAD_CONTENT when content is not a string', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: path.join(root, 'a.txt'), root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('BAD_CONTENT');
  });

  it('413 TOO_LARGE when content exceeds 1MB', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: path.join(root, 'big.txt'), content: 'x'.repeat(1024 * 1024 + 1), root });
    expect(res.status).toBe(413);
    expect(res.body.error.code).toBe('TOO_LARGE');
  });

  it('400 IS_A_DIR when target is a directory', async () => {
    const res = await request(app())
      .post('/api/fs/write')
      .send({ path: path.join(root, 'sub'), content: 'x', root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('IS_A_DIR');
  });

  it('403 FORBIDDEN when existing file is a symlink pointing outside root', async () => {
    const base = mkdtempSync(path.join(tmpdir(), 'sf-write-sym-'));
    try {
      const symRoot = path.join(base, 'allowed');
      const outsideDir = path.join(base, 'outside');
      mkdirSync(symRoot);
      mkdirSync(outsideDir);
      const secret = path.join(outsideDir, 'secret.txt');
      writeFileSync(secret, 'topsecret', 'utf8');
      const link = path.join(symRoot, 'link.txt');
      try {
        symlinkSync(secret, link);
      } catch {
        return; // Windows 非管理员无 symlink 权限 → 跳过
      }
      const res = await request(app())
        .post('/api/fs/write')
        .send({ path: link, content: 'overwrite', root: symRoot });
      expect(res.status).toBe(403);
      expect(readFileSync(secret, 'utf8')).toBe('topsecret');
    } finally {
      rmSync(base, { recursive: true, force: true });
    }
  });
});

describe('POST /api/fs/upload', () => {
  it('uploads a file into a directory inside root', async () => {
    const res = await request(app())
      .post('/api/fs/upload')
      .field('dir', root)
      .field('root', root)
      .attach('file', Buffer.from('企业资料内容'), '资料.txt');
    expect(res.status).toBe(200);
    expect(res.body.data.name).toBe('资料.txt');
    expect(readFileSync(path.join(root, '资料.txt'), 'utf8')).toBe('企业资料内容');
  });

  it('overwrites an existing file with the same name', async () => {
    writeFileSync(path.join(root, 'dup.txt'), 'old', 'utf8');
    const res = await request(app())
      .post('/api/fs/upload')
      .field('dir', root)
      .field('root', root)
      .attach('file', Buffer.from('new'), 'dup.txt');
    expect(res.status).toBe(200);
    expect(readFileSync(path.join(root, 'dup.txt'), 'utf8')).toBe('new');
  });

  it('400 TRAVERSAL when dir contains ..', async () => {
    // 注意不能用 path.join(root, '..') —— join 会把 '..' 规格化掉，须拼字面量。
    const res = await request(app())
      .post('/api/fs/upload')
      .field('dir', `${root}${path.sep}..`)
      .field('root', root)
      .attach('file', Buffer.from('x'), 'evil.txt');
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('TRAVERSAL');
  });

  it('403 FORBIDDEN when dir escapes root without .. (absolute sibling)', async () => {
    const sibling = mkdtempSync(path.join(tmpdir(), 'sf-upload-out-'));
    try {
      const res = await request(app())
        .post('/api/fs/upload')
        .field('dir', sibling)
        .field('root', root)
        .attach('file', Buffer.from('x'), 'evil.txt');
      expect(res.status).toBe(403);
      expect(res.body.error.code).toBe('FORBIDDEN');
      expect(existsSync(path.join(sibling, 'evil.txt'))).toBe(false);
    } finally {
      rmSync(sibling, { recursive: true, force: true });
    }
  });

  it('400 INVALID_NAME when filename contains path separators', async () => {
    // supertest/form-data 会把 filename 做 basename，构造不出带 '..' 的 filename，
    // 这里手工拼 multipart body 模拟恶意客户端。
    const boundary = '----sfwritetest';
    const body = Buffer.concat([
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="dir"\r\n\r\n${root}\r\n`),
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\n${root}\r\n`),
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="../evil.txt"\r\nContent-Type: application/octet-stream\r\n\r\n`),
      Buffer.from('x'),
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ]);
    const res = await request(app())
      .post('/api/fs/upload')
      .set('Content-Type', `multipart/form-data; boundary=${boundary}`)
      .send(body);
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('INVALID_NAME');
    expect(existsSync(path.join(root, '..', 'evil.txt'))).toBe(false);
  });

  it('400 NO_ROOT when root field missing', async () => {
    const res = await request(app())
      .post('/api/fs/upload')
      .field('dir', root)
      .attach('file', Buffer.from('x'), 'a.txt');
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_ROOT');
  });

  it('400 NO_FILE when no file attached', async () => {
    const res = await request(app())
      .post('/api/fs/upload')
      .field('dir', root)
      .field('root', root);
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_FILE');
  });
});
