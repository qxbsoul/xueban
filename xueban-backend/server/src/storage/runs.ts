/**
 * runs.ts — sqlite persistence for RunRecord.
 *
 * Story 15.8 — original JSON file implementation.
 * Story 15.16 — internals migrated to sqlite. Public API (listRuns, saveRun,
 *               MAX_RUNS, _resetForTests) and types are byte-compatible with
 *               the JSON era so 15.8 callers and tests don't change.
 *
 * MAX_RUNS cap is preserved in saveRun(): after every insert we delete
 * everything older than the MAX_RUNS most recent rows.
 */

import fs from 'fs';
import path from 'path';
import { getDb, _resetForTests as _resetSqliteForTests } from './sqlite';

export const MAX_RUNS = 100;

export type ArtifactType = 'yaml' | 'html' | 'markdown';
export type RunStatus = 'completed' | 'failed';

export interface RunRecord {
  run_id: string;
  session_id: string;
  goal: string;
  skill_name: string;
  skill_display_name: string;
  artifact_type: ArtifactType | null;
  artifact_filename: string | null;
  artifact_url: string | null;
  status: RunStatus;
  created_at: string;
  completed_at: string;
  project_dir: string;
}

interface RunRow {
  run_id: string;
  session_id: string;
  project_id: string | null;
  conversation_id: string | null;
  goal: string;
  skill_name: string | null;
  skill_display_name: string | null;
  artifact_type: ArtifactType | null;
  artifact_filename: string | null;
  artifact_url: string | null;
  project_dir: string | null;
  status: string;
  created_at: string;
  completed_at: string;
}

function rowToRecord(r: RunRow): RunRecord {
  // Coerce status to the 15.8-public union — anything weird falls back to
  // 'completed' to keep the type system honest.
  const status: RunStatus =
    r.status === 'completed' || r.status === 'failed' ? r.status : 'completed';
  return {
    run_id: r.run_id,
    session_id: r.session_id,
    goal: r.goal,
    skill_name: r.skill_name ?? '',
    skill_display_name: r.skill_display_name ?? '',
    artifact_type: r.artifact_type,
    artifact_filename: r.artifact_filename,
    artifact_url: r.artifact_url,
    status,
    created_at: r.created_at,
    completed_at: r.completed_at,
    project_dir: r.project_dir ?? '',
  };
}

/**
 * listRuns — all persisted runs sorted by completed_at DESC (newest first).
 * Safe to call with an empty table (returns []).
 */
export function listRuns(): RunRecord[] {
  const rows = getDb()
    .prepare(`SELECT * FROM runs ORDER BY completed_at DESC`)
    .all() as RunRow[];
  return rows.map(rowToRecord);
}

/**
 * saveRun — UPSERT then trim to MAX_RUNS most recent rows.
 *
 * Uses INSERT OR REPLACE so re-saving the same run_id (rare but possible if
 * a callback fires twice) doesn't blow up. The trim step deletes everything
 * older than the MAX_RUNS-th newest row.
 *
 * `linkage` (optional) persists the project_id / conversation_id the run was
 * created under — this is what makes GET /api/projects/:id/artifacts (which
 * filters runs WHERE project_id = ?) return rows. NULL when unknown. Kept
 * out of RunRecord so the 15.8-era public record shape stays byte-compatible.
 */
export interface RunLinkage {
  project_id?: string | null;
  conversation_id?: string | null;
}

export function saveRun(record: RunRecord, linkage?: RunLinkage): void {
  const db = getDb();
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT OR REPLACE INTO runs
       (run_id, session_id, project_id, conversation_id, goal,
        skill_name, skill_display_name, artifact_type, artifact_filename,
        artifact_url, project_dir, status, created_at, completed_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      record.run_id,
      record.session_id,
      linkage?.project_id ?? null,
      linkage?.conversation_id ?? null,
      record.goal,
      record.skill_name,
      record.skill_display_name,
      record.artifact_type,
      record.artifact_filename,
      record.artifact_url,
      record.project_dir,
      record.status,
      record.created_at,
      record.completed_at,
    );

    // Trim: delete every row whose completed_at is older than the MAX_RUNS-th
    // newest. Sub-select picks the cutoff timestamp; if there are fewer than
    // MAX_RUNS rows the LIMIT/OFFSET yields no row → no-op.
    db.prepare(
      `DELETE FROM runs
       WHERE completed_at < (
         SELECT completed_at FROM runs ORDER BY completed_at DESC LIMIT 1 OFFSET ?
       )`,
    ).run(MAX_RUNS - 1);

    // 多产物登记（2026-11-07）— 清掉被 trim 掉的 run 留下的孤儿产物行
    // （run_artifacts 不挂 FK，需手动同步）。
    db.prepare(
      `DELETE FROM run_artifacts WHERE run_id NOT IN (SELECT run_id FROM runs)`,
    ).run();
  });
  tx();
}

// ── 多产物登记（2026-11-07）─────────────────────────────────────────────────
// run_artifacts 表的读写。动机见 storage/sqlite.ts 内 DDL 注释：
// runs 三字段只留「主产物」（最后一件，向后兼容），每件 blueprint 产物
// 在本表按捕获序独立登记。

/** 一件登记产物（不含 run_id/seq —— 由容器决定）。 */
export interface RunArtifact {
  artifact_type: ArtifactType | null;
  artifact_filename: string | null;
  artifact_url: string | null;
}

/**
 * saveRunArtifacts — 全量重登记（幂等）：先清空该 run 的旧行，再按传入序
 * （seq = 数组下标）逐件插入。重复调用（回调触发两次/重放）不会堆行。
 * 空数组 = 清空登记（合法：run 可能没有产出任何产物）。
 */
export function saveRunArtifacts(runId: string, artifacts: RunArtifact[]): void {
  const db = getDb();
  const tx = db.transaction(() => {
    db.prepare(`DELETE FROM run_artifacts WHERE run_id = ?`).run(runId);
    const ins = db.prepare(
      `INSERT INTO run_artifacts
         (run_id, seq, artifact_type, artifact_filename, artifact_url, created_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
    );
    const now = new Date().toISOString();
    artifacts.forEach((a, seq) => {
      ins.run(runId, seq, a.artifact_type, a.artifact_filename, a.artifact_url, now);
    });
  });
  tx();
}

/** listRunArtifacts — 该 run 登记的全部产物，按捕获序（seq ASC）。 */
export function listRunArtifacts(runId: string): RunArtifact[] {
  const rows = getDb()
    .prepare(
      `SELECT artifact_type, artifact_filename, artifact_url
       FROM run_artifacts WHERE run_id = ? ORDER BY seq ASC`,
    )
    .all(runId) as RunArtifact[];
  return rows;
}

/** listAllRunArtifacts — 全表按 run 分组（GET /api/runs 行内嵌 artifacts 用）。 */
export function listAllRunArtifacts(): Map<string, RunArtifact[]> {
  const rows = getDb()
    .prepare(
      `SELECT run_id, artifact_type, artifact_filename, artifact_url
       FROM run_artifacts ORDER BY run_id, seq ASC`,
    )
    .all() as Array<RunArtifact & { run_id: string }>;
  const byRun = new Map<string, RunArtifact[]>();
  for (const r of rows) {
    const list = byRun.get(r.run_id) ?? [];
    list.push({
      artifact_type: r.artifact_type,
      artifact_filename: r.artifact_filename,
      artifact_url: r.artifact_url,
    });
    byRun.set(r.run_id, list);
  }
  return byRun;
}

export function deleteRun(runId: string): boolean {
  const db = getDb();
  const tx = db.transaction(() => {
    db.prepare(`DELETE FROM run_artifacts WHERE run_id = ?`).run(runId);
    const result = db.prepare(`DELETE FROM runs WHERE run_id = ?`).run(runId);
    return result.changes > 0;
  });
  return tx() as boolean;
}

/**
 * Test-only: drop the sqlite handle AND remove any db file in the current
 * cwd's .xueban/ dir.
 *
 * The legacy 15.8 test pattern (see runs.test.ts) chdirs into a fresh tmp
 * dir per test and calls _resetForTests() before/after. To make that pattern
 * work after the sqlite migration we need to (a) close the connection so
 * the next getDb() opens a fresh file in the new cwd, and (b) wipe any
 * leftover sqlite + JSON files so each test really starts empty.
 */
export function _resetForTests(): void {
  _resetSqliteForTests();
  const dir = path.join(process.cwd(), '.xueban');
  for (const name of [
    'app.sqlite',
    'app.sqlite-wal',
    'app.sqlite-shm',
    'app.sqlite-journal',
    'runs.json',
  ]) {
    const p = path.join(dir, name);
    if (fs.existsSync(p)) {
      try {
        fs.unlinkSync(p);
      } catch {
        /* ignore */
      }
    }
  }
}
