/**
 * run-event-store.ts — O2 / T3-5 (run-session-backend-optimization-audit-2026-06-01.md).
 *
 * Disk persistence for the run-event-bus, mirroring session-store.ts. Closes the
 * last durability gap: SessionRecord + step artifacts already survive a Node
 * restart, but the run TIMELINE (the buffered event log in run-event-bus) was
 * in-memory only — so after a restart, reconnecting to a session re-ran the
 * whole pipeline (wasted tokens) instead of replaying history. This persists
 * each run's snapshot so the bus can hydrate it at startup and serve it as a
 * read-only terminal entity.
 *
 * Design (session-store parity):
 *   - One JSON file per run at `<cwd>/.xueban/runs/<id>.json`.
 *   - save() is DEBOUNCED per run (coalesce bursts of emit() into one write,
 *     ~250ms) so a chatty stream doesn't write-amplify; finish/remove flush
 *     immediately. In-memory bus state is the runtime truth; disk is recovery.
 *   - Atomic writes: `<id>.json.tmp` then rename — never a half-written file.
 *   - loadAllSync() at construction so the bus can hydrate before the first
 *     request. Files are small (≤ maxEvents records) and few (bounded by the
 *     session TTL), so a sync read at boot is fine.
 *
 * NOT a database. Same trust boundary as session-store (user-machine local,
 * gitignored). If run cardinality balloons, upgrade to better-sqlite3.
 */

import { promises as fs } from 'fs';
import * as fsSync from 'fs';
import * as path from 'path';
import type { RunSnapshot } from './run-event-bus';

const RUNS_DIR = path.resolve(process.cwd(), '.xueban', 'runs');

export interface RunEventStore {
  /** Persist a run snapshot (debounced). */
  save(snap: RunSnapshot): void;
  /** Remove a run's persisted snapshot (immediate, cancels any pending write). */
  remove(id: string): void;
  /** Load every persisted snapshot synchronously (called once at bus construction). */
  loadAllSync(): RunSnapshot[];
  /** O4 — load a single persisted snapshot by id, or undefined if absent. */
  loadOneSync(id: string): RunSnapshot | undefined;
}

export interface RunEventStoreOptions {
  /** Debounce window for save() in ms (default 250). 0 = write synchronously-ish (still async fs). */
  debounceMs?: number;
}

function validId(id: string): boolean {
  return /^[A-Za-z0-9_-]+$/.test(id);
}

export function createRunEventStore(opts: RunEventStoreOptions = {}): RunEventStore {
  const debounceMs = opts.debounceMs ?? 250;

  try {
    fsSync.mkdirSync(RUNS_DIR, { recursive: true });
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
      // eslint-disable-next-line no-console
      console.warn(`[run-event-store] mkdir failed (degrading to in-memory only): ${(err as Error).message}`);
    }
  }

  // Latest pending snapshot per run + its debounce timer. We keep the LATEST
  // snapshot reference so a coalesced write persists the most recent state.
  const pending = new Map<string, RunSnapshot>();
  const timers = new Map<string, NodeJS.Timeout>();

  function runPath(id: string): string {
    if (!validId(id)) throw new Error(`[run-event-store] invalid run id: ${id}`);
    return path.join(RUNS_DIR, `${id}.json`);
  }

  async function flush(id: string): Promise<void> {
    const snap = pending.get(id);
    pending.delete(id);
    timers.delete(id);
    if (!snap) return;
    let fp: string;
    try {
      fp = runPath(id);
    } catch {
      return; // invalid id — never persist
    }
    const tmp = `${fp}.tmp`;
    try {
      await fs.writeFile(tmp, JSON.stringify(snap), 'utf8');
      await fs.rename(tmp, fp);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(`[run-event-store] write ${id} failed: ${(err as Error).message}`);
    }
  }

  return {
    save(snap: RunSnapshot): void {
      if (!validId(snap.id)) return;
      pending.set(snap.id, snap);
      if (timers.has(snap.id)) return; // a flush is already scheduled
      const t = setTimeout(() => void flush(snap.id), debounceMs);
      (t as { unref?: () => void }).unref?.();
      timers.set(snap.id, t);
    },

    remove(id: string): void {
      const t = timers.get(id);
      if (t) clearTimeout(t);
      timers.delete(id);
      pending.delete(id);
      if (!validId(id)) return;
      // Fire-and-forget unlink; ENOENT is fine.
      void fs.unlink(path.join(RUNS_DIR, `${id}.json`)).catch((err) => {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
          // eslint-disable-next-line no-console
          console.warn(`[run-event-store] unlink ${id} failed: ${(err as Error).message}`);
        }
      });
    },

    loadAllSync(): RunSnapshot[] {
      let files: string[];
      try {
        files = fsSync.readdirSync(RUNS_DIR);
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
          // eslint-disable-next-line no-console
          console.warn(`[run-event-store] readdir failed: ${(err as Error).message}`);
        }
        return [];
      }
      const out: RunSnapshot[] = [];
      for (const f of files) {
        if (!f.endsWith('.json') || f.endsWith('.tmp')) continue;
        const id = f.slice(0, -5);
        if (!validId(id)) continue;
        try {
          const text = fsSync.readFileSync(path.join(RUNS_DIR, f), 'utf8');
          const snap = JSON.parse(text) as RunSnapshot;
          if (snap && typeof snap === 'object' && snap.id === id && Array.isArray(snap.events)) {
            out.push(snap);
          }
        } catch (err) {
          // eslint-disable-next-line no-console
          console.warn(`[run-event-store] load ${f} failed: ${(err as Error).message}`);
        }
      }
      return out;
    },

    loadOneSync(id: string): RunSnapshot | undefined {
      if (!validId(id)) return undefined;
      // Prefer the latest pending (not-yet-flushed) snapshot if one is queued,
      // so a lazy recovery sees the freshest state rather than a stale file.
      const queued = pending.get(id);
      if (queued) return queued;
      let text: string;
      try {
        text = fsSync.readFileSync(path.join(RUNS_DIR, `${id}.json`), 'utf8');
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
          // eslint-disable-next-line no-console
          console.warn(`[run-event-store] loadOne ${id} failed: ${(err as Error).message}`);
        }
        return undefined;
      }
      try {
        const snap = JSON.parse(text) as RunSnapshot;
        if (snap && typeof snap === 'object' && snap.id === id && Array.isArray(snap.events)) {
          return snap;
        }
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn(`[run-event-store] loadOne parse ${id} failed: ${(err as Error).message}`);
      }
      return undefined;
    },
  };
}
