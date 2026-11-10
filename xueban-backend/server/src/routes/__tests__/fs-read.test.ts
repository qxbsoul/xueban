// server/src/routes/__tests__/fs-read.test.ts
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import fsRouter from '../fs-browse';

let root: string;
beforeAll(() => {
  root = mkdtempSync(path.join(tmpdir(), 'sf-read-'));
  writeFileSync(path.join(root, 'doc.md'), '# content', 'utf8');
});
afterAll(() => rmSync(root, { recursive: true, force: true }));
function app() { const a = express(); a.use('/api/fs', fsRouter); return a; }

describe('GET /api/fs/read', () => {
  it('reads a file inside root', async () => {
    const res = await request(app()).get('/api/fs/read').query({ path: path.join(root, 'doc.md'), root });
    expect(res.status).toBe(200);
    expect(res.body.data.content).toBe('# content');
  });

  it('403 when path escapes root (traversal)', async () => {
    const res = await request(app()).get('/api/fs/read').query({ path: path.join(root, '..', '..', 'etc', 'passwd'), root });
    expect(res.status).toBe(403);
    expect(res.body.error.code).toBe('FORBIDDEN');
  });

  it('400 when root missing', async () => {
    const res = await request(app()).get('/api/fs/read').query({ path: path.join(root, 'doc.md') });
    expect(res.status).toBe(400);
  });

  it('truncates files larger than 1MB with truncated:true', async () => {
    // 写一个超过 1MB 的文件
    const bigFile = path.join(root, 'big.bin');
    // 1MB + 1 字节
    const buf = Buffer.alloc(1024 * 1024 + 1, 'x');
    writeFileSync(bigFile, buf);
    const res = await request(app()).get('/api/fs/read').query({ path: bigFile, root });
    expect(res.status).toBe(200);
    expect(res.body.data.truncated).toBe(true);
    expect(res.body.data.content.length).toBeLessThanOrEqual(1024 * 1024);
  });

  // C1: 空 path 前置拒绝
  it('400 NO_PATH when path is empty', async () => {
    const res = await request(app()).get('/api/fs/read').query({ root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_PATH');
  });

  // M3: 相对路径拒绝
  it('400 NOT_ABSOLUTE when path is relative', async () => {
    const res = await request(app()).get('/api/fs/read').query({ path: 'relative/path/file.md', root });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NOT_ABSOLUTE');
  });

  // 兄弟前缀目录（前缀绕过）：<base>/foo 是 root，<base>/foobar/x.md 不在 root 内
  it('403 FORBIDDEN when sibling dir shares prefix with root', async () => {
    const base = mkdtempSync(path.join(tmpdir(), 'sf-prefix-'));
    try {
      const fooRoot = path.join(base, 'foo');
      const foobarDir = path.join(base, 'foobar');
      mkdirSync(fooRoot);
      mkdirSync(foobarDir);
      writeFileSync(path.join(foobarDir, 'x.md'), 'secret', 'utf8');
      const res = await request(app())
        .get('/api/fs/read')
        .query({ path: path.join(foobarDir, 'x.md'), root: fooRoot });
      expect(res.status).toBe(403);
      expect(res.body.error.code).toBe('FORBIDDEN');
    } finally {
      rmSync(base, { recursive: true, force: true });
    }
  });

  // I1: symlink 越权（需要系统有创建 symlink 权限；无权限时跳过）
  it('403 FORBIDDEN when symlink points outside root', async () => {
    const symlinkBase = mkdtempSync(path.join(tmpdir(), 'sf-sym-'));
    try {
      const symlinkRoot = path.join(symlinkBase, 'allowed');
      const outsideDir = path.join(symlinkBase, 'outside');
      mkdirSync(symlinkRoot);
      mkdirSync(outsideDir);
      writeFileSync(path.join(outsideDir, 'secret.txt'), 'topsecret', 'utf8');

      const linkPath = path.join(symlinkRoot, 'link.txt');
      let canSymlink = true;
      try {
        symlinkSync(path.join(outsideDir, 'secret.txt'), linkPath);
      } catch {
        canSymlink = false;
      }

      if (!canSymlink) {
        // 无权限创建 symlink（常见于 Windows 非管理员），跳过
        return;
      }

      const res = await request(app())
        .get('/api/fs/read')
        .query({ path: linkPath, root: symlinkRoot });
      expect(res.status).toBe(403);
      expect(res.body.error.code).toBe('FORBIDDEN');
    } finally {
      rmSync(symlinkBase, { recursive: true, force: true });
    }
  });
});
