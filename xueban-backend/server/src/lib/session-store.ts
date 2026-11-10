/**
 * session-store.ts — JSON-file-backed sessionStore for run sessions.
 *
 * Replaces the previous in-memory `Map<string, SessionRecord>()` that lost
 * every active session on backend restart. Symptom that prompted this:
 * 2026-05-19 — backend restart → frontend EventSource hits 404, retries 5
 * times, surfaces "已达最大重试次数" alert.
 *
 * Design (Cherry Studio parity, but server-side):
 *   - One JSON file per session at `<repo>/server/.xueban/sessions/<id>.json`
 *   - In-memory Map is the source of truth at runtime; disk is the recovery
 *     log. Reads never hit disk after `loadAll()` on startup.
 *   - set / delete double-write: in-memory first, then async file write. If
 *     disk fails, log + keep going — memory state is still correct, recovery
 *     just won't survive a restart.
 *   - Atomic writes: write to `<id>.json.tmp` then rename. Cheap on local FS,
 *     prevents partial-read on concurrent restart.
 *   - Sensitive fields (`api_key`, `anthropic_key`) are persisted. Same trust
 *     boundary as `.xueban/settings.json` (user-machine local, gitignored).
 *
 * NOT a database. If session count balloons (>1000) or write rate spikes,
 * upgrade to better-sqlite3. For now the 1h TTL keeps cardinality bounded.
 */

import { promises as fs } from 'fs';
import * as path from 'path';

const SESSIONS_DIR = path.resolve(process.cwd(), '.xueban', 'sessions');

/**
 * Ensure the sessions directory exists. Called once at module load.
 * Synchronous because everything else depends on it being writable.
 */
function ensureDir(): void {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    require('fs').mkdirSync(SESSIONS_DIR, { recursive: true });
  } catch (err) {
    // EEXIST is fine; anything else logs and degrades to memory-only mode.
    if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
      // eslint-disable-next-line no-console
      console.warn(`[session-store] mkdir failed (degrading to in-memory only): ${(err as Error).message}`);
    }
  }
}

ensureDir();

export interface PersistentSessionStore<T> {
  get(id: string): T | undefined;
  set(id: string, record: T): void;
  delete(id: string): void;
  has(id: string): boolean;
  entries(): IterableIterator<[string, T]>;
  /** Hydrate the in-memory map from disk. Call once at module load. */
  loadAll(): Promise<void>;
}

/**
 * Options for createSessionStore.
 *
 * `migrate` runs on every JSON record read from disk in loadAll(). Use it to
 * normalize old on-disk shapes into the current T. Return the (possibly
 * mutated) object; throw or return undefined to skip that record.
 *
 * S1 (D8, skill-team-conversion-design-v1.md §6) — SessionRecord gained
 * `messages: ConversationMessage[]` + `version: 1`. Sessions written before
 * S1 lack both fields; we inject `messages = []` and bump `version` 0 → 1 so
 * downstream code (ConversationRuntime, history replay) can assume the shape.
 */
export interface SessionStoreOptions<T> {
  migrate?: (raw: unknown) => T | undefined;
}

export function createSessionStore<T extends object>(
  opts: SessionStoreOptions<T> = {},
): PersistentSessionStore<T> {
  const mem = new Map<string, T>();

  function sessionPath(id: string): string {
    // Defensive: id is uuid in our pipeline but reject any path traversal.
    if (!/^[A-Za-z0-9_-]+$/.test(id)) {
      throw new Error(`[session-store] invalid session id: ${id}`);
    }
    return path.join(SESSIONS_DIR, `${id}.json`);
  }

  async function writeFile(id: string, record: T): Promise<void> {
    const fp = sessionPath(id);
    const tmp = `${fp}.tmp`;
    try {
      await fs.writeFile(tmp, JSON.stringify(record, null, 2), 'utf8');
      await fs.rename(tmp, fp);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(`[session-store] write ${id} failed: ${(err as Error).message}`);
    }
  }

  async function unlinkFile(id: string): Promise<void> {
    try {
      await fs.unlink(sessionPath(id));
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== 'ENOENT') {
        // eslint-disable-next-line no-console
        console.warn(`[session-store] unlink ${id} failed: ${(err as Error).message}`);
      }
    }
  }

  return {
    get: (id) => mem.get(id),
    has: (id) => mem.has(id),
    entries: () => mem.entries(),
    set: (id, record) => {
      mem.set(id, record);
      // Fire-and-forget: in-memory state already updated; failure to persist
      // is logged but never blocks the request path.
      void writeFile(id, record);
    },
    delete: (id) => {
      mem.delete(id);
      void unlinkFile(id);
    },
    async loadAll() {
      let files: string[];
      try {
        files = await fs.readdir(SESSIONS_DIR);
      } catch (err) {
        const code = (err as NodeJS.ErrnoException).code;
        if (code !== 'ENOENT') {
          // eslint-disable-next-line no-console
          console.warn(`[session-store] readdir failed: ${(err as Error).message}`);
        }
        return;
      }
      let loaded = 0;
      for (const f of files) {
        if (!f.endsWith('.json') || f.endsWith('.tmp')) continue;
        const id = f.slice(0, -5);
        if (!/^[A-Za-z0-9_-]+$/.test(id)) continue;
        try {
          const text = await fs.readFile(path.join(SESSIONS_DIR, f), 'utf8');
          const raw = JSON.parse(text) as unknown;
          // Run migrator first so old shapes get normalized before they enter
          // the typed memory map. A migrator that returns undefined means the
          // record is intentionally dropped (e.g. unrecoverable schema).
          const parsed = opts.migrate ? opts.migrate(raw) : (raw as T);
          if (parsed === undefined) continue;
          mem.set(id, parsed);
          loaded += 1;
        } catch (err) {
          // eslint-disable-next-line no-console
          console.warn(`[session-store] load ${f} failed: ${(err as Error).message}`);
        }
      }
      // eslint-disable-next-line no-console
      console.log(`[session-store] loaded ${loaded} session(s) from ${SESSIONS_DIR}`);
    },
  };
}
