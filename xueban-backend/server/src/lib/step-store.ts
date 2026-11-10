/**
 * step-store.ts — Per-step artifact persistence for the intent-workflow pipeline.
 *
 * Spec: docs/design/intent-workflow-design-v1.md §4.4 (S2.3 / S4.1 / S4.2).
 *
 * One JSON file per (session_id, step_index) at
 *   <cwd>/.xueban/projects/<session_id>/steps/<step_index>.json
 *
 * Design echoes session-store.ts:
 *   - In-memory Map<string, Map<number, StepArtifact>> is the runtime truth.
 *   - set() double-writes (memory first, then async atomic file write).
 *   - Atomic writes via .tmp + rename so a crash never produces a half-written
 *     JSON that the retry/resume endpoints can't parse.
 *   - id whitelist (`/^[A-Za-z0-9_-]+$/`) guards against `../` path traversal.
 *   - step_index whitelist 0..99 (integer) — caps cardinality per session so a
 *     runaway parser bug can't fill the disk.
 *
 * NOT a database. The 1h session TTL upstream (run-sessions.ts) keeps total
 * cardinality bounded; if that ever changes, upgrade to better-sqlite3.
 */

import { promises as fs } from 'fs';
import * as fsSync from 'fs';
import * as path from 'path';
import type { StepArtifact } from './contracts';

const PROJECTS_ROOT = path.resolve(process.cwd(), '.xueban', 'projects');

const ID_RE = /^[A-Za-z0-9_-]+$/;
const MAX_STEP_INDEX = 99;

function validId(id: string): boolean {
  return typeof id === 'string' && ID_RE.test(id);
}

function validStepIndex(n: number): boolean {
  return Number.isInteger(n) && n >= 0 && n <= MAX_STEP_INDEX;
}

function sessionDir(sessionId: string): string {
  if (!validId(sessionId)) {
    throw new Error(`[step-store] invalid session id: ${sessionId}`);
  }
  return path.join(PROJECTS_ROOT, sessionId, 'steps');
}

function stepPath(sessionId: string, stepIndex: number): string {
  if (!validStepIndex(stepIndex)) {
    throw new Error(`[step-store] invalid step index: ${stepIndex}`);
  }
  return path.join(sessionDir(sessionId), `${stepIndex}.json`);
}

export interface StepStore {
  /** Persist (in-memory + disk) a step artifact for a session. */
  put(sessionId: string, stepIndex: number, artifact: StepArtifact): void;
  /** Get a single step artifact (in-memory). */
  get(sessionId: string, stepIndex: number): StepArtifact | undefined;
  /** List all known step artifacts for a session, sorted by step_index asc. */
  list(sessionId: string): StepArtifact[];
  /** Drop in-memory entries AND best-effort delete the on-disk directory. */
  clear(sessionId: string): void;
  /** Hydrate the in-memory map for one session from disk (idempotent). */
  loadSession(sessionId: string): Promise<void>;
}

export function createStepStore(): StepStore {
  const mem = new Map<string, Map<number, StepArtifact>>();

  function ensureSessionDirSync(sessionId: string): void {
    try {
      fsSync.mkdirSync(sessionDir(sessionId), { recursive: true });
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== 'EEXIST') {
        // eslint-disable-next-line no-console
        console.warn(
          `[step-store] mkdir failed for ${sessionId}: ${(err as Error).message}`,
        );
      }
    }
  }

  async function writeAtomic(
    sessionId: string,
    stepIndex: number,
    artifact: StepArtifact,
  ): Promise<void> {
    const fp = stepPath(sessionId, stepIndex);
    const tmp = `${fp}.tmp`;
    try {
      await fs.writeFile(tmp, JSON.stringify(artifact, null, 2), 'utf8');
      await fs.rename(tmp, fp);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(
        `[step-store] write ${sessionId}/${stepIndex} failed: ${(err as Error).message}`,
      );
    }
  }

  return {
    put(sessionId, stepIndex, artifact) {
      if (!validId(sessionId)) {
        // eslint-disable-next-line no-console
        console.warn(`[step-store] put rejected: invalid session id ${sessionId}`);
        return;
      }
      if (!validStepIndex(stepIndex)) {
        // eslint-disable-next-line no-console
        console.warn(`[step-store] put rejected: invalid step index ${stepIndex}`);
        return;
      }
      let bucket = mem.get(sessionId);
      if (!bucket) {
        bucket = new Map<number, StepArtifact>();
        mem.set(sessionId, bucket);
      }
      bucket.set(stepIndex, artifact);
      ensureSessionDirSync(sessionId);
      void writeAtomic(sessionId, stepIndex, artifact);
    },

    get(sessionId, stepIndex) {
      return mem.get(sessionId)?.get(stepIndex);
    },

    list(sessionId) {
      const bucket = mem.get(sessionId);
      if (!bucket) return [];
      return [...bucket.values()].sort((a, b) => a.step_index - b.step_index);
    },

    clear(sessionId) {
      mem.delete(sessionId);
      if (!validId(sessionId)) return;
      // Best-effort recursive delete; tolerate missing dir.
      const dir = path.join(PROJECTS_ROOT, sessionId, 'steps');
      void fs.rm(dir, { recursive: true, force: true }).catch((err) => {
        // eslint-disable-next-line no-console
        console.warn(
          `[step-store] clear ${sessionId} failed: ${(err as Error).message}`,
        );
      });
    },

    async loadSession(sessionId) {
      if (!validId(sessionId)) return;
      const dir = sessionDir(sessionId);
      let files: string[];
      try {
        files = await fs.readdir(dir);
      } catch (err) {
        const code = (err as NodeJS.ErrnoException).code;
        if (code !== 'ENOENT') {
          // eslint-disable-next-line no-console
          console.warn(
            `[step-store] readdir ${sessionId} failed: ${(err as Error).message}`,
          );
        }
        return;
      }
      let bucket = mem.get(sessionId);
      if (!bucket) {
        bucket = new Map<number, StepArtifact>();
        mem.set(sessionId, bucket);
      }
      for (const f of files) {
        if (!f.endsWith('.json') || f.endsWith('.tmp')) continue;
        const base = f.slice(0, -5);
        const idx = Number(base);
        if (!validStepIndex(idx)) continue;
        try {
          const text = await fs.readFile(path.join(dir, f), 'utf8');
          const parsed = JSON.parse(text) as StepArtifact;
          bucket.set(idx, parsed);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.warn(
            `[step-store] load ${sessionId}/${f} failed: ${(err as Error).message}`,
          );
        }
      }
    },
  };
}
