/**
 * sqlite.test.ts — Story 15.16 — smoke test for the sqlite singleton.
 *
 * Run:  npx tsx src/storage/sqlite.test.ts   (from server/)
 *
 * Asserts:
 *   - getDb() creates the db file under .xueban/ in cwd.
 *   - WAL mode and FK constraint are enabled.
 *   - All 5 tables exist after init (idempotent rerun is a no-op).
 *   - FK CASCADE on conversations works.
 *   - FK SET NULL on runs.project_id works.
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
  fn: (mod: typeof import('./sqlite')) => Promise<void> | void,
): Promise<void> {
  const orig = process.cwd();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-sqlite-test-'));
  process.chdir(tmp);
  try {
    const mod = await import('./sqlite');
    mod._resetForTests();
    await fn(mod);
    mod._resetForTests();
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
  console.log('\n[1] db file creation + pragmas');
  await inIsolated(async (m) => {
    const db = m.getDb();
    check(
      'app.sqlite file exists',
      fs.existsSync(path.join(process.cwd(), '.xueban', 'app.sqlite')),
    );
    const journal = (
      db.pragma('journal_mode', { simple: true }) as string
    ).toLowerCase();
    check('journal_mode = wal', journal === 'wal', journal);
    const fk = db.pragma('foreign_keys', { simple: true }) as number;
    check('foreign_keys = 1', fk === 1, fk);
  });

  console.log('\n[2] all 5 tables present');
  await inIsolated(async (m) => {
    const db = m.getDb();
    const rows = db
      .prepare(`SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`)
      .all() as { name: string }[];
    const names = rows.map((r) => r.name);
    for (const t of ['agents', 'conversations', 'messages', 'projects', 'runs']) {
      check(`table ${t} exists`, names.includes(t), names);
    }
  });

  console.log('\n[3] idempotent rerun');
  await inIsolated(async (m) => {
    m.getDb(); // first init
    // Re-running getDb without close should be a no-op (cached handle).
    const db2 = m.getDb();
    const tableCount = (
      db2
        .prepare(`SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'`)
        .get() as { n: number }
    ).n;
    check('table count stable on re-getDb', tableCount >= 5, tableCount);
  });

  console.log('\n[4] FK CASCADE: deleting project removes conversations');
  await inIsolated(async (m) => {
    const db = m.getDb();
    db.prepare(
      `INSERT INTO projects (project_id, name, workspace_path, created_at, updated_at)
       VALUES ('p1','Test','/tmp/p1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
    ).run();
    db.prepare(
      `INSERT INTO conversations (conversation_id, project_id, title, created_at, updated_at)
       VALUES ('c1','p1','Main','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
    ).run();
    db.prepare(`DELETE FROM projects WHERE project_id='p1'`).run();
    const remaining = (
      db
        .prepare(`SELECT COUNT(*) AS n FROM conversations WHERE conversation_id='c1'`)
        .get() as { n: number }
    ).n;
    check('conversation cascaded out on project delete', remaining === 0, remaining);
  });

  console.log('\n[5] FK SET NULL: deleting project nulls runs.project_id');
  await inIsolated(async (m) => {
    const db = m.getDb();
    db.prepare(
      `INSERT INTO projects (project_id, name, workspace_path, created_at, updated_at)
       VALUES ('p2','P2','/tmp/p2','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
    ).run();
    db.prepare(
      `INSERT INTO runs (run_id, session_id, project_id, goal, status, created_at, completed_at)
       VALUES ('r1','s1','p2','demo','completed','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
    ).run();
    db.prepare(`DELETE FROM projects WHERE project_id='p2'`).run();
    const r = db.prepare(`SELECT project_id FROM runs WHERE run_id='r1'`).get() as
      | { project_id: string | null }
      | undefined;
    check('run survived', !!r);
    check('run.project_id set NULL', r?.project_id === null, r?.project_id);
  });

  console.log('\n[6] O3 — fresh db: messages.timeline_message_id column present');
  await inIsolated(async (m) => {
    const db = m.getDb();
    const cols = db.prepare(`PRAGMA table_info(messages)`).all() as { name: string }[];
    check(
      'messages has timeline_message_id',
      cols.some((c) => c.name === 'timeline_message_id'),
      cols.map((c) => c.name),
    );
  });

  console.log('\n[7] O3 — idempotent ALTER upgrades a legacy db missing the column');
  {
    // Simulate a pre-O3 db: a messages table WITHOUT timeline_message_id.
    const orig = process.cwd();
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-sqlite-legacy-'));
    process.chdir(tmp);
    try {
      const dir = path.join(tmp, '.xueban');
      fs.mkdirSync(dir, { recursive: true });
      const Database = (await import('better-sqlite3')).default;
      const legacyFile = path.join(dir, 'app.sqlite');
      const legacy = new Database(legacyFile);
      // Old messages schema (no timeline_message_id). Needs the FK target table.
      legacy.exec(`
        CREATE TABLE conversations (
          conversation_id TEXT PRIMARY KEY,
          project_id      TEXT NOT NULL,
          title           TEXT,
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL
        );
        CREATE TABLE messages (
          message_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          run_id TEXT,
          created_at TEXT NOT NULL
        );
      `);
      const beforeCols = (legacy.prepare(`PRAGMA table_info(messages)`).all() as { name: string }[]).map((c) => c.name);
      check('legacy db lacks the column pre-migration', !beforeCols.includes('timeline_message_id'), beforeCols);
      legacy.close();

      const m = await import('./sqlite');
      m._resetForTests();
      const db = m.getDb(); // runs DDL (no-op CREATE IF NOT EXISTS) + addColumnIfMissing
      const afterCols = (db.prepare(`PRAGMA table_info(messages)`).all() as { name: string }[]).map((c) => c.name);
      check('column added to legacy db after migration', afterCols.includes('timeline_message_id'), afterCols);
      // Re-running getDb (idempotent) must not error / double-add.
      m._resetForTests();
      const db2 = m.getDb();
      const reCols = (db2.prepare(`PRAGMA table_info(messages)`).all() as { name: string }[]).filter((c) => c.name === 'timeline_message_id');
      check('column present exactly once after second migration', reCols.length === 1, reCols.length);
      m._resetForTests();
    } finally {
      process.chdir(orig);
      try {
        fs.rmSync(tmp, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
  }

  console.log('\n────────────────────────────────────────');
  console.log(`  ${pass} passed,  ${fail} failed`);
  console.log('────────────────────────────────────────\n');
  if (fail > 0) process.exit(1);
}

main().catch((e) => {
  console.error('test crashed:', e);
  process.exit(1);
});
