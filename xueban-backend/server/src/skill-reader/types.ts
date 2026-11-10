/**
 * skill-reader/types.ts — verbatim skill content shapes (PR-A foundation).
 *
 * The Round 4 architecture pivots Xueban from "skill = static team yaml" to
 * "skill content (raw text) → LLM-compiled agent OR team". PR-A's job is to
 * collect the verbatim text inputs that PR-C's SkillCompiler will feed into the
 * LLM. PR-A does **not** interpret content — no agent-count inference, no
 * workflow ordering, no persona extraction. Just classify files into three
 * buckets (agent / workflow / doc) by their on-disk location and keep the raw
 * bytes intact for downstream LLM consumption.
 *
 * Design notes:
 *   - `raw` field is byte-equal to `fs.readFileSync(path, 'utf-8')`. PR-C
 *     tests assert this (golden file invariant).
 *   - `frontmatter` is gray-matter parsed for `.md` files only; YAML files
 *     keep `null` (PR-C parses them as a whole if needed).
 *   - `path` is always relative to `skillDir` (POSIX `/` separator) so the
 *     hash is stable across Windows / Linux.
 *   - `content_hash` covers every (path, raw) pair sorted by path. Any byte
 *     change anywhere busts the cache. This is what PR-C uses as its compile
 *     cache key (`source_content_hash`).
 *
 * Out of scope for PR-A:
 *   - Agent persona extraction (PR-C)
 *   - Workflow DAG parsing (PR-C)
 *   - team-vs-agent decision (PR-C)
 *   - LLM calls of any kind
 */

/**
 * One verbatim file picked up from the skill tree. Used uniformly for agent /
 * workflow / doc buckets — they only differ by source directory and how PR-C
 * interprets them, not by data shape.
 */
export interface SkillFileEntry {
  /** Path relative to skillDir, POSIX separator (e.g. `agents/analyst.md`). */
  path: string;
  /** Raw file bytes decoded as UTF-8. Byte-equal to `fs.readFileSync`. */
  raw: string;
  /** gray-matter parsed frontmatter for `.md` files; `null` otherwise / no fm. */
  frontmatter: Record<string, unknown> | null;
}

/**
 * Result of `readSkill(skillDir)`. Verbatim text + classification only — no
 * interpretation. Cached at `.xueban/cache/skill-reader/<content_hash>.json`
 * keyed by the SHA-256 of all (path, raw) pairs.
 */
export interface SkillReadOutput {
  /** Canonical skill id (derived from skillDir basename — caller's contract). */
  skill_id: string;
  /** SHA-256 hex of all (path, raw) pairs, sorted by path. Stable across runs. */
  content_hash: string;
  /** Verbatim SKILL.md content if present at skillDir root, else empty string. */
  raw_skill_md: string;
  /** Files under `agents/` + synthesized entries from `bmad-modules.yaml`. */
  agent_files: SkillFileEntry[];
  /** Files under `workflows/`. */
  workflow_files: SkillFileEntry[];
  /** Top-level prose docs: README, AGENTS.md, CHANGELOG, etc. */
  doc_files: SkillFileEntry[];
}

/**
 * Re-export alias used in `parse-agents.ts` / `parse-workflow.ts` for clarity
 * at the call site, though the underlying shape is identical.
 */
export type AgentFile = SkillFileEntry;
export type WorkflowFile = SkillFileEntry;
export type DocFile = SkillFileEntry;
