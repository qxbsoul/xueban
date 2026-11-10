/**
 * skill-types.ts — S6.0
 *
 * Data shapes for skill-backed agents (v3 stacked design).
 *
 * Skills used to be one chunk of system_prompt text. v3 pulls each agent's
 * persona/model/tools/memory/io from a structured YAML so the front-end can
 * show provenance ("from reader.skill.yaml#persona 632 tokens · cached")
 * and the back-end can inject agents into the SSE stream without asking
 * the LLM to invent them.
 *
 * Layout on disk:
 *   .xueban/skills/<id>/
 *     ├── SKILL.md             (existing — frontmatter + free system_prompt)
 *     ├── team.skill.yaml      (NEW — declares which agents + edges)
 *     ├── <agent>.skill.yaml   (NEW — one per agent, full spec)
 *     └── <agent>.skill.yaml ...
 *
 * Token counts are coarse (`Math.ceil(chars/4)`) — accurate-enough for the
 * provenance label, not used for billing.
 */

export type SkillSlot = 'persona' | 'model' | 'tools' | 'memory' | 'io';

export interface SkillAnchor {
  /** `<agent>.skill.yaml#persona` */
  ref: string;
  /** Token count of the body, char/4 estimate. */
  tokens: number;
  /** True once this anchor has been read into a session prompt. */
  cached: boolean;
}

export interface AgentModelSpec {
  id: string;
  temperature?: number;
  max_tokens?: number;
  context_window?: number;
}

export interface AgentToolsSpec {
  picked: string[];
  candidate: string[];
}

export interface AgentIOSpec {
  inputs?: { expects?: Record<string, string> };
  outputs?: { produces?: Record<string, string> };
}

export interface SkillAgentDef {
  id: string;
  title: string;
  sub?: string;
  avatar_char?: string;
  type?: 'agent' | 'coordinator';

  /** Persona body text — used as that agent's system prompt. */
  persona: string;
  model: AgentModelSpec;
  tools: AgentToolsSpec;
  memory?: string;
  io?: AgentIOSpec;

  /** Per-slot provenance metadata (precomputed by skill-yaml.ts). */
  anchors: Record<SkillSlot, SkillAnchor>;

  /** File the agent was loaded from, relative to the skill dir. */
  source_file: string;
}

export interface TeamEdge {
  from: string;
  to: string;
}

export interface TeamDef {
  /** Team identifier — e.g. "paper.review.v1". */
  name: string;
  mode?: 'serial' | 'parallel' | 'dag';
  policy?: 'strict' | 'permissive';
  retry?: number;
  agents: SkillAgentDef[];
  edges: TeamEdge[];

  /** mtime in ms — used to skip reload when unchanged. */
  loaded_at: number;
  source_dir: string;
}

// ─── Round 4 PR-A re-exports ────────────────────────────────────────────────
//
// `SkillReadOutput` is the verbatim file collection PR-C's SkillCompiler
// consumes. It lives next to skill-ingest under `server/src/skill-reader/`
// (separate concern from the v3 stacked skill yaml above). Re-exported here
// so downstream callers can `import { SkillReadOutput } from '../lib/skill-types'`
// alongside the existing TeamDef / SkillAgentDef without learning a new path.
export type {
  SkillReadOutput,
  SkillFileEntry,
  AgentFile as SkillReaderAgentFile,
  WorkflowFile as SkillReaderWorkflowFile,
  DocFile as SkillReaderDocFile,
} from '../skill-reader/types';
