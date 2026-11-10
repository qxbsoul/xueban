/**
 * skill-reader/index.ts — verbatim skill content collector (PR-A foundation).
 *
 * Round 4 architecture: Xueban becomes a "skill → agent/team" compiler.
 * `skill-reader` is the input side — it gathers the raw text the LLM-side
 * compiler (PR-C) needs without interpreting any of it:
 *
 *   skillDir on disk
 *     → readSkill(skillDir)
 *         → parseAgents()      (agents/*.md + bmad-modules.yaml synthesis)
 *         → parseWorkflows()   (workflows/*.yaml|yml|md)
 *         → collectDocs()      (SKILL.md, README.md, AGENTS.md, etc.)
 *         → computeContentHash() (SHA-256 over sorted (path, raw) tuples)
 *     → SkillReadOutput   (cached at .xueban/cache/skill-reader/<hash>.json)
 *
 * The output is verbatim — `raw` fields are byte-equal to `fs.readFileSync`.
 * PR-C consumes this and produces a `CompiledSkill` (agent OR team config)
 * via LLM.
 *
 * Re-runs are cache-friendly: the same skillDir contents always hash to the
 * same `content_hash`, so a second call hits the disk cache and skips the
 * (cheap) re-walk + parse. Any byte change in any file busts the cache.
 *
 * Caller-visible contract:
 *   - Never throws on missing/empty subdirs.
 *   - Never throws on malformed frontmatter (degrades to `frontmatter: null`).
 *   - Never executes anything in the skill (security — we read text only).
 *   - Returns an empty bucket rather than skipping the field, so PR-C can
 *     always destructure `{ agent_files, workflow_files, doc_files }`.
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import { parseAgents } from './parse-agents';
import { parseWorkflows } from './parse-workflow';
import { computeContentHash, readCache, writeCache } from './cache';
import type { DocFile, SkillReadOutput, SkillFileEntry } from './types';

export type { SkillReadOutput, SkillFileEntry } from './types';
export { computeContentHash, readCache, writeCache } from './cache';
export { parseAgents } from './parse-agents';
export { parseWorkflows } from './parse-workflow';

/** Top-level files considered "documentation" rather than agent/workflow specs.
 *  Filename matching is case-insensitive and matches the **stem** only so
 *  `README.md` and `Readme.md` both qualify. */
const DOC_FILE_STEMS = new Set([
  'readme',
  'readme_cn',
  'readme_vn',
  'agents',
  'changelog',
  'contributing',
  'contributors',
  'security',
  'trademark',
  'license',
  'workflow-map',
  'index',
  'overview',
]);

const DOC_EXTS = new Set(['.md', '.mdx', '.txt']);

/** POSIX-normalize a path so hashes are stable across Windows/Linux. */
function toPosix(p: string): string {
  return p.replace(/\\/g, '/');
}

/** Read SKILL.md verbatim from skillDir root. Empty string when absent. */
function readSkillMd(skillDir: string): string {
  for (const candidate of ['SKILL.md', 'skill.md']) {
    const abs = path.join(skillDir, candidate);
    if (fs.existsSync(abs)) {
      try {
        return fs.readFileSync(abs, 'utf-8');
      } catch {
        return '';
      }
    }
  }
  return '';
}

/**
 * Pick up top-level documentation files (README, AGENTS.md, CHANGELOG, ...).
 * Only the root directory is scanned — sub-directory docs belong to PR-C
 * if it wants them. This keeps PR-A focused on the "headline" prose a
 * compiler would read first.
 */
function collectDocs(skillDir: string): DocFile[] {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(skillDir, { withFileTypes: true });
  } catch {
    return [];
  }

  const out: DocFile[] = [];
  for (const e of entries) {
    if (!e.isFile()) continue;
    if (e.name.startsWith('.')) continue;
    const ext = path.extname(e.name).toLowerCase();
    if (!DOC_EXTS.has(ext)) continue;

    const stem = path.basename(e.name, ext).toLowerCase();
    // SKILL.md is the dedicated `raw_skill_md` field — don't double-count it.
    if (stem === 'skill') continue;
    if (!DOC_FILE_STEMS.has(stem)) continue;

    const abs = path.join(skillDir, e.name);
    let raw: string;
    try {
      raw = fs.readFileSync(abs, 'utf-8');
    } catch {
      continue;
    }

    let frontmatter: Record<string, unknown> | null = null;
    if (ext === '.md' || ext === '.mdx') {
      try {
        const parsed = matter(raw);
        if (parsed.data && Object.keys(parsed.data).length > 0) {
          frontmatter = parsed.data as Record<string, unknown>;
        }
      } catch {
        // malformed frontmatter → drop, keep raw
      }
    }

    out.push({
      path: toPosix(e.name),
      raw,
      frontmatter,
    });
  }

  out.sort((a, b) => a.path.localeCompare(b.path));
  return out;
}

/**
 * Derive a stable skill_id from the directory path when the caller doesn't
 * supply one. Walks up past the conventional `references/` subdir so callers
 * passing `<.xueban/skills/<id>/references>` still get `<id>` instead of
 * the literal string `"references"`.
 *
 * 2026-05-26 follow-up to PR-E: SkillDropdown showed `◌ 未编译` because the
 * boot warmer wrote compile cache keyed by `"references"` instead of the
 * real skill id.
 */
function deriveSkillIdFromPath(skillDir: string): string {
  const abs = path.resolve(skillDir);
  const base = path.basename(abs);
  if (base.toLowerCase() === 'references') {
    const parent = path.basename(path.dirname(abs));
    if (parent) return parent;
  }
  return base;
}

export interface ReadSkillOptions {
  /** Explicit skill_id override. When omitted, derived from skillDir. */
  skill_id?: string;
}

/**
 * Read a skill directory into a verbatim `SkillReadOutput`.
 *
 * @param skillDir absolute or cwd-relative path to a skill root. The root is
 *                 expected to contain `SKILL.md`, `agents/`, `workflows/`, etc.
 *                 For installed skills the raw content sits under
 *                 `<.xueban/skills/<id>/references/>` — call this with
 *                 the `references/` path. For freshly-cloned cache content
 *                 (e.g. `.xueban/cache/skill-ingest/<hash>/`) call this
 *                 with the cache root directly.
 * @param opts.skill_id explicit override (recommended when caller knows the
 *                 real id). Falls back to `deriveSkillIdFromPath(skillDir)`.
 * @returns A verbatim `SkillReadOutput`. Idempotent — same disk state always
 *          yields the same `content_hash`. Disk cache hit avoids the file
 *          walk; cache miss writes the result for next time.
 */
export async function readSkill(skillDir: string, opts?: ReadSkillOptions): Promise<SkillReadOutput> {
  if (!fs.existsSync(skillDir)) {
    // Empty skill — still return a well-shaped object so callers don't have
    // to special-case it. content_hash of empty input is deterministic.
    const skill_id = opts?.skill_id ?? deriveSkillIdFromPath(skillDir);
    const empty: SkillReadOutput = {
      skill_id,
      content_hash: computeContentHash([]),
      raw_skill_md: '',
      agent_files: [],
      workflow_files: [],
      doc_files: [],
    };
    return empty;
  }

  const skill_id = opts?.skill_id ?? deriveSkillIdFromPath(skillDir);

  const [agent_files, workflow_files] = await Promise.all([
    parseAgents(skillDir),
    parseWorkflows(skillDir),
  ]);
  const doc_files = collectDocs(skillDir);
  const raw_skill_md = readSkillMd(skillDir);

  // Hash input: every (path, raw) tuple — SKILL.md included so changes there
  // bust the cache the same way agents/workflows/docs do.
  const allFiles: Array<{ path: string; raw: string }> = [];
  if (raw_skill_md) allFiles.push({ path: 'SKILL.md', raw: raw_skill_md });
  for (const f of agent_files) allFiles.push({ path: f.path, raw: f.raw });
  for (const f of workflow_files) allFiles.push({ path: f.path, raw: f.raw });
  for (const f of doc_files) allFiles.push({ path: f.path, raw: f.raw });

  const content_hash = computeContentHash(allFiles);

  // Cache hit check. We re-derive the hash from disk regardless (cheap) so
  // a stale cache file under a moved skillDir doesn't pin the wrong content.
  const cached = await readCache(content_hash);
  if (cached) {
    // Trust the cache for the heavy fields, but refresh `skill_id` so the
    // same content under a renamed skill dir surfaces the new id.
    return { ...cached, skill_id };
  }

  const out: SkillReadOutput = {
    skill_id,
    content_hash,
    raw_skill_md,
    agent_files,
    workflow_files,
    doc_files,
  };

  try {
    await writeCache(out);
  } catch {
    // Cache write failure is non-fatal — return the fresh result anyway.
  }

  return out;
}

/**
 * Helper for the skill-ingest hot path: best-effort `readSkill` that swallows
 * errors. The ingest pipeline must not fail because the post-install reader
 * tripped over a disk read; the worst case is PR-C re-walks on first compile.
 */
export async function tryReadSkill(
  skillDir: string,
  opts?: ReadSkillOptions,
): Promise<SkillReadOutput | null> {
  try {
    return await readSkill(skillDir, opts);
  } catch (err) {
    console.warn(`[skill-reader] readSkill failed for ${skillDir}:`, err);
    return null;
  }
}
