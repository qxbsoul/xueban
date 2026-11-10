/**
 * run-index.test.ts — 持久 run 索引存储测试（2026-11-05 迭代三补）。
 *
 * 覆盖：upsert 幂等 / setRunIndexStatus / listRunIndexByConversation /
 * sweepOrphanRunIndex（NULL → canceled，已终态行不动，幂等）。
 *
 * 隔离：chdir 临时目录 + sqlite._resetForTests()（参考 groups-store.test.ts）。
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

import * as sqlite from './sqlite';
import * as idx from './run-index';

let origCwd: string;
let tmpDir: string;

beforeEach(() => {
  origCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-run-index-'));
  process.chdir(tmpDir);
  sqlite._resetForTests();
  sqlite.getDb();
});

afterEach(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

describe('run-index', () => {
  it('upsert 幂等：同 session 二次 upsert 更新而不插新行', () => {
    idx.upsertRunIndex({ session_id: 's1', conversation_id: 'c1', goal: 'g', created_at: 1 });
    idx.upsertRunIndex({ session_id: 's1', conversation_id: 'c1', goal: 'g2', created_at: 2 });
    const rows = idx.listRunIndexByConversation('c1');
    expect(rows).toHaveLength(1);
    expect(rows[0].goal).toBe('g2');
  });

  it('setRunIndexStatus 回填终态', () => {
    idx.upsertRunIndex({ session_id: 's1', conversation_id: 'c1', goal: 'g' });
    expect(idx.listRunIndexByConversation('c1')[0].status).toBeNull();
    idx.setRunIndexStatus('s1', 'succeeded');
    expect(idx.listRunIndexByConversation('c1')[0].status).toBe('succeeded');
  });

  it('sweepOrphanRunIndex：NULL → canceled，终态行不动，幂等', () => {
    idx.upsertRunIndex({ session_id: 'orphan1', conversation_id: 'c1', goal: 'g' });
    idx.upsertRunIndex({ session_id: 'orphan2', conversation_id: 'c1', goal: 'g' });
    idx.upsertRunIndex({ session_id: 'finished', conversation_id: 'c1', goal: 'g' });
    idx.setRunIndexStatus('finished', 'succeeded');

    const n = idx.sweepOrphanRunIndex();
    expect(n).toBe(2);
    const byId = new Map(idx.listRunIndexByConversation('c1').map((r) => [r.session_id, r]));
    expect(byId.get('orphan1')!.status).toBe('canceled');
    expect(byId.get('orphan2')!.status).toBe('canceled');
    expect(byId.get('finished')!.status).toBe('succeeded');

    // 幂等
    expect(idx.sweepOrphanRunIndex()).toBe(0);
  });
});
