/**
 * skill-ingest/index.ts — public API.
 *
 * The whole module exists to take a user-supplied skill source (URL or
 * pasted text) at chat time and turn it into a registered skill at
 * `.xueban/skills/<id>/` that the rest of the run-session pipeline can
 * use unchanged.
 *
 * Pipeline:
 *   ingestSkill(source)
 *     → fetchSkill()     (clone / download / write text to cache)
 *     → probeSkill()     (walk + classify files, no schema enforced)
 *     → registerSkill()  (copy references/, write SKILL.md + probe.json)
 *     → return { id, ... }  — caller plumbs id into run-session.skill_name
 *
 * Re-runs are idempotent: same source string → same cache dir, same skill id,
 * fresh upstream content. Different sources that happen to slug to the same
 * id get disambiguated by appending the source hash.
 */

import path from 'path';
import fs from 'fs';
import { fetchSkill, parseSource } from './fetch';
import { probeSkill, renderProbeForPrompt } from './probe';
import { registerSkill, listInstalled, getInstalled } from './register';
import { tryReadSkill } from '../skill-reader';
import { compile as compileSkill } from '../lib/skill-compiler';
import type { ProbeResult } from './probe';
import type { InstalledSkill } from './register';

export { parseSource, fetchSkill, probeSkill, renderProbeForPrompt };
export { registerSkill, listInstalled, getInstalled };
export type { ProbeResult, InstalledSkill };

export interface IngestResult {
  id: string;
  name: string;
  is_new: boolean;
  probe: ProbeResult;
  source_label: string;
  source_hash: string;
  /** ready-to-inject markdown block summarizing the skill */
  prompt_block: string;
}

/**
 * One-shot ingest: fetch → probe → register. Returns everything the chat
 * handler needs to acknowledge installation and chain into a run session.
 */
export async function ingestSkill(source: string, forced_id?: string): Promise<IngestResult> {
  const fetched = await fetchSkill(source);
  const probe = probeSkill(fetched.dir, fetched.subpath);
  const reg = registerSkill({ fetched, probe, forced_id });

  // PR-A (Round 4): after the skill has been copied into
  // `.xueban/skills/<id>/references/`, walk it through `readSkill()` so
  // the SkillCompiler (PR-C) has a cached `SkillReadOutput` ready when the
  // user first invokes the skill.
  //
  // PR-C (Round 4): immediately following the reader walk, invoke the
  // compiler to produce a `CompiledSkill` (agent or team config) under
  // `.xueban/cache/skill-compile/<content_hash>.json`. Compilation is
  // best-effort: any failure is swallowed (the assembler will see a cache
  // miss and either re-compile or use fallback). Failures here MUST NOT
  // break ingest — registering the skill is the user-visible contract.
  const refDir = path.join(reg.dir, 'references');
  if (fs.existsSync(refDir)) {
    const skillRead = await tryReadSkill(refDir, { skill_id: reg.id });
    if (skillRead) {
      try {
        await compileSkill(skillRead);
      } catch (err) {
        console.warn(
          `[skill-ingest] compile failed for ${reg.id}: ${(err as Error).message ?? err} — fallback will apply at run time`,
        );
      }
    }
  }

  return {
    id: reg.id,
    name: reg.name,
    is_new: reg.is_new,
    probe,
    source_label: fetched.source_label,
    source_hash: fetched.source_hash,
    prompt_block: renderProbeForPrompt(probe, reg.name),
  };
}
