/**
 * skill-yaml.ts — S6.0
 *
 * Loads `<skill>/team.skill.yaml` + the per-agent `<agent>.skill.yaml`
 * files referenced from it, parses them via js-yaml, computes per-slot
 * token counts, and produces a TeamDef ready for SSE injection.
 *
 * Cache strategy: keyed by skill dir, invalidated by team.skill.yaml mtime.
 * No cross-process locking — the loader is read-only and a stale cache for
 * a few seconds during edits is acceptable.
 */

import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import type {
  SkillAgentDef,
  SkillAnchor,
  SkillSlot,
  TeamDef,
} from './skill-types';

const cache = new Map<string, { mtime: number; team: TeamDef }>();

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
  agentFile: string,
  agent: Partial<SkillAgentDef>,
): Record<SkillSlot, SkillAnchor> {
  const slots: SkillSlot[] = ['persona', 'model', 'tools', 'memory', 'io'];
  const result = {} as Record<SkillSlot, SkillAnchor>;
  for (const s of slots) {
    result[s] = {
      ref: `${agentFile}#${s}`,
      tokens: estimateTokens(slotBody(s, agent)),
      cached: false,
    };
  }
  return result;
}

function loadAgentYaml(
  skillDir: string,
  agentFile: string,
): SkillAgentDef | { error: string } {
  const fp = path.join(skillDir, agentFile);
  let raw: string;
  try {
    raw = fs.readFileSync(fp, 'utf-8');
  } catch (err) {
    return { error: `read ${agentFile} failed: ${(err as Error).message}` };
  }
  let parsed: unknown;
  try {
    parsed = yaml.load(raw);
  } catch (err) {
    return { error: `parse ${agentFile} failed: ${(err as Error).message}` };
  }
  if (!parsed || typeof parsed !== 'object') {
    return { error: `${agentFile} is not a YAML mapping` };
  }
  const o = parsed as Record<string, unknown>;
  if (typeof o.id !== 'string' || typeof o.title !== 'string') {
    return { error: `${agentFile} missing required id/title` };
  }
  if (typeof o.persona !== 'string') {
    return { error: `${agentFile} missing persona body` };
  }
  if (!o.model || typeof o.model !== 'object') {
    return { error: `${agentFile} missing model spec` };
  }
  if (!o.tools || typeof o.tools !== 'object') {
    return { error: `${agentFile} missing tools spec` };
  }
  // Coerce a partial structure with defensive defaults.
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
    source_file: agentFile,
  };
  agent.anchors = buildAnchors(agentFile, agent);
  return agent as SkillAgentDef;
}

export interface TeamLoadResult {
  team: TeamDef | null;
  errors: string[];
}

/** Load `<skillDir>/team.skill.yaml` and all referenced agent yamls. */
export function loadTeam(skillDir: string): TeamLoadResult {
  const teamFile = path.join(skillDir, 'team.skill.yaml');
  if (!fs.existsSync(teamFile)) {
    return { team: null, errors: [] };
  }
  let stat: fs.Stats;
  try {
    stat = fs.statSync(teamFile);
  } catch (err) {
    return { team: null, errors: [`stat team.skill.yaml: ${(err as Error).message}`] };
  }
  const cached = cache.get(skillDir);
  if (cached && cached.mtime === stat.mtimeMs) {
    return { team: cached.team, errors: [] };
  }

  let teamRaw: string;
  try {
    teamRaw = fs.readFileSync(teamFile, 'utf-8');
  } catch (err) {
    return { team: null, errors: [`read team.skill.yaml: ${(err as Error).message}`] };
  }
  let teamData: unknown;
  try {
    teamData = yaml.load(teamRaw);
  } catch (err) {
    return { team: null, errors: [`parse team.skill.yaml: ${(err as Error).message}`] };
  }
  if (!teamData || typeof teamData !== 'object') {
    return { team: null, errors: [`team.skill.yaml is not a mapping`] };
  }
  const t = teamData as Record<string, unknown>;
  if (typeof t.name !== 'string') {
    return { team: null, errors: [`team.skill.yaml missing name`] };
  }
  if (!Array.isArray(t.agents) || t.agents.length === 0) {
    return { team: null, errors: [`team.skill.yaml needs at least one agent entry`] };
  }

  const errors: string[] = [];
  const agents: SkillAgentDef[] = [];
  for (const rawEntry of t.agents as unknown[]) {
    if (!rawEntry || typeof rawEntry !== 'object') {
      errors.push(`agent entry is not a mapping`);
      continue;
    }
    const entry = rawEntry as Record<string, unknown>;
    if (typeof entry.skill_ref !== 'string') {
      errors.push(`agent ${entry.id ?? '?'} missing skill_ref`);
      continue;
    }
    const result = loadAgentYaml(skillDir, entry.skill_ref);
    if ('error' in result) {
      errors.push(result.error);
      continue;
    }
    // team-level overrides win for display attrs but not for content slots
    if (typeof entry.title === 'string') result.title = entry.title;
    if (typeof entry.sub === 'string') result.sub = entry.sub;
    if (typeof entry.avatar_char === 'string' && entry.avatar_char.length > 0)
      result.avatar_char = entry.avatar_char.charAt(0);
    if (entry.type === 'coordinator' || entry.type === 'agent') result.type = entry.type;
    agents.push(result);
  }

  if (agents.length === 0) {
    return { team: null, errors: errors.length > 0 ? errors : ['no agents loaded'] };
  }

  const edges: TeamDef['edges'] = Array.isArray(t.edges)
    ? (t.edges as unknown[]).flatMap((e) => {
        if (e && typeof e === 'object') {
          const eo = e as Record<string, unknown>;
          if (typeof eo.from === 'string' && typeof eo.to === 'string') {
            return [{ from: eo.from, to: eo.to }];
          }
        }
        errors.push(`bad edge entry: ${JSON.stringify(e)}`);
        return [];
      })
    : [];

  const team: TeamDef = {
    name: t.name,
    mode: t.mode === 'parallel' || t.mode === 'dag' ? t.mode : 'serial',
    policy: t.policy === 'permissive' ? 'permissive' : 'strict',
    retry: typeof t.retry === 'number' ? t.retry : 3,
    agents,
    edges,
    loaded_at: Date.now(),
    source_dir: skillDir,
  };

  cache.set(skillDir, { mtime: stat.mtimeMs, team });
  return { team, errors };
}

/** Convenience — get a single agent by id, or undefined. */
export function getAgent(team: TeamDef, agentId: string): SkillAgentDef | undefined {
  return team.agents.find((a) => a.id === agentId);
}

/** Convenience — get the body text for an `<agent>#<slot>` reference. */
export function getAnchorBody(
  team: TeamDef,
  agentId: string,
  slot: SkillSlot,
): { body: string; tokens: number } | undefined {
  const agent = getAgent(team, agentId);
  if (!agent) return undefined;
  const body = slotBody(slot, agent);
  return { body, tokens: estimateTokens(body) };
}

/** Clear the in-memory cache — exposed for tests / `POST /skills/reload`. */
export function clearTeamCache(): void {
  cache.clear();
}
