/**
 * run-session-step-get.test.ts — Task 1 unit tests for
 * GET /api/run-sessions/:id/steps/:n (补 fetchStepArtifact 缺失后端端点).
 *
 * Run (from repo root):
 *   npx vitest run --root server --config ../vitest.config.ts \
 *     src/routes/__tests__/run-session-step-get.test.ts
 *
 * Isolation: importing run-sessions.ts has module-load side effects (it mkdir's
 * .xueban/{sessions,runs} in cwd). We chdir to a throwaway tmp dir BEFORE
 * the dynamic import so the real project .xueban is never touched.
 * Pattern mirrors run-sessions-stream-disposition.test.ts.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import express from 'express';
import request from 'supertest';

let runSessionsRouter: express.Router;
let origCwd: string;
let tmp: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-step-get-'));
  process.chdir(tmp);
  const mod = await import('../run-sessions');
  runSessionsRouter = mod.default;
});

afterAll(() => {
  process.chdir(origCwd);
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/run-sessions', runSessionsRouter);
  return a;
}

describe('GET /api/run-sessions/:id/steps/:n', () => {
  it('returns 404 with NOT_FOUND when step artifact does not exist', async () => {
    const res = await request(app()).get('/api/run-sessions/nonexistent-sid/steps/0');
    expect(res.status).toBe(404);
    expect(res.body.error.code).toBe('NOT_FOUND');
  });

  it('returns 400 when n is not a valid step index', async () => {
    const res = await request(app()).get('/api/run-sessions/some-sid/steps/abc');
    expect(res.status).toBe(400);
  });
});
