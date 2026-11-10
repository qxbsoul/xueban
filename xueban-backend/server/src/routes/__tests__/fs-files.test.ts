// server/src/routes/__tests__/fs-files.test.ts
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import fsRouter from '../fs-browse';

let dir: string;
beforeAll(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'sf-files-'));
  writeFileSync(path.join(dir, 'a.md'), '# hi', 'utf8');
  mkdirSync(path.join(dir, 'sub'));        // 子目录不应出现在 files 里
  writeFileSync(path.join(dir, '.hidden'), 'x', 'utf8'); // dotfile 应隐藏
});
afterAll(() => rmSync(dir, { recursive: true, force: true }));

function app() { const a = express(); a.use('/api/fs', fsRouter); return a; }

describe('GET /api/fs/files', () => {
  it('lists files only (no dirs, no dotfiles) with size/ext', async () => {
    const res = await request(app()).get('/api/fs/files').query({ path: dir });
    expect(res.status).toBe(200);
    const names = res.body.data.files.map((f: any) => f.name);
    expect(names).toContain('a.md');
    expect(names).not.toContain('sub');
    expect(names).not.toContain('.hidden');
    const a = res.body.data.files.find((f: any) => f.name === 'a.md');
    expect(a.size).toBeGreaterThan(0);
    expect(a.ext).toBe('.md');
  });

  it('404 when dir missing', async () => {
    const res = await request(app()).get('/api/fs/files').query({ path: path.join(dir, 'nope') });
    expect(res.status).toBe(404);
  });

  it('400 NOT_A_DIR when path points to a file', async () => {
    const filePath = path.join(dir, 'a.md');
    const res = await request(app()).get('/api/fs/files').query({ path: filePath });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NOT_A_DIR');
  });

  it('400 NO_PATH when path is not provided', async () => {
    const res = await request(app()).get('/api/fs/files');
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe('NO_PATH');
  });
});
