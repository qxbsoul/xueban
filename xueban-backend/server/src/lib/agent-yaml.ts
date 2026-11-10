/**
 * agent-yaml.ts — S0.5
 *
 * Global agent library loader. Replaces the per-skill embedded
 * `<agent>.skill.yaml` files (which were inside `.xueban/skills/<id>/`)
 * with a single global library at `.xueban/agents/<id>.agent.yaml`.
 *
 * Why global: one agent (e.g. `reader`) can be referenced by multiple teams
 * (paper-review + literature-survey + ...). Previously the same persona had
 * to be copy-pasted into each skill's yaml. Now a team references agent by
 * id and the loader resolves to the single source-of-truth file.
 *
 * Cache strategy: keyed by agent id, invalidated by file mtime. No watch —
 * caller can clearAgentCache() between runs.
 */

import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import type {
  SkillAgentDef,
  SkillAnchor,
  SkillSlot,
} from './skill-types';
// NOTE: atomic-fs.ts (S0.6) not yet built; agent-yaml is read-only for now
// so we don't need atomic writes here.

// ─── Paths ───────────────────────────────────────────────────────────────────
// The Node server runs from `<repo>/server/`, so process.cwd() resolves there.
// Demo bundles + user-edited agents live at `<repo>/.xueban/agents/`,
// which is `cwd/../.xueban/agents/` from the server's perspective. Mirror
// the dual-dir pattern from skill-loader.ts.
const ROOT_AGENTS_DIR = path.join(process.cwd(), '..', '.xueban', 'agents');
const LOCAL_AGENTS_DIR = path.join(process.cwd(), '.xueban', 'agents');

const VALID_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

// ─── Cache ───────────────────────────────────────────────────────────────────

interface CacheEntry {
  mtime: number;
  agent: SkillAgentDef;
}
const cache = new Map<string, CacheEntry>();

/** Coarse token count — chars/4. Accurate enough for the provenance label. */
function estimateTokens(text: string): number {
  return Math.max(0, Math.ceil(text.length / 4));
}

/** Stringify a slot value so we can token-count it uniformly. */
function slotBody(slot: SkillSlot, agent: Partial<SkillAgentDef>): string {
  switch (slot) {
    case 'persona':
      return agent.persona ?? '';
    case 'model':
      return JSON.stringify(agent.model ?? {});
    case 'tools':
      return JSON.stringify(agent.tools ?? {});
    case 'memory':
      return agent.memory ?? '';
    case 'io':
      return JSON.stringify(agent.io ?? {});
  }
}

function buildAnchors(
  agentSource: string,
  agent: Partial<SkillAgentDef>,
): Record<SkillSlot, SkillAnchor> {
  const slots: SkillSlot[] = ['persona', 'model', 'tools', 'memory', 'io'];
  const result = {} as Record<SkillSlot, SkillAnchor>;
  for (const s of slots) {
    result[s] = {
      ref: `${agentSource}#${s}`,
      tokens: estimateTokens(slotBody(s, agent)),
      cached: false,
    };
  }
  return result;
}

/** Resolve an agent id to its yaml file on disk, checking root first then server-local. */
function resolveAgentFile(agentId: string): string | null {
  if (!VALID_ID_RE.test(agentId)) return null;
  const candidates = [
    path.join(ROOT_AGENTS_DIR, `${agentId}.agent.yaml`),
    path.join(LOCAL_AGENTS_DIR, `${agentId}.agent.yaml`),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

export interface AgentLoadError {
  error: string;
}

export type AgentLoadResult = SkillAgentDef | AgentLoadError;

/**
 * Load a single agent yaml by id. Returns the parsed agent or an error.
 * Uses mtime cache — re-reads only when the file changes.
 */
export function loadAgent(agentId: string): AgentLoadResult {
  if (!VALID_ID_RE.test(agentId)) {
    return { error: `invalid agent id: ${agentId}` };
  }
  const filePath = resolveAgentFile(agentId);
  if (!filePath) {
    return { error: `agent yaml not found: ${agentId}.agent.yaml` };
  }
  let stat: fs.Stats;
  try {
    stat = fs.statSync(filePath);
  } catch (err) {
    return { error: `stat ${filePath}: ${(err as Error).message}` };
  }
  const cached = cache.get(agentId);
  if (cached && cached.mtime === stat.mtimeMs) return cached.agent;

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    return { error: `read ${filePath}: ${(err as Error).message}` };
  }
  let parsed: unknown;
  try {
    parsed = yaml.load(raw);
  } catch (err) {
    return { error: `parse ${filePath}: ${(err as Error).message}` };
  }
  if (!parsed || typeof parsed !== 'object') {
    return { error: `${filePath} is not a YAML mapping` };
  }
  const o = parsed as Record<string, unknown>;
  if (typeof o.id !== 'string' || typeof o.title !== 'string') {
    return { error: `${filePath} missing required id/title` };
  }
  if (o.id !== agentId) {
    return { error: `${filePath} id="${o.id}" does not match filename "${agentId}"` };
  }
  if (typeof o.persona !== 'string') {
    return { error: `${filePath} missing persona body` };
  }
  if (!o.model || typeof o.model !== 'object') {
    return { error: `${filePath} missing model spec` };
  }
  if (!o.tools || typeof o.tools !== 'object') {
    return { error: `${filePath} missing tools spec` };
  }

  const agentSource = `${agentId}.agent.yaml`;
  const agent: Partial<SkillAgentDef> = {
    id: o.id,
    title: o.title,
    sub: typeof o.sub === 'string' ? o.sub : undefined,
    avatar_char:
      typeof o.avatar_char === 'string' && o.avatar_char.length > 0
        ? o.avatar_char.charAt(0)
        : o.title.charAt(0),
    type: o.type === 'coordinator' ? 'coordinator' : 'agent',
    persona: o.persona,
    model: o.model as SkillAgentDef['model'],
    tools: o.tools as SkillAgentDef['tools'],
    memory: typeof o.memory === 'string' ? o.memory : undefined,
    io: (o.io as SkillAgentDef['io']) ?? undefined,
    source_file: agentSource,
  };
  agent.anchors = buildAnchors(agentSource, agent);
  const resolved = agent as SkillAgentDef;
  cache.set(agentId, { mtime: stat.mtimeMs, agent: resolved });
  return resolved;
}

/** List all agent ids found in either directory. Errors are surfaced separately. */
export function listAgents(): { agents: SkillAgentDef[]; errors: string[] } {
  const agents: SkillAgentDef[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  for (const dir of [ROOT_AGENTS_DIR, LOCAL_AGENTS_DIR]) {
    if (!fs.existsSync(dir)) continue;
    let files: string[];
    try {
      files = fs.readdirSync(dir);
    } catch (err) {
      errors.push(`readdir ${dir}: ${(err as Error).message}`);
      continue;
    }
    for (const f of files) {
      if (!f.endsWith('.agent.yaml')) continue;
      const id = f.slice(0, -'.agent.yaml'.length);
      if (seen.has(id)) continue;  // root wins over local
      seen.add(id);
      const result = loadAgent(id);
      if ('error' in result) {
        errors.push(result.error);
      } else {
        agents.push(result);
      }
    }
  }
  return { agents, errors };
}

/**
 * Delete an agent's yaml file (the design-time soul template under
 * `.xueban/agents/<id>.agent.yaml`). Returns true if a file was removed,
 * false if no yaml existed for this id.
 *
 * Used by the agents route's DELETE fallback: yaml templates have semantic ids
 * ("reader" / "arch" / "critic"…), not Python's `agent-*`, so Python returns
 * 404 for them — we delete the local file instead. Re-seeding restores them.
 */
export function deleteAgent(agentId: string): boolean {
  const filePath = resolveAgentFile(agentId);
  if (!filePath) return false;
  try {
    fs.unlinkSync(filePath);
  } catch {
    return false;
  }
  clearAgentCache();
  return true;
}

/** Clear the in-memory cache — useful for tests / hot reload. */
export function clearAgentCache(): void {
  cache.clear();
}
