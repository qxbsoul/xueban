/**
 * migrate-json-to-sqlite.ts — Story 15.16
 *
 * Run from server/:    npx tsx ../scripts/migrate-json-to-sqlite.ts
 * Or from repo root:   cd server && npx tsx ../scripts/migrate-json-to-sqlite.ts
 *
 * One-shot, idempotent migration of the legacy JSON stores
 * (.xueban/agents.json, .xueban/runs.json) into the new sqlite db.
 *
 * Idempotency: every INSERT uses INSERT OR IGNORE on the primary key, so a
 * second invocation is a no-op. Originals are renamed to .bak.<timestamp>
 * after a successful import — never deleted, never overwritten.
 *
 * The script must run from the server/ working directory because (a) the
 * server package.json is CommonJS (root is "type":"module") and (b) the
 * .xueban/ dir layout assumes cwd-relative paths.
 *
 * Output: a JSON line summarising the run, e.g.
 *   {"agents_migrated":3,"runs_migrated":12,"agents_backup":"…","ts":"…"}
 */

import fs from 'fs';
import path from 'path';
import { getDb } from '../server/src/storage/sqlite';

interface MigrationReport {
  agents_migrated: number;
  runs_migrated: number;
  agents_backup: string | null;
  runs_backup: string | null;
  no_op: boolean;
  ts: string;
}

const STORAGE_DIR = path.join(process.cwd(), '.xueban');

function backup(file: string): string | null {
  if (!fs.existsSync(file)) return null;
  const ts = Date.now();
  const dst = `${file}.bak.${ts}`;
  fs.renameSync(file, dst);
  return dst;
}

function migrateAgents(db: ReturnType<typeof getDb>): {
  count: number;
  backup: string | null;
} {
  const file = path.join(STORAGE_DIR, 'agents.json');
  if (!fs.existsSync(file)) return { count: 0, backup: null };

  let arr: unknown;
  try {
    arr = JSON.parse(fs.readFileSync(file, 'utf-8'));
  } catch (e) {
    console.warn(`[migrate] skipping agents.json — parse failed: ${(e as Error).message}`);
    return { count: 0, backup: null };
  }
  if (!Array.isArray(arr)) return { count: 0, backup: null };

  const stmt = db.prepare(
    `INSERT OR IGNORE INTO agents
     (agent_id, name, soul, workspace_id, blueprint, status, source, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  );

  let inserted = 0;
  const insertAll = db.transaction(() => {
    for (const a of arr as Array<Record<string, unknown>>) {
      const info = stmt.run(
        String(a.agent_id ?? ''),
        String(a.name ?? ''),
        String(a.soul ?? ''),
        String(a.workspace_id ?? 'default'),
        JSON.stringify(a.blueprint ?? {}),
        String(a.status ?? 'idle'),
        String(a.source ?? 'quick_hire'),
        String(a.created_at ?? new Date().toISOString()),
      );
      if (info.changes > 0) inserted++;
    }
  });
  insertAll();

  return { count: inserted, backup: backup(file) };
}

function migrateRuns(db: ReturnType<typeof getDb>): {
  count: number;
  backup: string | null;
} {
  const file = path.join(STORAGE_DIR, 'runs.json');
  if (!fs.existsSync(file)) return { count: 0, backup: null };

  let arr: unknown;
  try {
    arr = JSON.parse(fs.readFileSync(file, 'utf-8'));
  } catch (e) {
    console.warn(`[migrate] skipping runs.json — parse failed: ${(e as Error).message}`);
    return { count: 0, backup: null };
  }
  if (!Array.isArray(arr)) return { count: 0, backup: null };

  const stmt = db.prepare(
    `INSERT OR IGNORE INTO runs
     (run_id, session_id, project_id, conversation_id, goal,
      skill_name, skill_display_name, artifact_type, artifact_filename,
      artifact_url, project_dir, status, created_at, completed_at)
     VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );

  let inserted = 0;
  const insertAll = db.transaction(() => {
    for (const r of arr as Array<Record<string, unknown>>) {
      const info = stmt.run(
        String(r.run_id ?? ''),
        String(r.session_id ?? ''),
        String(r.goal ?? ''),
        r.skill_name == null ? null : String(r.skill_name),
        r.skill_display_name == null ? null : String(r.skill_display_name),
        r.artifact_type == null ? null : String(r.artifact_type),
        r.artifact_filename == null ? null : String(r.artifact_filename),
        r.artifact_url == null ? null : String(r.artifact_url),
        r.project_dir == null ? null : String(r.project_dir),
        String(r.status ?? 'completed'),
        String(r.created_at ?? new Date().toISOString()),
        String(r.completed_at ?? new Date().toISOString()),
      );
      if (info.changes > 0) inserted++;
    }
  });
  insertAll();

  return { count: inserted, backup: backup(file) };
}

function main(): MigrationReport {
  fs.mkdirSync(STORAGE_DIR, { recursive: true });
  const db = getDb();

  const a = migrateAgents(db);
  const r = migrateRuns(db);

  const report: MigrationReport = {
    agents_migrated: a.count,
    runs_migrated: r.count,
    agents_backup: a.backup,
    runs_backup: r.backup,
    no_op: a.count === 0 && r.count === 0 && !a.backup && !r.backup,
    ts: new Date().toISOString(),
  };
  console.log(JSON.stringify(report));
  return report;
}

// Allow imports for testing without auto-running.
if (require.main === module) {
  try {
    main();
  } catch (e) {
    console.error('[migrate] failed:', (e as Error).message);
    process.exit(1);
  }
}

export { main as runMigration };
