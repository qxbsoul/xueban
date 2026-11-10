/**
 * runs.test.ts — standalone smoke test for storage/runs.ts (Story 15.8)
 *
 * Run with:  npx tsx src/storage/runs.test.ts   (from server/)
 *
 * No external test framework — vitest/jest are not yet installed in the
 * server package. Each `check` prints PASS or FAIL and increments counters;
 * the process exits non-zero if any check fails.
 *
 * Coverage:
 *   - saveRun() persists to disk and listRuns() reads it back
 *   - listRuns() returns DESC order by completed_at
 *   - MAX_RUNS cap drops oldest entries
 *   - Empty state (file missing) returns []
 *   - Corrupt file falls back to []
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

let passCount = 0;
let failCount = 0;

function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    passCount++;
    console.log(`  PASS  ${label}`);
  } else {
    failCount++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

// Run each test in its own tmp cwd so tests don't trample each other.
function inIsolatedCwd(fn: (storage: typeof import('./runs')) => void | Promise<void>): Promise<void> {
  // async 逻辑放在内部函数里，executor 本身保持同步 —— async executor 抛出的
  // 拒绝会被 Promise 吞掉（no-async-promise-executor），这里显式 return。
  const run = async (): Promise<void> => {
    const originalCwd = process.cwd();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-runs-test-'));
    process.chdir(tmpDir);

    try {
      // Re-import storage so process.cwd() is captured fresh — note that
      // storage/runs.ts reads cwd lazily via storagePaths(), so a single
      // import is fine. Use a cache-busting query string so each isolated
      // test gets the same module but recomputes paths.
      const mod = await import('./runs');
      mod._resetForTests();
      await fn(mod);
      mod._resetForTests();
    } finally {
      process.chdir(originalCwd);
      try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  };

  return run();
}

function makeRecord(overrides: Partial<import('./runs').RunRecord> = {}): import('./runs').RunRecord {
  return {
    run_id: 'run-test123',
    session_id: 'sess-test12345',
    goal: 'demo goal',
    skill_name: 'agent-team-blueprint',
    skill_display_name: 'Agent Team Blueprint',
    artifact_type: 'yaml',
    artifact_filename: 'team_blueprint.yml',
    artifact_url: '/projects/sess-test12345/team_blueprint.yml',
    status: 'completed',
    created_at: '2026-05-10T10:00:00Z',
    completed_at: '2026-05-10T10:00:30Z',
    project_dir: '.xueban/projects/sess-test12345',
    ...overrides,
  };
}

async function main() {
  // ── Test 1: empty when file missing ─────────────────────────────────────
  console.log('\n[1] empty store');
  await inIsolatedCwd((s) => {
    const runs = s.listRuns();
    check('listRuns() returns [] when no file', Array.isArray(runs) && runs.length === 0);
  });

  // ── Test 2: saveRun + listRuns roundtrip ────────────────────────────────
  console.log('\n[2] saveRun roundtrip');
  await inIsolatedCwd((s) => {
    const r = makeRecord();
    s.saveRun(r);
    const runs = s.listRuns();
    check('listRuns() length=1 after save', runs.length === 1);
    check('record.run_id roundtrips', runs[0].run_id === 'run-test123');
    check('record.goal roundtrips', runs[0].goal === 'demo goal');
    check('record.artifact_url roundtrips', runs[0].artifact_url === '/projects/sess-test12345/team_blueprint.yml');
    // Story 15.16: storage moved from runs.json to app.sqlite. Assertion
    // updated to check the new file path; old runs.json is migrated/backed
    // up by scripts/migrate-json-to-sqlite.ts.
    check('db file exists at .xueban/app.sqlite',
      fs.existsSync(path.join(process.cwd(), '.xueban', 'app.sqlite')));
  });

  // ── Test 3: DESC order by completed_at ──────────────────────────────────
  console.log('\n[3] sort order');
  await inIsolatedCwd((s) => {
    s.saveRun(makeRecord({ run_id: 'run-old', completed_at: '2026-05-01T00:00:00Z' }));
    s.saveRun(makeRecord({ run_id: 'run-mid', completed_at: '2026-05-05T00:00:00Z' }));
    s.saveRun(makeRecord({ run_id: 'run-new', completed_at: '2026-05-10T00:00:00Z' }));
    const runs = s.listRuns();
    check('3 records persisted', runs.length === 3);
    check('newest first', runs[0].run_id === 'run-new', runs.map(r => r.run_id));
    check('mid second', runs[1].run_id === 'run-mid');
    check('oldest last', runs[2].run_id === 'run-old');
  });

  // ── Test 4: MAX_RUNS cap (drops oldest) ─────────────────────────────────
  console.log('\n[4] MAX_RUNS cap');
  await inIsolatedCwd((s) => {
    // Fill to MAX_RUNS+5 with monotonically increasing timestamps
    const N = s.MAX_RUNS + 5;
    for (let i = 0; i < N; i++) {
      const ts = new Date(2026, 0, 1, 0, 0, i).toISOString();
      s.saveRun(makeRecord({
        run_id: `run-${i}`,
        completed_at: ts,
      }));
    }
    const runs = s.listRuns();
    check(`length capped at MAX_RUNS=${s.MAX_RUNS}`, runs.length === s.MAX_RUNS, runs.length);
    // Oldest entries (run-0 .. run-4) should be evicted
    check('run-0 evicted (oldest)', !runs.some(r => r.run_id === 'run-0'));
    check('run-4 evicted', !runs.some(r => r.run_id === 'run-4'));
    // Newest entry preserved
    check(`run-${N - 1} preserved (newest)`, runs.some(r => r.run_id === `run-${N - 1}`));
  });

  // ── Test 5: failed-status records persist ───────────────────────────────
  console.log('\n[5] failed status');
  await inIsolatedCwd((s) => {
    s.saveRun(makeRecord({
      run_id: 'run-failed',
      status: 'failed',
      artifact_type: null,
      artifact_filename: null,
      artifact_url: null,
    }));
    const runs = s.listRuns();
    check('failed record persisted', runs.length === 1 && runs[0].status === 'failed');
    check('artifact fields nullable', runs[0].artifact_url === null && runs[0].artifact_type === null);
  });

  // ── Test 6: corrupt file falls back to [] ───────────────────────────────
  console.log('\n[6] corrupt file recovery');
  await inIsolatedCwd((s) => {
    const dir = path.join(process.cwd(), '.xueban');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'runs.json'), '{ this is not valid json', 'utf-8');
    const runs = s.listRuns();
    check('listRuns() returns [] on parse error', Array.isArray(runs) && runs.length === 0);
    // saveRun still works — overwrites the corrupt file
    s.saveRun(makeRecord());
    const after = s.listRuns();
    check('saveRun recovers from corrupt file', after.length === 1);
  });

  // ── Test 7: project_id / conversation_id linkage persists ───────────────
  console.log('\n[7] linkage persistence');
  await inIsolatedCwd(async (s) => {
    // runs.project_id / conversation_id 是 FK（REFERENCES projects/conversations
    // ON DELETE SET NULL），先建行再挂链；生产路径的 id 同样来自已校验/已建行。
    const { getOrCreateProject } = await import('./projects');
    const { createConversation } = await import('./conversations');
    const project = getOrCreateProject('proj-abc', 'Linkage Test');
    const conv = createConversation(project.project_id);
    s.saveRun(makeRecord({ run_id: 'run-linked' }), {
      project_id: project.project_id,
      conversation_id: conv.conversation_id,
    });
    s.saveRun(makeRecord({ run_id: 'run-unlinked' }));
    const { getDb } = await import('./sqlite');
    const linked = getDb()
      .prepare(`SELECT project_id, conversation_id FROM runs WHERE run_id = ?`)
      .get('run-linked') as { project_id: string | null; conversation_id: string | null };
    check(
      'linked run persists project_id',
      linked.project_id === project.project_id,
      linked,
    );
    check(
      'linked run persists conversation_id',
      linked.conversation_id === conv.conversation_id,
      linked,
    );
    const unlinked = getDb()
      .prepare(`SELECT project_id, conversation_id FROM runs WHERE run_id = ?`)
      .get('run-unlinked') as { project_id: string | null; conversation_id: string | null };
    check(
      'omitted linkage stores NULL',
      unlinked.project_id === null && unlinked.conversation_id === null,
      unlinked,
    );
  });

  // ── Summary ─────────────────────────────────────────────────────────────
  console.log('\n────────────────────────────────────────');
  console.log(`  ${passCount} passed,  ${failCount} failed`);
  console.log('────────────────────────────────────────\n');

  if (failCount > 0) process.exit(1);
}

main().catch((err) => {
  console.error('test runner crashed:', err);
  process.exit(1);
});
