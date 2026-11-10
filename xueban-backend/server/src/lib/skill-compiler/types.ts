/**
 * skill-compiler/types.ts — `CompiledSkill` shape (Round 4 PR-C).
 *
 * The compiler reads a verbatim `SkillReadOutput` (PR-A foundation), asks an
 * LLM whether the skill should run as a single agent or a multi-agent team,
 * and emits a `CompiledSkill` that downstream `assembler.ts` consumes at run
 * time. Compilation is a **one-time** event per skill content: the result is
 * cached under `.xueban/cache/skill-compile/<source_content_hash>.json`
 * and shared across every subsequent run. Goal text never participates in
 * compilation — goal is purely a run-time `user` message.
 *
 * Two execution modes:
 *   - `agent`  : single LLM with `agentConfig.system_prompt` + `tools`
 *   - `team`   : multi-agent DAG driven by `workflow/scheduler.runDag()`
 *
 * `teamConfig` is shaped to be **directly passable** to `runDag()` after
 * downstream resolution of `members_ids` into `SkillAgentDef[]`. We carry
 * `members_personas` separately so the assembler can synthesize lightweight
 * `SkillAgentDef`s on the fly when the skill bundle doesn't ship dedicated
 * `<agent>.agent.yaml` files (which is the BMAD case — agents are described
 * in prose only).
 *
 * `derivedFrom` records the provenance of the team config so the UI / future
 * cost telemetry can show "this team was inferred by LLM" vs "this team was
 * fallback-extracted from prose because LLM was unavailable".
 */

import type { TeamEdgeV1, TeamPolicyV1, EdgeKind } from '../team-yaml';

export type CompiledMode = 'agent' | 'team';

/**
 * Single-agent execution config.
 *
 * `system_prompt` is the full string passed to the LLM as `system`. It is
 * pre-composed by the compiler from `raw_skill_md` + `persona` so the
 * assembler doesn't have to redo the splicing. `tools` is a whitelist —
 * empty array means "no tools" (built-in read tools always allowed at the
 * runtime layer per PR-D, but the compiled config doesn't assume that).
 *
 * `max_iterations` defaults to 50 (mirrors Claude Code's default safety cap).
 * The runtime layer reads this verbatim when wiring the tool-use loop.
 */
export interface CompiledAgentConfig {
  /** ≤ 500-char persona summary the LLM extracted from agent_files. */
  persona: string;
  /** Full system prompt for the LLM `system` field at run time. */
  system_prompt: string;
  /** Tool whitelist (built-in tool ids). Empty = read tools only at runtime. */
  tools: string[];
  /** Optional model preference declared by the skill author. */
  model_hint?: string;
  /** Max tool-use iterations before runtime aborts the conversation. */
  max_iterations: number;
}

/**
 * Multi-agent team execution config.
 *
 * Shaped so that `assembler.ts` can build a `TeamDefV1` by lifting
 * `members_ids` + `edges_v1` + `policy_obj` directly and synthesizing
 * `agents: SkillAgentDef[]` from `members_personas`. See `assembler.ts`
 * Branch 1 for the actual conversion.
 *
 * `derivedFrom`:
 *   - `prose-llm`   : LLM read agent_files / docs and inferred the team
 *   - `structured`  : skill shipped explicit team yaml or `bmad-modules.yaml`
 *                     and the compiler trusted it verbatim
 *   - `fallback`    : LLM unavailable / JSON parse failed → rule-based guess
 *                     (members from agent file basenames, sequential edges)
 */
export interface CompiledTeamConfig {
  /** Stable team id — usually the skill_id for compiler-emitted teams. */
  team_id: string;
  version: 1;
  name: string;
  description?: string;
  /** Agent ids the team runs (executor in DAG topological order). */
  members_ids: string[];
  /**
   * Per-member persona text. The assembler synthesizes a `SkillAgentDef`
   * carrying this as the agent's system prompt when the skill bundle does
   * not ship a real `<agent>.agent.yaml`.
   */
  members_personas: Record<string, string>;
  /** DAG edges with default kind = 'sequential'. */
  edges_v1: TeamEdgeV1[];
  /** Retry / escalation / per-step timeout policy. */
  policy_obj: TeamPolicyV1;
  /** Provenance — see jsdoc on this interface. */
  derivedFrom: 'structured' | 'prose-llm' | 'fallback';
}

/**
 * LLM call accounting — surfaces in compile cache + status endpoint (PR-E).
 *
 * `model` is the actual provider/model id used (`<provider>:<model>`); for
 * fallback compiles we record `model: 'fallback'` and zero tokens so cost
 * telemetry can distinguish degraded paths.
 */
export interface CompileLlmMeta {
  model: string;
  tokens_in: number;
  tokens_out: number;
  duration_ms: number;
}

/**
 * Top-level cache entry — the only artifact the compiler writes to disk.
 *
 * Invariant: exactly one of `agentConfig` / `teamConfig` is populated,
 * matching `mode`. Schema validation in `index.ts` enforces this; the
 * fallback path in `fallback.ts` also obeys it.
 */
export interface CompiledSkill {
  skill_id: string;
  /** Mirrors `SkillReadOutput.content_hash` so cache lookups are unified. */
  source_content_hash: string;
  /** ISO 8601 UTC timestamp of compilation. */
  compiled_at: string;
  /** Bumping this invalidates caches. 'v1' for LLM/fallback; structured path
   *  appends a SPEC fingerprint (`v1+spec:<hash>`) so changing path_a_structured
   *  busts its caches. */
  compiler_version: string;
  mode: CompiledMode;
  agentConfig?: CompiledAgentConfig;
  teamConfig?: CompiledTeamConfig;
  llm_call_meta: CompileLlmMeta;
}

/** Re-export edge kind for downstream typecheckers. */
export type { TeamEdgeV1, TeamPolicyV1, EdgeKind };
