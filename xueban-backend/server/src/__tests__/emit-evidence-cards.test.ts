/**
 * 2026-11-07 检查器产出归属 — emitCommitCard/emitFileDiffCards 归属契约
 * （NeoAgentInspector 工具卡/产出卡归属链路回归，与 emit-tool-chunk who 同批语义）：
 *   - who 传入 → commit_record/file_diff 卡带 agent_id/agent_name（检查器按
 *     agent 筛产出，「有 agent_id 的按 id 筛」）；
 *   - 无 who（编排者合并提交 / 老调用）→ 不带归属字段，零行为变化；
 *   - 并发交错：两 agent 的证据卡各自归属不串台（对齐 emit-tool-chunk 的
 *     per-agent 槽测试）。
 */
import { describe, it, expect } from 'vitest';
import { emitCommitCard, emitFileDiffCards } from '../routes/groups-chat';
import type { TimelineMessage, MessagePatch } from '../lib/contracts';
import type { GitFileDiff } from '../lib/git-introspect';

function makeGw() {
  const emitted: TimelineMessage[] = [];
  let n = 0;
  return {
    gw: {
      nextId: () => `gw_test_${++n}`,
      turnId: 'turn_1',
      emitMessage: (m: TimelineMessage) => emitted.push(m),
      emitPatch: (_p: MessagePatch) => {},
    },
    emitted,
  };
}

const baseCommit = {
  repo: 'Scout',
  sha: 'abc1234',
  short_sha: 'abc1234',
  message: '[sf] Scout: 扫描任务',
  author: 'Scout',
  files_changed: 1,
  insertions: 3,
  deletions: 1,
};

const baseDiffs: GitFileDiff[] = [
  { path: 'src/a.ts', added: 2, removed: 0, lines: [] },
  { path: 'src/b.ts', added: 1, removed: 1, lines: [] },
];

describe('emitCommitCard/emitFileDiffCards（检查器产出归属）', () => {
  it('who 归属 — commit_record 带 agent_id/agent_name', () => {
    const { gw, emitted } = makeGw();
    emitCommitCard(gw, baseCommit, { agentId: 'a1', agentName: 'Scout' });
    expect(emitted).toHaveLength(1);
    const m = emitted[0] as Extract<TimelineMessage, { kind: 'commit_record' }>;
    expect(m.kind).toBe('commit_record');
    expect(m.sha).toBe('abc1234');
    expect(m.agent_id).toBe('a1');
    expect(m.agent_name).toBe('Scout');
  });

  it('who 归属 — file_diff 每张卡都带 agent_id/agent_name', () => {
    const { gw, emitted } = makeGw();
    emitFileDiffCards(gw, baseDiffs, { agentId: 'a1', agentName: 'Scout' });
    expect(emitted).toHaveLength(2);
    for (const m of emitted) {
      const d = m as Extract<TimelineMessage, { kind: 'file_diff' }>;
      expect(d.kind).toBe('file_diff');
      expect(d.agent_id).toBe('a1');
      expect(d.agent_name).toBe('Scout');
    }
    expect((emitted[0] as Extract<TimelineMessage, { kind: 'file_diff' }>).path).toBe('src/a.ts');
    expect((emitted[1] as Extract<TimelineMessage, { kind: 'file_diff' }>).path).toBe('src/b.ts');
  });

  it('无 who（编排者合并提交/老调用）— 不带归属字段，零行为变化', () => {
    const { gw, emitted } = makeGw();
    emitCommitCard(gw, baseCommit);
    emitFileDiffCards(gw, baseDiffs);
    expect(emitted).toHaveLength(3);
    for (const m of emitted) {
      const withAttr = m as { agent_id?: string; agent_name?: string };
      expect(withAttr.agent_id).toBeUndefined();
      expect(withAttr.agent_name).toBeUndefined();
    }
  });

  it('并发交错 — 两 agent 的证据卡各自归属不串台', () => {
    const { gw, emitted } = makeGw();
    emitCommitCard(gw, { ...baseCommit, sha: 'aaaaaaa', short_sha: 'aaaaaaa' }, { agentId: 'a1', agentName: 'A' });
    emitCommitCard(gw, { ...baseCommit, sha: 'bbbbbbb', short_sha: 'bbbbbbb' }, { agentId: 'a2', agentName: 'B' });
    emitFileDiffCards(gw, [{ path: 'a.ts', added: 1, removed: 0, lines: [] }], { agentId: 'a1', agentName: 'A' });
    emitFileDiffCards(gw, [{ path: 'b.ts', added: 1, removed: 0, lines: [] }], { agentId: 'a2', agentName: 'B' });

    expect(emitted.map((m) => [m.kind, (m as { agent_id?: string }).agent_id])).toEqual([
      ['commit_record', 'a1'],
      ['commit_record', 'a2'],
      ['file_diff', 'a1'],
      ['file_diff', 'a2'],
    ]);
  });

  it('gw=null → no-op（不抛）', () => {
    expect(() => emitCommitCard(null, baseCommit, { agentId: 'a1', agentName: 'A' })).not.toThrow();
    expect(() => emitFileDiffCards(null, baseDiffs, { agentId: 'a1', agentName: 'A' })).not.toThrow();
  });

  it('emitCommitCard 空 sha → 不 emit', () => {
    const { gw, emitted } = makeGw();
    emitCommitCard(gw, { ...baseCommit, sha: '' }, { agentId: 'a1', agentName: 'A' });
    expect(emitted).toHaveLength(0);
  });
});
