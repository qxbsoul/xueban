/**
 * run-sessions-stream-disposition.test.ts — O4 unit tests for the /stream
 * graceful-degradation decision (read-only replay vs. 404).
 *
 * Run (from repo root):
 *   npx vitest run --root server --config ../vitest.config.ts \
 *     src/routes/__tests__/run-sessions-stream-disposition.test.ts
 *
 * The pure `resolveStreamDisposition` mirrors the swapDagRun unit-test pattern
 * (groups-chat-dag-lifecycle.test.ts): the route handler's branching is lifted
 * into an exported pure function so it's testable without Express/SSE/fs.
 *
 * Isolation: importing run-sessions.ts has module-load side effects (it mkdir's
 * .xueban/{sessions,runs} in cwd and hydrates from disk). We chdir to a
 * throwaway tmp dir BEFORE the dynamic import so the real server/.xueban is
 * never touched, then restore cwd + rm the tmp dir afterwards.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

let resolveStreamDisposition: typeof import('../run-sessions').resolveStreamDisposition;
let origCwd: string;
let tmp: string;

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-stream-disp-'));
  process.chdir(tmp);
  ({ resolveStreamDisposition } = await import('../run-sessions'));
});

afterAll(() => {
  process.chdir(origCwd);
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

describe('resolveStreamDisposition (O4)', () => {
  it('session present → live (normal pipeline path)', () => {
    expect(resolveStreamDisposition({ hasSession: true, run: undefined })).toEqual({
      kind: 'live',
    });
    // hasSession wins even if a run also exists.
    expect(
      resolveStreamDisposition({ hasSession: true, run: { status: 'succeeded' } }),
    ).toEqual({ kind: 'live' });
  });

  it('session gone but run recoverable → read-only replay (terminal)', () => {
    expect(
      resolveStreamDisposition({ hasSession: false, run: { status: 'succeeded' } }),
    ).toEqual({ kind: 'replay', status: 'succeeded' });
    expect(
      resolveStreamDisposition({ hasSession: false, run: { status: 'canceled' } }),
    ).toEqual({ kind: 'replay', status: 'canceled' });
    expect(
      resolveStreamDisposition({ hasSession: false, run: { status: 'failed' } }),
    ).toEqual({ kind: 'replay', status: 'failed' });
  });

  it('session gone but run still running (orphaned-but-live) → replay, not 404', () => {
    // A run whose session was deleted while the run kept going: still serve a
    // read-only view rather than killing it.
    expect(
      resolveStreamDisposition({ hasSession: false, run: { status: 'running' } }),
    ).toEqual({ kind: 'replay', status: 'running' });
  });

  it('session gone and no recoverable run → gone (accurate 404)', () => {
    expect(resolveStreamDisposition({ hasSession: false, run: undefined })).toEqual({
      kind: 'gone',
    });
  });
});
