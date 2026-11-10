/**
 * skill-anchors.ts — Tool **schema** catalog for future LLM-driven skill assembly.
 *
 * Post-Phase 2 status (2026-05-22 onward):
 *   - The Phase 2 decision A3 (daemon-led DAG) replaced the LLM tool_use
 *     orchestration these tools were built for. There is NO runtime caller
 *     of these schemas in the current codebase.
 *   - The 4 ToolSpec definitions are retained as a reference for a possible
 *     future "LLM-driven explicit DAG assembly" mode. If that ever comes
 *     back, the executor implementations were preserved in git history at
 *     commit `7e9fe80` (server/src/lib/tools/skill-anchors.ts in that revision).
 *
 * Why keep the schemas at all:
 *   - They document the contract a future LLM-driven mode would have to
 *     honour (list_team_agents / get_skill_anchor / register_agent / register_edge).
 *   - The `disable-model-invocation` mechanism in skill-loader.ts can opt
 *     specific user skills back into the tool-use path; the schemas here are
 *     the canonical definition of what those tools would expose to the LLM.
 *
 * Removed in this commit (commit follow-up to 7e9fe80 — P2-8 cleanup):
 *   - All 4 executor implementations (execListTeamAgents / execGetSkillAnchor /
 *     execRegisterAgent / execRegisterEdge) and the `skillAnchorExecutors`
 *     dispatch map.
 *   - Helper functions only used by the executors (isRecord, errResult,
 *     isSkillSlot, isEdgeKind, slotBodyVerbatim).
 *   - The test file `__tests__/skill-anchors.test.ts` which exclusively
 *     exercised the deleted executors.
 *
 * See:
 *   - docs/architecture/orchestration-transport.md §"Phase 2" for the
 *     decision rationale (A3 daemon-led DAG, A6 unified callable path)
 *   - server/src/workflow/scheduler.ts for the replacement
 */

import type { ToolSpec } from '../tool-spec';

/**
 * What a tool returns to the runtime. `output` is fed straight back to the
 * LLM as the `tool_result.output` JSON-encoded blob. `sseEvents` is the side
 * channel for register_agent / register_edge — the runtime yields each entry
 * downstream verbatim. `isError` flips the `is_error` flag on the
 * `tool_result` ContentBlock.
 *
 * Retained as a type-only export — no concrete executor uses it today; a
 * future LLM-driven mode that re-implements the four tools would return this
 * shape.
 */
export interface ToolExecutionResult {
  output: unknown;
  sseEvents?: Array<{ event: string; data: unknown }>;
  isError?: boolean;
}

export type ToolExecutor = (input: unknown) => Promise<ToolExecutionResult>;

// ─── ToolSpec catalog ───────────────────────────────────────────────────────
//
// All four specs are `source: 'base'`. They are NOT registered with any
// runtime today. A future LLM-driven assembly mode would import this array
// and register the corresponding executors on a ToolRegistry.

export const skillAnchorTools: ToolSpec[] = [
  {
    name: 'list_team_agents',
    description:
      'List candidate agents declared by a team (resolved from .xueban/teams/<skill_id>.team.yaml). ' +
      'Returns a short summary per agent — title, type, persona token count, model id, picked-tool count. ' +
      'Use this at the very start of phase 2 to enumerate who you can hire.',
    input_schema: {
      type: 'object',
      properties: {
        skill_id: { type: 'string', description: 'Team / skill identifier (filename stem).' },
      },
      required: ['skill_id'],
      additionalProperties: false,
    },
    source: 'base',
  },
  {
    name: 'get_skill_anchor',
    description:
      'Fetch one slot (persona | model | tools | memory | io) of one agent verbatim from its agent yaml. ' +
      'Returns { ref, tokens, body }. The body is byte-identical to the file — do NOT paraphrase or trim it; ' +
      'echo it directly into <sf:agent-persona> / <sf:agent-model> / etc.',
    input_schema: {
      type: 'object',
      properties: {
        skill_id: { type: 'string', description: 'Team / skill identifier.' },
        agent_id: { type: 'string', description: 'Agent id (must be a team member).' },
        slot: {
          type: 'string',
          enum: ['persona', 'model', 'tools', 'memory', 'io'],
          description: 'Which provenance slot to fetch.',
        },
      },
      required: ['skill_id', 'agent_id', 'slot'],
      additionalProperties: false,
    },
    source: 'base',
  },
  {
    name: 'register_agent',
    description:
      'Declare a single team member, emitting an `event: "node"` SSE frame for the UI to render. ' +
      'Pass persona / model / tools you previously fetched via get_skill_anchor verbatim.',
    input_schema: {
      type: 'object',
      properties: {
        id: { type: 'string', description: 'Agent id (filename stem of the .agent.yaml).' },
        title: { type: 'string' },
        sub: { type: 'string' },
        avatar_char: { type: 'string' },
        type: { type: 'string', enum: ['agent', 'coordinator'] },
        persona: { type: 'string' },
        model: { type: 'object' },
        tools: { type: 'object' },
        memory: { type: 'string' },
        io: { type: 'object' },
        source_file: { type: 'string' },
      },
      required: ['id', 'title', 'persona', 'model', 'tools'],
      additionalProperties: true,
    },
    source: 'base',
  },
  {
    name: 'register_edge',
    description:
      'Declare a DAG edge from one agent to another, emitting an `event: "edge"` SSE frame. ' +
      'kind defaults to "sequential". For "conditional" kind, supply a condition expression (expr-eval syntax).',
    input_schema: {
      type: 'object',
      properties: {
        from: { type: 'string' },
        to: { type: 'string' },
        kind: { type: 'string', enum: ['sequential', 'parallel', 'conditional'] },
        condition: { type: 'string' },
        max_retries: { type: 'number' },
      },
      required: ['from', 'to'],
      additionalProperties: false,
    },
    source: 'base',
  },
];
