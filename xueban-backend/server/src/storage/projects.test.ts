/**
 * projects.test.ts — Story 15.16 — Project CRUD smoke test.
 *
 * Run:  npx tsx src/storage/projects.test.ts   (from server/)
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

async function inIsolated(
  fn: (mods: {
    projects: typeof import('./projects');
    sqlite: typeof import('./sqlite');
  }) => Promise<void> | void,
): Promise<void> {
  const orig = process.cwd();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-projects-test-'));
  process.chdir(tmp);
  try {
    const sqlite = await import('./sqlite');
    sqlite._resetForTests();
    const projects = await import('./projects');
    await fn({ projects, sqlite });
    sqlite._resetForTests();
  } finally {
    process.chdir(orig);
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
}

async function main() {
  console.log('\n[1] empty list');
  await inIsolated(async ({ projects }) => {
    const list = projects.listProjects();
    check('listProjects() === []', Array.isArray(list) && list.length === 0);
  });

  console.log('\n[2] create + get + list');
  await inIsolated(async ({ projects }) => {
    const p = projects.createProject({ name: 'demo', workspace_path: '/tmp/demo' });
    check('project_id is uuid-ish', /^[0-9a-f-]{36}$/i.test(p.project_id), p.project_id);
    check('name persisted', p.name === 'demo');
    check('workspace_path passes through', p.workspace_path === '/tmp/demo');
    check('created_at iso', /T.*Z$/.test(p.created_at), p.created_at);

    const got = projects.getProject(p.project_id);
    check('getProject returns row', got?.project_id === p.project_id);

    const list = projects.listProjects();
    check('list has 1', list.length === 1);
  });

  console.log('\n[3] default workspace_path under .xueban/projects');
  await inIsolated(async ({ projects }) => {
    const p = projects.createProject({ name: 'auto-cwd' });
    check(
      'workspace_path defaults under .xueban/projects/<id>',
      p.workspace_path.includes('.xueban') &&
        p.workspace_path.endsWith(p.project_id),
      p.workspace_path,
    );
  });

  console.log('\n[4] update');
  await inIsolated(async ({ projects }) => {
    const p = projects.createProject({ name: 'a' });
    // Brief sleep guarantees updated_at moves forward (ISO ms resolution).
    await new Promise((r) => setTimeout(r, 5));
    const updated = projects.updateProject(p.project_id, {
      name: 'a-new',
      skill_id: 'agent-team-blueprint',
    });
    check('update returns row', !!updated);
    check('name updated', updated?.name === 'a-new');
    check('skill_id updated', updated?.skill_id === 'agent-team-blueprint');
    check(
      'updated_at moved forward',
      (updated?.updated_at ?? '') > p.updated_at,
      { before: p.updated_at, after: updated?.updated_at },
    );
  });

  console.log('\n[5] delete + cascade');
  await inIsolated(async ({ projects, sqlite }) => {
    const db = sqlite.getDb();
    const p = projects.createProject({ name: 'doomed' });
    db.prepare(
      `INSERT INTO conversations (conversation_id, project_id, title, created_at, updated_at)
       VALUES ('cz', ?, 'main', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')`,
    ).run(p.project_id);

    const ok = projects.deleteProject(p.project_id);
    check('deleteProject returns true', ok);
    check('project gone', projects.getProject(p.project_id) === null);

    const conv = (
      db
        .prepare(`SELECT COUNT(*) AS n FROM conversations WHERE conversation_id='cz'`)
        .get() as { n: number }
    ).n;
    check('conversation cascaded', conv === 0);
  });

  console.log('\n[6] delete missing returns false');
  await inIsolated(async ({ projects }) => {
    check(
      'delete unknown returns false',
      projects.deleteProject('not-a-real-id') === false,
    );
  });

  console.log('\n────────────────────────────────────────');
  console.log(`  ${pass} passed,  ${fail} failed`);
  console.log('────────────────────────────────────────\n');
  if (fail > 0) process.exit(1);
}

main().catch((e) => {
  console.error('test crashed:', e);
  process.exit(1);
});
