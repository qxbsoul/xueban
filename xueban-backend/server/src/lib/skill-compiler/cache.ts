/**
 * skill-compiler/cache.ts — content-hash addressed cache for `CompiledSkill`.
 *
 * Mirrors `skill-reader/cache.ts` layout: atomic write-tmp + rename, defensive
 * shape check on read. Cache key is the `source_content_hash` from PR-A so the
 * full pipeline shares a single hash identity: skill content bytes → reader
 * hash → compile cache key. Any byte change in any source file invalidates
 * both the reader cache and the compile cache.
 *
 * Cache layout:
 *   .xueban/cache/skill-compile/<sha256-hex>.json
 *
 * Why split from PR-A's cache file: a compile entry can become stale even
 * when the reader entry is still valid (e.g. compiler_version bump, prompt
 * template change). Keeping the two files apart lets us nuke `skill-compile/`
 * to force a re-compile without losing the (free, expensive-to-rebuild)
 * reader walk results.
 */

import fs from 'fs';
import path from 'path';
import type { CompiledSkill } from './types';

const CACHE_ROOT = path.join(process.cwd(), '.xueban', 'cache', 'skill-compile');

/** Override cache root for tests — never used in production code paths. */
let cacheRootOverride: string | null = null;
export function _setCacheRootForTests(root: string | null): void {
  cacheRootOverride = root;
}
function cacheRoot(): string {
  return cacheRootOverride ?? CACHE_ROOT;
}

/**
 * Read a previously persisted CompiledSkill by content hash. Returns null on
 * miss or malformed cache file — callers (compile()) fall back to a fresh
 * LLM call. Also returns null when `compiler_version` doesn't match the
 * current code, so prompt template changes auto-invalidate every existing
 * entry without manual flushing.
 */
export async function readCompileCache(
  content_hash: string,
): Promise<CompiledSkill | null> {
  const file = path.join(cacheRoot(), `${content_hash}.json`);
  if (!fs.existsSync(file)) return null;
  try {
    const raw = await fs.promises.readFile(file, 'utf-8');
    const parsed = JSON.parse(raw) as CompiledSkill;
    // Defensive shape check — older / partially-written entries are treated
    // as a miss so compile() regenerates from scratch.
    if (
      typeof parsed.skill_id !== 'string' ||
      typeof parsed.source_content_hash !== 'string' ||
      typeof parsed.compiled_at !== 'string' ||
      parsed.compiler_version !== 'v1' ||
      (parsed.mode !== 'agent' && parsed.mode !== 'team')
    ) {
      return null;
    }
    if (parsed.mode === 'agent' && !parsed.agentConfig) return null;
    if (parsed.mode === 'team' && !parsed.teamConfig) return null;
    if (parsed.source_content_hash !== content_hash) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Persist a CompiledSkill keyed by its `source_content_hash`. Atomic write
 * via tmp + rename so a crash mid-write never poisons the cache. mkdir is
 * lazy so the cache dir isn't created until the first successful compile.
 */
export async function writeCompileCache(c: CompiledSkill): Promise<void> {
  const root = cacheRoot();
  await fs.promises.mkdir(root, { recursive: true });
  const file = path.join(root, `${c.source_content_hash}.json`);
  const tmp = `${file}.tmp.${process.pid}`;
  await fs.promises.writeFile(tmp, JSON.stringify(c, null, 2), 'utf-8');
  await fs.promises.rename(tmp, file);
}
