/**
 * agent-hops.test.ts — agent 间交接记录存储层测试（2026-11-05）。
 *
 * 覆盖 storage/agent-hops.ts：turn_seq 递增 / open→start→settle 全生命周期 /
 * settle 幂等（已结算不覆盖）/ listHopsByTurn+listRecentHops / summarizeHops
 * 各状态文案 / 空群摘要返回空串。
 *
 * 隔离：chdir 临时目录 + sqlite._resetForTests()（参考 groups-store.test.ts）。
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

import * as sqlite from './sqlite';
import * as hops from './agent-hops';

let origCwd: string;
let tmpDir: string;

beforeEach(() => {
  origCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-agent-hops-'));
  process.chdir(tmpDir);
  sqlite._resetForTests();
  sqlite.getDb(); // boot schema in the fresh cwd
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

describe('agent-hops', () => {
  it('nextTurnSeq 从 1 开始按群递增（不同群互不干扰）', () => {
    expect(hops.nextTurnSeq('g1')).toBe(1);
    hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'a', to_agent: 'b' });
    expect(hops.nextTurnSeq('g1')).toBe(2);
    expect(hops.nextTurnSeq('g2')).toBe(1);
  });

  it('open→start→settle 全生命周期：status 终态 + output_chars + detail', () => {
    const id = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'pm', to_agent: 'dev' });
    expect(hops.listHopsByTurn('g1', 1)[0].status).toBe('pending');
    hops.startHop(id);
    expect(hops.listHopsByTurn('g1', 1)[0].status).toBe('running');
    hops.settleHop(id, 'done', { outputChars: 123 });
    const row = hops.listHopsByTurn('g1', 1)[0];
    expect(row.status).toBe('done');
    expect(row.output_chars).toBe(123);
    expect(row.settled_at).not.toBeNull();
  });

  it('settle 幂等：已结算的 hop 不被覆盖', () => {
    const id = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'a', to_agent: 'b' });
    hops.settleHop(id, 'done', { outputChars: 10 });
    hops.settleHop(id, 'failed', { detail: 'late write attempt' });
    const row = hops.listHopsByTurn('g1', 1)[0];
    expect(row.status).toBe('done');
    expect(row.detail).toBe('');
  });

  it('listRecentHops 返回最近回合（新在前）+ detail 截断到 500', () => {
    hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'a', to_agent: 'b' });
    hops.openHop({ group_id: 'g1', turn_seq: 2, from_agent: 'a', to_agent: 'b' });
    expect(hops.listRecentHops('g1', 1).every((h) => h.turn_seq === 2)).toBe(true);
    expect(hops.listRecentHops('g1', 5).length).toBe(2);
    // detail 截断
    const id = hops.openHop({ group_id: 'g1', turn_seq: 3, from_agent: 'a', to_agent: 'b' });
    hops.settleHop(id, 'failed', { detail: 'x'.repeat(600) });
    expect(hops.listHopsByTurn('g1', 3)[0].detail.length).toBeLessThanOrEqual(500);
  });

  it('summarizeHops：done 带字数、failed 带原因、空回合返回空串', () => {
    const id1 = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'pm', to_agent: 'dev' });
    hops.settleHop(id1, 'done', { outputChars: 456 });
    const id2 = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'dev', to_agent: 'qa' });
    hops.settleHop(id2, 'failed', { detail: 'boom' });
    const summary = hops.summarizeHops('g1', 1);
    expect(summary).toContain('2 次交接');
    expect(summary).toContain('456 字');
    expect(summary).toContain('boom');

    expect(hops.summarizeHops('empty-group', 1)).toBe('');
  });

  it('summarizeHops：deny/abort/未结算各状态文案', () => {
    const d = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'a', to_agent: 'b' });
    hops.settleHop(d, 'denied', { detail: 'policy deny' });
    const ab = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'b', to_agent: 'c' });
    hops.settleHop(ab, 'aborted');
    const dangling = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'c', to_agent: 'd' });
    void dangling; // 故意不结算 → 未结算文案
    const summary = hops.summarizeHops('g1', 1);
    expect(summary).toContain('被策略拦截');
    expect(summary).toContain('回合中止');
    expect(summary).toContain('未结算');
  });

  it('sweepOrphanHops：未结算行补 aborted，已结算行不动，幂等', () => {
    const settled = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'a', to_agent: 'b' });
    hops.settleHop(settled, 'done', { outputChars: 10 });
    const orphan1 = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'b', to_agent: 'c' });
    hops.startHop(orphan1);
    const orphan2 = hops.openHop({ group_id: 'g1', turn_seq: 1, from_agent: 'c', to_agent: 'd' });

    const n = hops.sweepOrphanHops();
    expect(n).toBe(2);
    const rows = hops.listHopsByTurn('g1', 1);
    const byId = new Map(rows.map((r) => [r.id, r]));
    expect(byId.get(settled)!.status).toBe('done'); // 已结算不动
    expect(byId.get(orphan1)!.status).toBe('aborted');
    expect(byId.get(orphan1)!.settled_at).not.toBeNull();
    expect(byId.get(orphan2)!.status).toBe('aborted');
    expect(byId.get(orphan2)!.detail).toContain('进程重启');

    // 幂等：再扫一遍 0 行
    expect(hops.sweepOrphanHops()).toBe(0);
    // 摘要不再显示「未结算」
    expect(hops.summarizeHops('g1', 1)).not.toContain('未结算');
  });
});
