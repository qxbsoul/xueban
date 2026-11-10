/**
 * skill-compiler/fallback.ts — degraded compile path when LLM is unavailable.
 *
 * Called when:
 *   - No BYOK / env API key is configured for any usable provider
 *   - LLM call throws (network, auth, rate limit)
 *   - LLM returns malformed / un-parseable JSON
 *   - LLM JSON fails schema validation
 *
 * Strategy is intentionally dumb-but-deterministic so identical inputs always
 * compile to identical outputs even when nothing is online:
 *
 *   - `agent_files.length >= 2`   → team mode, members = file basenames,
 *                                    edges = sequential chain in disk order
 *   - `agent_files.length <= 1`   → agent mode, persona = raw_skill_md
 *                                    (truncated), tools = read-only default
 *
 * The `derivedFrom: 'fallback'` provenance tag lets downstream consumers
 * (UI, telemetry, tests) detect that compilation was degraded and offer a
 * "retry compile" affordance when keys come back online. Test golden files
 * also assert on this field to distinguish LLM vs fallback paths.
 */

import path from 'path';
import type { SkillReadOutput, SkillFileEntry } from '../../skill-reader/types';
import type {
  CompiledSkill,
  CompiledAgentConfig,
  CompiledTeamConfig,
} from './types';

/**
 * Default read-only tool set assumed available at the runtime layer (PR-D
 * built-in registry). Falls back agents don't get write/edit/shell tools
 * because the compiler had no LLM judgement to confirm they're safe.
 */
const DEFAULT_READ_TOOLS: ReadonlyArray<string> = [
  'read_file',
  'list_dir',
  'glob_files',
  'grep',
];

/**
 * Cap persona-string length on the agent path so a giant SKILL.md doesn't
 * blow past LLM context windows at run time. The full system_prompt is
 * still composed verbatim (truncation only affects the short "persona"
 * field surfaced in UI / sidebars).
 */
const PERSONA_MAX_CHARS = 1500;

/**
 * Build a deterministic fallback `CompiledSkill` from PR-A's SkillReadOutput.
 *
 * @param skill   the verbatim file collection from `readSkill()`
 * @param reason  caller-supplied free-text reason (logged + recorded into
 *                `llm_call_meta.model` as `fallback:<reason>` so log diving
 *                tells you which branch tripped)
 */
export function fallbackCompile(
  skill: SkillReadOutput,
  reason: string,
): CompiledSkill {
  const now = new Date().toISOString();
  const baseMeta = {
    model: 'fallback',
    tokens_in: 0,
    tokens_out: 0,
    duration_ms: 0,
  };

  const agentCount = skill.agent_files.length;

  if (agentCount >= 2) {
    return {
      skill_id: skill.skill_id,
      source_content_hash: skill.content_hash,
      compiled_at: now,
      compiler_version: 'v1',
      mode: 'team',
      teamConfig: buildFallbackTeam(skill),
      llm_call_meta: { ...baseMeta, model: `fallback:${reason}` },
    };
  }

  // Single agent (or zero agent files — we still compile to "agent" mode and
  // use raw_skill_md as the persona, so a prose-only skill remains runnable).
  return {
    skill_id: skill.skill_id,
    source_content_hash: skill.content_hash,
    compiled_at: now,
    compiler_version: 'v1',
    mode: 'agent',
    agentConfig: buildFallbackAgent(skill),
    llm_call_meta: { ...baseMeta, model: `fallback:${reason}` },
  };
}

// ─── internals ────────────────────────────────────────────────────────────────

function buildFallbackAgent(skill: SkillReadOutput): CompiledAgentConfig {
  const personaSource =
    skill.agent_files[0]?.raw?.trim() || skill.raw_skill_md.trim() || '';
  const persona =
    personaSource.length > PERSONA_MAX_CHARS
      ? personaSource.slice(0, PERSONA_MAX_CHARS) + '\n[…truncated]'
      : personaSource;

  const system_prompt = composeAgentSystemPrompt(skill, persona);

  return {
    persona,
    system_prompt,
    tools: [...DEFAULT_READ_TOOLS],
    max_iterations: 50,
  };
}

function buildFallbackTeam(skill: SkillReadOutput): CompiledTeamConfig {
  // Member id = first path segment after `agents/` (or full stem if not there)
  // lower-cased + dot-stripped so it satisfies the standard agent-id regex.
  const members_ids: string[] = [];
  const members_personas: Record<string, string> = {};
  for (const f of skill.agent_files) {
    const id = memberIdFromFile(f);
    if (!id || members_ids.includes(id)) continue;
    members_ids.push(id);
    const raw = f.raw.trim();
    members_personas[id] =
      raw.length > PERSONA_MAX_CHARS
        ? raw.slice(0, PERSONA_MAX_CHARS) + '\n[…truncated]'
        : raw;
  }

  // Sequential chain: a → b → c …
  const edges_v1 = members_ids.slice(1).map((to, i) => ({
    from: members_ids[i],
    to,
    kind: 'sequential' as const,
  }));

  return {
    team_id: skill.skill_id,
    version: 1,
    name: skill.skill_id,
    description: `Fallback-compiled team from ${members_ids.length} agent file(s).`,
    members_ids,
    members_personas,
    edges_v1,
    policy_obj: {
      retry: 3,
      timeout_per_step_ms: 60_000,
    },
    derivedFrom: 'fallback',
  };
}

/**
 * Compose a system prompt by stacking the skill's SKILL.md + agent prose +
 * a short execution directive. Run-time then prepends this to every turn.
 */
function composeAgentSystemPrompt(skill: SkillReadOutput, persona: string): string {
  const parts: string[] = [];
  if (skill.raw_skill_md.trim()) {
    parts.push(skill.raw_skill_md.trim());
  }
  if (persona && (!skill.raw_skill_md || !skill.raw_skill_md.includes(persona))) {
    parts.push('---', persona);
  }
  parts.push(
    '---',
    `You are executing the "${skill.skill_id}" skill. Follow the instructions above precisely. Use the available tools to complete the user's goal.`,
  );
  return parts.join('\n\n');
}

/**
 * Reduce an agent file path to a stable id. Matches the canonical
 * `[a-z0-9][a-z0-9_-]{0,63}` regex used elsewhere in the codebase.
 *
 * Examples:
 *   agents/analyst.md                 → "analyst"
 *   .../bmad-agent-analyst/SKILL.md   → "bmad-agent-analyst"  (file stem is a
 *                                        generic marker → use the dir name, so
 *                                        N nested SKILL.md files don't all
 *                                        collapse to the id "skill")
 *   agents/01-pm.synthesized.md       → "01-pm"  (legacy synth suffix stripped)
 */
const GENERIC_FILE_STEMS = new Set(['skill', 'index', 'agent', 'main', 'readme']);

export function memberIdFromFile(f: SkillFileEntry): string | null {
  const segs = f.path.split('/').filter(Boolean);
  const file = segs[segs.length - 1] ?? '';
  let stem = file.replace(/\.synthesized\.md$/i, '').replace(/\.[^.]+$/, '');
  // When the file itself carries no identity (e.g. `SKILL.md` inside a
  // per-agent directory), the meaningful id is the containing directory.
  if (GENERIC_FILE_STEMS.has(stem.toLowerCase()) && segs.length >= 2) {
    stem = segs[segs.length - 2];
  }
  const slug = stem
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
  if (!slug || !/^[a-z0-9]/.test(slug)) return null;
  return slug;
}
