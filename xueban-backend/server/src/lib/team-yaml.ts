/**
 * team-yaml.ts — S0.5
 *
 * Global team library loader. Replaces the per-skill `team.skill.yaml` (which
 * was inside `.xueban/skills/<id>/`) with a single global library at
 * `.xueban/teams/<id>.team.yaml`.
 *
 * A team references its agent members by id; this loader resolves them via
 * agent-yaml.ts loadAgent() so the returned TeamDef carries full agent data
 * for downstream consumers (parser, SSE synthesizer, AgentDetail UI).
 *
 * DAG validation: detect cycles, orphan members, and unresolved agent refs.
 * Reports errors but never throws — callers (S0.7 API route) decide whether
 * to 400 or warn.
 *
 * ⚠️ D1(2026-06-03 plan-eng-review 锁定):team 的 edges/policy 单一真源 =
 *    Python JSON(`.xueban/teams/<id>.json`),运行期读 `team-source.loadTeamForRun`。
 *    本 yaml 仅保留 dag_layout(坐标)+ 编译 skill 的静态蓝图;**禁止**再把用户
 *    保存的 edges/policy 往 yaml 写,否则重新分叉。新读路径一律走 team-source。
 */

import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import { loadAgent } from './agent-yaml';
import type { SkillAgentDef, TeamDef } from './skill-types';

export const ROOT_TEAMS_DIR = path.join(process.cwd(), '..', '.xueban', 'teams');
export const LOCAL_TEAMS_DIR = path.join(process.cwd(), '.xueban', 'teams');

export const VALID_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

// ─── Schema (extends TeamDef with v1 fields the S0.5 yaml adds) ──────────────
//
// TeamDef in skill-types.ts is the historical shape (S6.0). The new yaml has
// richer fields (description, policy as object, edge.kind, dag_layout). We
// keep TeamDef back-compat and put new fields on a wider interface.

/**
 * EdgeKind — 团队 DAG 边类型。
 * 2026-11-05 新增 'approve'（OpenBot 审批纪律对齐）：流转前人工审批的顺序边。
 * 参与分层与执行顺序约束（同 sequential），但 chat 执行器在该节点跑之前挂起等
 * 用户批准（POST /api/groups/:id/approvals/:nodeId decide）；拒绝 → 下游标
 * blocked，其出边下游按失败传播知情。Python 侧 mode:'approve' 此前被 mapMode
 * 折叠成 sequential（审批语义在读路径丢失），本类型扩展后打通。
 */
export type EdgeKind = 'sequential' | 'parallel' | 'conditional' | 'approve';

export interface TeamEdgeV1 {
  from: string;
  to: string;
  kind?: EdgeKind;
  condition?: string;
  max_retries?: number;
}

export interface TeamPolicyV1 {
  retry?: number;
  escalation?: string;
  timeout_per_step_ms?: number;
}

export interface DagLayoutNode {
  id: string;
  x: number;
  y: number;
}

export interface DagLayout {
  mode: 'auto' | 'manual';
  nodes: DagLayoutNode[];
  viewport: { zoom: number; pan_x: number; pan_y: number };
}

export interface TeamDefV1 extends TeamDef {
  /** Stable team id (matches filename and registry key). */
  team_id: string;
  version: number;
  description?: string;
  policy_obj: TeamPolicyV1;
  members_ids: string[];
  edges_v1: TeamEdgeV1[];
  dag_layout?: DagLayout;
}

// ─── Cache ───────────────────────────────────────────────────────────────────

interface CacheEntry {
  mtime: number;
  team: TeamDefV1;
}
const cache = new Map<string, CacheEntry>();

function resolveTeamFile(teamId: string): string | null {
  if (!VALID_ID_RE.test(teamId)) return null;
  for (const dir of [ROOT_TEAMS_DIR, LOCAL_TEAMS_DIR]) {
    const fp = path.join(dir, `${teamId}.team.yaml`);
    if (fs.existsSync(fp)) return fp;
  }
  return null;
}

export interface TeamLoadResultV1 {
  team: TeamDefV1 | null;
  /** Fully-resolved member agents (skipped on cache hit; caller can re-resolve). */
  resolvedAgents: SkillAgentDef[];
  errors: string[];
}

/**
 * Load a team yaml by id, resolve all members via loadAgent(), validate DAG.
 * Non-fatal errors are surfaced in `errors[]`; `team` is null only when the
 * file itself is unreadable / unparseable.
 */
export function loadTeam(teamId: string): TeamLoadResultV1 {
  if (!VALID_ID_RE.test(teamId)) {
    return { team: null, resolvedAgents: [], errors: [`invalid team id: ${teamId}`] };
  }
  const filePath = resolveTeamFile(teamId);
  if (!filePath) {
    return { team: null, resolvedAgents: [], errors: [`team yaml not found: ${teamId}.team.yaml`] };
  }
  let stat: fs.Stats;
  try {
    stat = fs.statSync(filePath);
  } catch (err) {
    return { team: null, resolvedAgents: [], errors: [`stat ${filePath}: ${(err as Error).message}`] };
  }

  const cached = cache.get(teamId);
  if (cached && cached.mtime === stat.mtimeMs) {
    // re-resolve members on every call (agent cache layer handles its own mtime)
    const { resolvedAgents, errors: memberErrs } = resolveMembers(cached.team.members_ids);
    return { team: cached.team, resolvedAgents, errors: memberErrs };
  }

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    return { team: null, resolvedAgents: [], errors: [`read ${filePath}: ${(err as Error).message}`] };
  }

  let parsed: unknown;
  try {
    parsed = yaml.load(raw);
  } catch (err) {
    return { team: null, resolvedAgents: [], errors: [`parse ${filePath}: ${(err as Error).message}`] };
  }
  if (!parsed || typeof parsed !== 'object') {
    return { team: null, resolvedAgents: [], errors: [`${filePath} is not a YAML mapping`] };
  }
  const o = parsed as Record<string, unknown>;
  if (typeof o.id !== 'string' || o.id !== teamId) {
    return { team: null, resolvedAgents: [], errors: [`${filePath} id mismatch (got "${o.id}")`] };
  }
  if (typeof o.name !== 'string') {
    return { team: null, resolvedAgents: [], errors: [`${filePath} missing name`] };
  }
  if (!Array.isArray(o.members) || o.members.length === 0) {
    return { team: null, resolvedAgents: [], errors: [`${filePath} needs at least one member`] };
  }
  const members_ids = (o.members as unknown[]).filter(
    (m): m is string => typeof m === 'string',
  );

  const errors: string[] = [];

  // Resolve members through agent library
  const { resolvedAgents, errors: memberErrs } = resolveMembers(members_ids);
  errors.push(...memberErrs);

  // Parse edges
  const edges_v1: TeamEdgeV1[] = Array.isArray(o.edges)
    ? (o.edges as unknown[]).flatMap((e) => {
        if (e && typeof e === 'object') {
          const eo = e as Record<string, unknown>;
          if (typeof eo.from === 'string' && typeof eo.to === 'string') {
            return [{
              from: eo.from,
              to: eo.to,
              kind: (eo.kind as EdgeKind | undefined) ?? 'sequential',
              condition: typeof eo.condition === 'string' ? eo.condition : undefined,
              max_retries: typeof eo.max_retries === 'number' ? eo.max_retries : undefined,
            }];
          }
        }
        errors.push(`bad edge entry: ${JSON.stringify(e)}`);
        return [];
      })
    : [];

  // Parse policy
  const policy_obj: TeamPolicyV1 = {};
  if (o.policy && typeof o.policy === 'object') {
    const p = o.policy as Record<string, unknown>;
    if (typeof p.retry === 'number') policy_obj.retry = p.retry;
    if (typeof p.escalation === 'string') policy_obj.escalation = p.escalation;
    if (typeof p.timeout_per_step_ms === 'number')
      policy_obj.timeout_per_step_ms = p.timeout_per_step_ms;
  } else if (typeof o.policy === 'string') {
    // back-compat: old yaml had `policy: strict` as string. Map to retry default.
    policy_obj.retry = typeof o.retry === 'number' ? o.retry : 3;
  }

  // Parse dag_layout
  let dag_layout: DagLayout | undefined;
  if (o.dag_layout && typeof o.dag_layout === 'object') {
    const dl = o.dag_layout as Record<string, unknown>;
    const vp = (dl.viewport ?? {}) as Record<string, unknown>;
    dag_layout = {
      mode: dl.mode === 'manual' ? 'manual' : 'auto',
      nodes: Array.isArray(dl.nodes)
        ? (dl.nodes as unknown[]).flatMap((n) => {
            if (n && typeof n === 'object') {
              const no = n as Record<string, unknown>;
              if (typeof no.id === 'string' && typeof no.x === 'number' && typeof no.y === 'number') {
                return [{ id: no.id, x: no.x, y: no.y }];
              }
            }
            return [];
          })
        : [],
      viewport: {
        zoom: typeof vp.zoom === 'number' ? vp.zoom : 1.0,
        pan_x: typeof vp.pan_x === 'number' ? vp.pan_x : 0,
        pan_y: typeof vp.pan_y === 'number' ? vp.pan_y : 0,
      },
    };
  }

  const team: TeamDefV1 = {
    team_id: teamId,
    version: typeof o.version === 'number' ? o.version : 1,
    name: o.name,
    description: typeof o.description === 'string' ? o.description : undefined,
    mode: o.mode === 'parallel' || o.mode === 'dag' || o.mode === 'custom'
      ? (o.mode as 'parallel' | 'dag')  // 'custom' falls through narrow type
      : 'serial',
    policy: policy_obj.retry !== undefined ? 'strict' : 'permissive',  // legacy field
    policy_obj,
    retry: policy_obj.retry ?? 3,
    members_ids,
    edges_v1,
    edges: edges_v1.map(e => ({ from: e.from, to: e.to })),  // legacy field
    agents: resolvedAgents,  // legacy field
    dag_layout,
    loaded_at: Date.now(),
    source_dir: path.dirname(filePath),
  };

  // DAG validation
  const dagErrors = validateDag(team, resolvedAgents);
  errors.push(...dagErrors);

  cache.set(teamId, { mtime: stat.mtimeMs, team });
  return { team, resolvedAgents, errors };
}

/** Resolve a list of agent ids through the agent library. Bad ids → errors[]. */
function resolveMembers(ids: string[]): { resolvedAgents: SkillAgentDef[]; errors: string[] } {
  const resolvedAgents: SkillAgentDef[] = [];
  const errors: string[] = [];
  for (const id of ids) {
    const r = loadAgent(id);
    if ('error' in r) {
      errors.push(`member ${id}: ${r.error}`);
    } else {
      resolvedAgents.push(r);
    }
  }
  return { resolvedAgents, errors };
}

/**
 * DAG validation: cycle detection (excluding conditional retry-back edges),
 * orphan member detection, unresolved agent ref detection.
 *
 * Returns non-fatal error strings. Empty array = valid.
 */
export function validateDag(team: TeamDefV1, agents: SkillAgentDef[]): string[] {
  const errs: string[] = [];

  // 1. member ↔ resolved agent consistency
  const resolvedIds = new Set(agents.map(a => a.id));
  for (const memberId of team.members_ids) {
    if (!resolvedIds.has(memberId)) {
      errs.push(`member "${memberId}" not resolvable to any agents/*.agent.yaml`);
    }
  }

  // 2. edge endpoints must be members
  const memberSet = new Set(team.members_ids);
  for (const e of team.edges_v1) {
    if (!memberSet.has(e.from)) {
      errs.push(`edge from "${e.from}" → "${e.to}" : from is not a team member`);
    }
    if (!memberSet.has(e.to)) {
      errs.push(`edge from "${e.from}" → "${e.to}" : to is not a team member`);
    }
  }

  // 3. orphan members (no incoming + no outgoing edges, when team has >1 member)
  if (team.members_ids.length > 1) {
    const hasEdge = new Set<string>();
    for (const e of team.edges_v1) {
      hasEdge.add(e.from);
      hasEdge.add(e.to);
    }
    for (const m of team.members_ids) {
      if (!hasEdge.has(m)) {
        errs.push(`member "${m}" has no incoming or outgoing edges (orphan)`);
      }
    }
  }

  // 4. cycle detection (sequential edges only — conditional retry-back is allowed)
  // 2026-11-05 — approve 边同为顺序边（流转前人工审批），同样参与环检测。
  const seqEdges = team.edges_v1.filter(e => e.kind === 'sequential' || e.kind === 'approve' || e.kind === undefined);
  const cycle = findCycle(team.members_ids, seqEdges);
  if (cycle) {
    errs.push(`cycle detected via sequential edges: ${cycle.join(' → ')}`);
  }

  return errs;
}

function findCycle(nodes: string[], edges: TeamEdgeV1[]): string[] | null {
  const graph = new Map<string, string[]>();
  for (const n of nodes) graph.set(n, []);
  for (const e of edges) {
    if (!graph.has(e.from)) graph.set(e.from, []);
    graph.get(e.from)!.push(e.to);
  }
  const visited = new Set<string>();
  const stack = new Set<string>();
  const path: string[] = [];
  function dfs(n: string): string[] | null {
    if (stack.has(n)) {
      const startIdx = path.indexOf(n);
      return path.slice(startIdx).concat(n);
    }
    if (visited.has(n)) return null;
    visited.add(n);
    stack.add(n);
    path.push(n);
    for (const next of graph.get(n) ?? []) {
      const r = dfs(next);
      if (r) return r;
    }
    stack.delete(n);
    path.pop();
    return null;
  }
  for (const n of nodes) {
    if (!visited.has(n)) {
      const r = dfs(n);
      if (r) return r;
    }
  }
  return null;
}

/** List all team ids found in either directory. */
export function listTeams(): { teams: TeamDefV1[]; errors: string[] } {
  const teams: TeamDefV1[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  for (const dir of [ROOT_TEAMS_DIR, LOCAL_TEAMS_DIR]) {
    if (!fs.existsSync(dir)) continue;
    let files: string[];
    try {
      files = fs.readdirSync(dir);
    } catch (err) {
      errors.push(`readdir ${dir}: ${(err as Error).message}`);
      continue;
    }
    for (const f of files) {
      if (!f.endsWith('.team.yaml')) continue;
      const id = f.slice(0, -'.team.yaml'.length);
      if (seen.has(id)) continue;
      seen.add(id);
      const result = loadTeam(id);
      if (result.team) {
        teams.push(result.team);
        errors.push(...result.errors);
      } else {
        errors.push(...result.errors);
      }
    }
  }
  return { teams, errors };
}

export function clearTeamCache(): void {
  cache.clear();
}

// ─── TeamDefV1 synthesizers ──────────────────────────────────────────────────
//
// Round 4 PR-C: assembler used to synthesize TeamDefV1 from the legacy
// `SkillDefinition.team` shape (TeamDef). With the SkillCompiler in place,
// teams can also be synthesized from a CompiledTeamConfig (which carries
// `members_personas` instead of pre-resolved `SkillAgentDef[]`). Both helpers
// live here so callers don't have to duplicate the field synthesis logic.

/**
 * Convert a legacy `TeamDef` (the shape `SkillDefinition.team` historically
 * carries from `team.skill.yaml` / `team_ref`) into a `TeamDefV1` that
 * `workflow/scheduler.runDag()` consumes. Synthesises v1 fields
 * (`members_ids`, `edges_v1`, `policy_obj`) from the legacy fields so old
 * skills still execute on the new DAG engine. This used to live inside
 * `assembler.ts`; moved here in PR-C as part of the compile-config rewrite.
 */
export function toTeamDefV1(team: TeamDef): TeamDefV1 {
  return {
    ...team,
    team_id: team.name,
    version: 1,
    description: undefined,
    policy_obj: {
      retry: team.retry ?? 3,
    },
    members_ids: team.agents.map((a) => a.id),
    // Legacy edges have no `kind` — default to sequential so the scheduler's
    // Kahn-layered topology drives each agent after its parent completes.
    edges_v1: team.edges.map((e) => ({
      from: e.from,
      to: e.to,
      kind: 'sequential' as const,
    })),
  };
}

/**
 * Build a `TeamDefV1` from PR-C's `CompiledTeamConfig` plus an `agents:
 * SkillAgentDef[]` array (synthesized by the assembler from
 * `members_personas`). This is the **post-compile** equivalent of
 * `toTeamDefV1` — the input now carries explicit edges_v1/policy_obj
 * already, so we just stitch the legacy fields back on for downstream
 * scheduler/observer interop.
 *
 * @param compiled  output of `compile()` — exactly one teamConfig populated
 * @param agents    SkillAgentDef[] in the same order as compiled.members_ids;
 *                  the assembler synthesizes these from members_personas
 *                  (lightweight SkillAgentDef with persona-only payload).
 */
export function toTeamDefV1FromCompiled(
  compiled: {
    team_id: string;
    name: string;
    description?: string;
    members_ids: string[];
    edges_v1: TeamEdgeV1[];
    policy_obj: TeamPolicyV1;
  },
  agents: SkillAgentDef[],
): TeamDefV1 {
  return {
    name: compiled.name,
    team_id: compiled.team_id,
    version: 1,
    description: compiled.description,
    mode: 'serial',
    policy: compiled.policy_obj.retry !== undefined ? 'strict' : 'permissive',
    policy_obj: compiled.policy_obj,
    retry: compiled.policy_obj.retry ?? 3,
    members_ids: compiled.members_ids,
    edges_v1: compiled.edges_v1,
    edges: compiled.edges_v1.map((e) => ({ from: e.from, to: e.to })),
    agents,
    loaded_at: Date.now(),
    source_dir: '<compiled>',
  };
}
