/**
 * skill-compiler/prompt.ts — versioned LLM compile prompt.
 *
 * The template is intentionally a TS function (not a static string) so we can
 * inject `SkillReadOutput` files verbatim with proper XML-ish boundaries —
 * `<file path="…">…</file>` — without YAML / Markdown escaping headaches.
 *
 * VERSION: v1 — initial Round 4 release.
 *
 * Bump the constant below alongside any wording change. Compile cache uses
 * `compiler_version` to invalidate stale entries: bumping `PROMPT_VERSION`
 * forces every cached compile to re-run on next access.
 */

import type { SkillReadOutput, SkillFileEntry } from '../../skill-reader/types';

export const PROMPT_VERSION = 'v1';

/**
 * Per-file inclusion cap. Skills can ship surprisingly large agent files
 * (BMAD's `bmad-modules.yaml` is ~10KB by itself). We truncate any single
 * file beyond this so a giant skill doesn't blow context, but we keep
 * enough head/tail to give the LLM something to reason about.
 */
const MAX_FILE_CHARS = 12_000;

/**
 * Total prompt cap as a safety belt. Even after per-file truncation a skill
 * with many files can balloon. Empirically BMAD's full content is ~110KB;
 * we cap at ~80KB → roughly 20K tokens, well under any modern model's
 * single-call window.
 */
const MAX_TOTAL_CHARS = 80_000;

export interface CompilePrompt {
  system: string;
  user: string;
  /** Approximate input length in chars (for telemetry / cap monitoring). */
  estimated_chars: number;
}

/**
 * Build the (system, user) message pair for the compile LLM call.
 *
 * The system message defines the contract: "read a skill, decide single vs
 * team, emit JSON". The user message dumps the actual skill content. We
 * keep them separate so cache-friendly providers (Anthropic) can cache the
 * stable system half across repeated compiles.
 */
export function buildCompilePrompt(skill: SkillReadOutput): CompilePrompt {
  const system = buildSystem();
  const user = buildUser(skill);
  return {
    system,
    user,
    estimated_chars: system.length + user.length,
  };
}

// ─── internals ────────────────────────────────────────────────────────────────

function buildSystem(): string {
  return [
    'You are Xueban Skill Compiler v1.',
    '',
    'Your job: read a skill (a folder of Markdown + YAML files) and decide whether it should execute as a SINGLE AGENT or a MULTI-AGENT TEAM. Then emit a JSON config for the chosen mode.',
    '',
    'You will see four sections:',
    '  - <SKILL_MD>   — top-level SKILL.md (entry point)',
    '  - <AGENTS>     — files under agents/ (one or more agent persona descriptions)',
    '  - <WORKFLOWS>  — files under workflows/ (DAG / pipeline specs)',
    '  - <DOCS>       — README, AGENTS.md, etc.',
    '',
    'Decision rules:',
    '  - If AGENTS contains 2+ distinct agent personas AND any DOCS / WORKFLOWS describe sequential collaboration, handoff, or pipeline → emit mode="team".',
    '  - If AGENTS describes a single agent with a list of tools, or AGENTS is empty → emit mode="agent".',
    '  - When in doubt with 2+ AGENTS, prefer team (the assembler can collapse trivial DAGs at runtime).',
    '',
    'Output JSON schema (EMIT NOTHING ELSE — no markdown fences, no prose):',
    '{',
    '  "mode": "agent" | "team",',
    '  "agentConfig"?: {',
    '    "persona": string,              // 100-500 chars, paraphrased from AGENTS',
    '    "system_prompt": string,        // full system prompt — verbatim SKILL.md + persona is fine',
    '    "tools": string[],              // tool ids the skill needs; default ["read_file","list_dir","glob_files","grep"]',
    '    "model_hint"?: string,          // e.g. "claude-sonnet-4-6" if skill recommends one',
    '    "max_iterations"?: number       // default 50',
    '  },',
    '  "teamConfig"?: {',
    '    "name": string,                 // human-readable team name',
    '    "description"?: string,',
    '    "members_ids": string[],        // canonical ids matching [a-z0-9][a-z0-9_-]{0,63}',
    '    "members_personas": {           // one persona per id, 100-500 chars',
    '      [id: string]: string',
    '    },',
    '    "edges_v1": [{                  // DAG edges',
    '      "from": string,               // member id',
    '      "to": string,                 // member id',
    '      "kind"?: "sequential" | "parallel" | "conditional",',
    '      "condition"?: string,         // only when kind="conditional"',
    '      "max_retries"?: number',
    '    }],',
    '    "policy_obj": {',
    '      "retry"?: number,             // default 3',
    '      "timeout_per_step_ms"?: number // default 60000',
    '    }',
    '  }',
    '}',
    '',
    'Exactly ONE of agentConfig / teamConfig must be present, matching mode.',
    'For team mode: every member id in members_ids MUST appear as a key in members_personas. Every edge from/to MUST be in members_ids.',
    'Default temperature 0 — same input → same output.',
  ].join('\n');
}

function buildUser(skill: SkillReadOutput): string {
  const parts: string[] = [];
  parts.push(`Skill id: ${skill.skill_id}`);
  parts.push(`Content hash: ${skill.content_hash}`);
  parts.push('');

  parts.push('<SKILL_MD>');
  parts.push(truncate(skill.raw_skill_md));
  parts.push('</SKILL_MD>');
  parts.push('');

  parts.push('<AGENTS>');
  for (const f of skill.agent_files) {
    parts.push(renderFile(f));
  }
  parts.push('</AGENTS>');
  parts.push('');

  parts.push('<WORKFLOWS>');
  for (const f of skill.workflow_files) {
    parts.push(renderFile(f));
  }
  parts.push('</WORKFLOWS>');
  parts.push('');

  parts.push('<DOCS>');
  for (const f of skill.doc_files) {
    parts.push(renderFile(f));
  }
  parts.push('</DOCS>');
  parts.push('');

  parts.push(
    'Emit ONLY the JSON object described in the system message. No code fence, no commentary.',
  );

  let out = parts.join('\n');
  if (out.length > MAX_TOTAL_CHARS) {
    out =
      out.slice(0, MAX_TOTAL_CHARS) +
      '\n\n[…prompt truncated to fit context cap — file order preserved]';
  }
  return out;
}

function renderFile(f: SkillFileEntry): string {
  return `<file path="${escapeAttr(f.path)}">\n${truncate(f.raw)}\n</file>`;
}

function truncate(s: string): string {
  if (s.length <= MAX_FILE_CHARS) return s;
  const head = s.slice(0, Math.floor(MAX_FILE_CHARS * 0.7));
  const tail = s.slice(s.length - Math.floor(MAX_FILE_CHARS * 0.2));
  return `${head}\n\n[…middle truncated…]\n\n${tail}`;
}

function escapeAttr(s: string): string {
  return s.replace(/"/g, '&quot;');
}
