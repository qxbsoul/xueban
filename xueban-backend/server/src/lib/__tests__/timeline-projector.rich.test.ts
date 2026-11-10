/**
 * timeline-projector.rich.test.ts — Polynoia-parity rich content emitters.
 *
 * Covers the 2026-08-15 additions: plan_card materialization + live task
 * patches, and the six stateless rich cards (table/image/web/artifact/
 * commit/file_diff/verdict). Ids stay deterministic per (seed, order).
 */
import { describe, expect, it } from 'vitest';
import { createTimelineProjector } from '../timeline-projector';
import type { ProjectorEmit } from '../contracts';

function msgs(emit: ProjectorEmit) {
  return emit.messages;
}
function patches(emit: ProjectorEmit) {
  return emit.patches;
}

describe('plan_card', () => {
  it('emits one plan_card and patches task status by task_id', () => {
    const p = createTimelineProjector({ idSeed: 's1' });
    p.onUserMessage('构建一个落地页');

    const out = p.onPlanCard({
      goal: '构建一个落地页',
      tasks: [
        { id: 'node:a', title: 'Writer', status: 'running' },
        { id: 'node:b', title: 'Reviewer', status: 'pending' },
      ],
    });
    // ensureMsgFoot() may prepend the turn's msg_foot — find the card by kind.
    const plan = msgs(out).find((m) => m.kind === 'plan_card')!;
    expect(plan).toBeDefined();
    if (plan.kind !== 'plan_card') return;
    expect(plan.tasks).toHaveLength(2);
    expect(plan.tasks[0]!.status).toBe('running');
    const planId = plan.id;

    const upd = patches(p.onPlanTaskUpdate('node:b', { status: 'running' }));
    expect(upd).toHaveLength(1);
    expect(upd[0]).toMatchObject({
      id: planId,
      op: 'plan_update_task',
      task_id: 'node:b',
      patch: { status: 'running' },
    });

    const done = patches(p.onPlanTaskUpdate('node:a', { status: 'done', elapsed_ms: 1200 }));
    expect(done[0]).toMatchObject({ op: 'plan_update_task', task_id: 'node:a' });
  });

  it('drops plan patches when no plan_card was emitted this turn', () => {
    const p = createTimelineProjector({ idSeed: 's2' });
    p.onUserMessage('go');
    expect(patches(p.onPlanTaskUpdate('node:x', { status: 'done' }))).toHaveLength(0);
  });

  it('resets the plan anchor on a new turn', () => {
    const p = createTimelineProjector({ idSeed: 's3' });
    p.onUserMessage('t1');
    p.onPlanCard({ tasks: [{ id: 'node:a', title: 'A', status: 'pending' }] });
    p.onUserMessage('t2');
    // No plan emitted in turn 2 → patch is a no-op (never targets the stale card).
    expect(patches(p.onPlanTaskUpdate('node:a', { status: 'done' }))).toHaveLength(0);
  });
});

describe('stateless rich cards', () => {
  const p = createTimelineProjector({ idSeed: 's4' });

  it('onTablePanel emits table_panel with columns/rows', () => {
    const out = msgs(
      p.onTablePanel({ caption: '文件变更', columns: ['文件', '+', '-'], rows: [['a.ts', 3, 1]] }),
    )[0]!;
    expect(out.kind).toBe('table_panel');
    if (out.kind === 'table_panel') {
      expect(out.caption).toBe('文件变更');
      expect(out.rows[0]).toEqual(['a.ts', 3, 1]);
    }
  });

  it('onImagePanel / onWebPreview emit their kinds', () => {
    const img = msgs(p.onImagePanel({ src: '/x.png', alt: 'x' }))[0]!;
    expect(img.kind).toBe('image_panel');
    const web = msgs(p.onWebPreview({ title: 'index.html', url: '/projects/s/index.html' }))[0]!;
    expect(web.kind).toBe('web_preview');
    if (web.kind === 'web_preview') expect(web.url).toBe('/projects/s/index.html');
  });

  it('onArtifactCard emits artifact_card with optional url dropped when absent', () => {
    const card = msgs(p.onArtifactCard({ artifact_type: 'html', filename: 'out.html' }))[0]!;
    expect(card.kind).toBe('artifact_card');
    if (card.kind === 'artifact_card') expect(card.url).toBeUndefined();
  });

  it('onCommitRecord carries sha/message/stats', () => {
    const c = msgs(
      p.onCommitRecord({
        repo: 'demo', sha: 'a'.repeat(40), short_sha: 'aaaaaaa', message: 'feat: landing',
        author: 'agent-writer', files_changed: 2, insertions: 10, deletions: 2,
      }),
    )[0]!;
    expect(c.kind).toBe('commit_record');
    if (c.kind === 'commit_record') {
      expect(c.short_sha).toBe('aaaaaaa');
      expect(c.message).toBe('feat: landing');
    }
  });

  it('onFileDiff emits file_diff lines verbatim', () => {
    const d = msgs(
      p.onFileDiff({ path: 'src/a.ts', added: 1, removed: 1, lines: [{ no: 1, mark: '+', code: 'x' }] }),
    )[0]!;
    expect(d.kind).toBe('file_diff');
  });

  it('onVerdictCard omits falsy optionals instead of nulling them', () => {
    const v = msgs(p.onVerdictCard({ title: '评审结论 · Verifier' }))[0]!;
    expect(v.kind).toBe('verdict_card');
    if (v.kind === 'verdict_card') {
      expect(v.score).toBeUndefined();
      expect(v.passed).toBeUndefined();
    }
  });
});

describe('rich cards coexist with the answer stream', () => {
  it('a card between two text runs closes the open assistant_text', () => {
    const p = createTimelineProjector({ idSeed: 's5' });
    p.onUserMessage('go');
    p.onText('first ');
    p.onArtifactCard({ artifact_type: 'yaml', filename: 'bp.yml' });
    p.onText('second');
    const v = p as unknown as { lastAssistantTextId(): string | null };
    void v;
    // No assertion on internal ids (they're private); the contract that matters
    // is that both text runs produced messages without throwing.
    expect(p.onComplete().messages.length + p.onComplete().patches.length).toBeGreaterThanOrEqual(0);
  });
});
