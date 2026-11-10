/**
 * skill-reader/cache.ts — content-addressed cache for SkillReadOutput.
 *
 * PR-A produces verbatim file collections, not LLM artifacts, so the "cache"
 * here is really a memoisation of the file walk + frontmatter parse + hash
 * computation. Cheap on its own, but PR-C will reuse the same `content_hash`
 * as its compile cache key, so persisting the read result alongside lets the
 * compiler short-circuit re-walks when nothing on disk has changed.
 *
 * Cache layout:
 *   .xueban/cache/skill-reader/<sha256-hex>.json
 *
 * Why SHA-256 instead of the SHA-1 used by `skill-ingest/canonical-id.ts`:
 *   canonical-id only needs a short fallback slug from a URL; collisions
 *   would just rename a skill. PR-A's hash is the cache key for downstream
 *   LLM compilation — a collision would silently serve the wrong compiled
 *   team. SHA-256 is the cheap, future-proof choice. (Implementer decision,
 *   noted in commit body.)
 *
 * Hash input contract:
 *   Sort entries by `path` (POSIX), then concatenate `path + "\0" + raw + "\0"`
 *   for each entry. The null bytes prevent ambiguity between "ab" + "c" and
 *   "a" + "bc" file boundaries (paths and raw are arbitrary text).
 */

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import type { SkillReadOutput } from './types';

const CACHE_ROOT = path.join(process.cwd(), '.xueban', 'cache', 'skill-reader');

/**
 * Compute the canonical content hash for a list of (path, raw) tuples.
 * Sorting guarantees the same set of files always hashes to the same value
 * regardless of fs.readdir() iteration order across platforms.
 */
export function computeContentHash(files: Array<{ path: string; raw: string }>): string {
  const sorted = [...files].sort((a, b) => a.path.localeCompare(b.path));
  const h = crypto.createHash('sha256');
  for (const f of sorted) {
    h.update(f.path);
    h.update('\0');
    h.update(f.raw);
    h.update('\0');
  }
  return h.digest('hex');
}

/**
 * Read a previously persisted SkillReadOutput by content hash. Returns null
 * on miss / malformed cache file — caller falls back to a fresh walk.
 */
export async function readCache(hash: string): Promise<SkillReadOutput | null> {
  const file = path.join(CACHE_ROOT, `${hash}.json`);
  if (!fs.existsSync(file)) return null;
  try {
    const raw = await fs.promises.readFile(file, 'utf-8');
    const parsed = JSON.parse(raw) as SkillReadOutput;
    // Defensive: minimal shape check. A partial / older-schema cache file
    // is treated as a miss so the caller regenerates a fresh entry.
    if (
      typeof parsed.skill_id !== 'string' ||
      typeof parsed.content_hash !== 'string' ||
      !Array.isArray(parsed.agent_files) ||
      !Array.isArray(parsed.workflow_files) ||
      !Array.isArray(parsed.doc_files)
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Persist a SkillReadOutput keyed by its `content_hash`. Atomic via
 * write-to-tmp + rename so a partial write doesn't poison the cache.
 */
export async function writeCache(out: SkillReadOutput): Promise<void> {
  await fs.promises.mkdir(CACHE_ROOT, { recursive: true });
  const file = path.join(CACHE_ROOT, `${out.content_hash}.json`);
  const tmp = `${file}.tmp.${process.pid}`;
  await fs.promises.writeFile(tmp, JSON.stringify(out, null, 2), 'utf-8');
  await fs.promises.rename(tmp, file);
}
