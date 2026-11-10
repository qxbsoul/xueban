/**
 * teams.ts — Team DAG router (S0.7)
 *
 * Mounts BEFORE proxyFallback in index.ts. Only handles the DAG-specific
 * paths we own (`/:id/dag`, `/:id/dag/validate`). Other /api/teams paths
 * (root list, create, etc.) fall through Express's no-match to the proxy
 * → Python sqlite backend.
 *
 * Endpoints:
 *   GET  /api/teams/:id/dag           → load yaml + return {team, agents, errors}
 *   PUT  /api/teams/:id/dag           → write dag_layout back to yaml
 *   POST /api/teams/:id/dag/validate  → re-validate without persisting
 *
 * Yaml is source of truth (S0.5). Frontend BlueprintCanvas drags a node →
 * PUT writes the new {x,y} into dag_layout.nodes and flips dag_layout.mode
 * to 'manual'. Auto layout becomes opt-in once a manual placement exists.
 */

import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import { Router, Request, Response } from 'express';
import {
  loadTeam,
  clearTeamCache,
  validateDag,
  type DagLayout,
} from '../lib/team-yaml';
import { listAgents as listYamlAgents } from '../lib/agent-yaml';
import {
  generateDagDraft,
  type DagDraftAgent,
} from '../lib/team-dag-draft';
import { resolveProviderKey } from '../transport/byokKey';
import { isProviderId, PROVIDER_IDS, type ProviderId } from '../transport/api-clients';

const router = Router();

const VALID_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

const PYTHON_BASE = process.env.PYTHON_BACKEND_URL ?? 'http://localhost:8000';

// Mirror of the path resolution in team-yaml.ts. Kept local because the
// writer needs the same precedence (root wins over local) when picking
// where to overwrite.
const ROOT_TEAMS_DIR = path.join(process.cwd(), '..', '.xueban', 'teams');
const LOCAL_TEAMS_DIR = path.join(process.cwd(), '.xueban', 'teams');

function resolveTeamFile(teamId: string): string | null {
  if (!VALID_ID_RE.test(teamId)) return null;
  for (const dir of [ROOT_TEAMS_DIR, LOCAL_TEAMS_DIR]) {
    const fp = path.join(dir, `${teamId}.team.yaml`);
    if (fs.existsSync(fp)) return fp;
  }
  return null;
}

// ─── GET /api/teams/:id/dag ──────────────────────────────────────────────────

router.get('/:id/dag', (req: Request, res: Response) => {
  const teamId = req.params.id;
  if (!VALID_ID_RE.test(teamId)) {
    res.status(400).json({ error: { code: 'INVALID_ID', message: 'invalid team id' } });
    return;
  }
  const result = loadTeam(teamId);
  if (!result.team) {
    res.status(404).json({
      error: { code: 'TEAM_NOT_FOUND', message: 'team yaml not found', detail: result.errors },
    });
    return;
  }
  const { team, resolvedAgents, errors } = result;
  res.json({
    data: {
      team_id: team.team_id,
      name: team.name,
      mode: team.mode,
      policy: team.policy_obj,
      members: team.members_ids,
      edges: team.edges_v1,
      dag_layout: team.dag_layout ?? { mode: 'auto', nodes: [], viewport: { zoom: 1, pan_x: 0, pan_y: 0 } },
      resolved_agents: resolvedAgents.map((a) => ({
        id: a.id,
        title: a.title,
        sub: a.sub,
        avatar_char: a.avatar_char,
        type: a.type,
        model_id: a.model.id,
      })),
    },
    meta: { errors },
  });
});

// ─── PUT /api/teams/:id/dag ──────────────────────────────────────────────────
// Body: { dag_layout: DagLayout }
// Writes the dag_layout block back to the yaml file (atomic .tmp + rename).
// Other yaml fields (members, edges, policy) untouched.

router.put('/:id/dag', (req: Request, res: Response) => {
  const teamId = req.params.id;
  if (!VALID_ID_RE.test(teamId)) {
    res.status(400).json({ error: { code: 'INVALID_ID', message: 'invalid team id' } });
    return;
  }
  const body = (req.body ?? {}) as { dag_layout?: unknown };
  const incoming = body.dag_layout;
  if (!incoming || typeof incoming !== 'object') {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'dag_layout required' } });
    return;
  }
  // Shape-validate. Reject silently-invalid input rather than letting bad
  // yaml land in the file.
  const dl = incoming as Record<string, unknown>;
  const mode = dl.mode === 'manual' ? 'manual' : 'auto';
  const nodes = Array.isArray(dl.nodes)
    ? (dl.nodes as unknown[]).flatMap((n) => {
        if (n && typeof n === 'object') {
          const no = n as Record<string, unknown>;
          if (typeof no.id === 'string' && typeof no.x === 'number' && typeof no.y === 'number') {
            return [{ id: no.id, x: no.x, y: no.y }];
          }
        }
        return [];
      })
    : [];
  const vp = (dl.viewport ?? {}) as Record<string, unknown>;
  const newLayout: DagLayout = {
    mode,
    nodes,
    viewport: {
      zoom: typeof vp.zoom === 'number' ? vp.zoom : 1,
      pan_x: typeof vp.pan_x === 'number' ? vp.pan_x : 0,
      pan_y: typeof vp.pan_y === 'number' ? vp.pan_y : 0,
    },
  };

  const filePath = resolveTeamFile(teamId);
  if (!filePath) {
    res.status(404).json({ error: { code: 'TEAM_NOT_FOUND', message: 'team yaml not found' } });
    return;
  }

  // D1: write-time DAG validation. The PUT only mutates dag_layout coordinates,
  // but we MUST NOT persist a layout onto a team whose members/edges are
  // inconsistent (orphan members, edges pointing at non-members, sequential
  // cycles). Reuse validateDag (the same function /dag/validate exposes) so the
  // write path enforces what the preflight only suggested. conditional edges are
  // intentionally excluded from cycle detection by validateDag — we keep that
  // semantics by not touching it here.
  const loaded = loadTeam(teamId);
  if (!loaded.team) {
    res.status(404).json({
      error: { code: 'TEAM_NOT_FOUND', message: 'team yaml not loadable', detail: loaded.errors },
    });
    return;
  }
  const dagErrors = validateDag(loaded.team, loaded.resolvedAgents);
  if (dagErrors.length > 0) {
    // errors are hard failures: validateDag returns a flat string[] with no
    // warning channel, so every entry blocks the write (422, not persisted).
    res.status(422).json({
      error: {
        code: 'DAG_INVALID',
        message: 'team DAG is invalid; refusing to save layout',
        errors: dagErrors,
      },
    });
    return;
  }

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    res.status(500).json({ error: { code: 'READ_FAILED', message: (err as Error).message } });
    return;
  }
  let parsed: unknown;
  try {
    parsed = yaml.load(raw);
  } catch (err) {
    res.status(500).json({ error: { code: 'PARSE_FAILED', message: (err as Error).message } });
    return;
  }
  if (!parsed || typeof parsed !== 'object') {
    res.status(500).json({ error: { code: 'BAD_YAML', message: 'not a mapping' } });
    return;
  }
  const teamObj = parsed as Record<string, unknown>;
  teamObj.dag_layout = newLayout;

  // Atomic write — same pattern as session-store.ts.
  const tmp = `${filePath}.tmp`;
  try {
    fs.writeFileSync(tmp, yaml.dump(teamObj, { lineWidth: 100, noRefs: true }), 'utf-8');
    fs.renameSync(tmp, filePath);
  } catch (err) {
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
    res.status(500).json({ error: { code: 'WRITE_FAILED', message: (err as Error).message } });
    return;
  }

  clearTeamCache();
  res.json({ data: { team_id: teamId, dag_layout: newLayout }, meta: {} });
});

// ─── POST /api/teams/:id/dag/validate ────────────────────────────────────────
// Re-runs validateDag on the current yaml without persisting. Useful for
// the frontend "save" preflight (catch ref errors before PUT).

router.post('/:id/dag/validate', (req: Request, res: Response) => {
  const teamId = req.params.id;
  if (!VALID_ID_RE.test(teamId)) {
    res.status(400).json({ error: { code: 'INVALID_ID', message: 'invalid team id' } });
    return;
  }
  const result = loadTeam(teamId);
  if (!result.team) {
    res.status(404).json({ error: { code: 'TEAM_NOT_FOUND', message: 'team yaml not found' } });
    return;
  }
  const validateErrors = validateDag(result.team, result.resolvedAgents);
  res.json({
    data: {
      team_id: teamId,
      ok: validateErrors.length === 0 && result.errors.length === 0,
      errors: [...result.errors, ...validateErrors],
    },
    meta: {},
  });
});

// ─── POST /api/teams/dag-draft ───────────────────────────────────────────────
// 自然语言 → DAG 草稿（不落库）。Body: { prompt: string, agentIds?: string[] }。
// 把 prompt + 可选 roster（含各自 role 摘要）喂给 BYOK LLM，要求严格 JSON
// { nodes, edges }；D1/D2 校验不合法则回喂错误让模型重试一次，仍不合法 → 422；
// LLM 自身失败 → 502。前端 TeamDagEditor「AI 生成草稿」消费。

/** 解析 roster：Python agent 库优先，yaml 模板兜底；找不到的 id 原样带出。 */
async function resolveDraftRoster(agentIds: string[]): Promise<DagDraftAgent[]> {
  const wanted = new Set(agentIds);
  const found = new Map<string, DagDraftAgent>();
  try {
    const pyRes = await fetch(`${PYTHON_BASE}/api/agents`);
    if (pyRes.ok) {
      const env = (await pyRes.json()) as {
        data?: Array<{ agent_id?: unknown; name?: unknown; soul?: unknown }>;
      };
      for (const a of env.data ?? []) {
        if (typeof a.agent_id !== 'string' || !wanted.has(a.agent_id)) continue;
        found.set(a.agent_id, {
          id: a.agent_id,
          name: typeof a.name === 'string' && a.name ? a.name : a.agent_id,
          role: typeof a.soul === 'string' ? a.soul : undefined,
        });
      }
    }
  } catch {
    /* Python 不可达 — 退到 yaml 模板 + 裸 id */
  }
  try {
    for (const a of listYamlAgents().agents) {
      if (!wanted.has(a.id) || found.has(a.id)) continue;
      found.set(a.id, {
        id: a.id,
        name: a.title || a.id,
        role: a.persona.split('\n').find((l) => l.trim().length > 0)?.trim() ?? a.sub,
      });
    }
  } catch {
    /* yaml 库不可读 — 裸 id 兜底 */
  }
  return agentIds.map((id) => found.get(id) ?? { id, name: id });
}

/** 选 provider：X-LLM-Provider 头优先，否则第一个有 key 可解析的 provider。 */
function pickDraftProvider(
  headers: Record<string, string | string[] | undefined>,
): { provider?: ProviderId; apiKey?: string; model?: string } {
  const raw = headers['x-llm-provider'];
  const headerProvider = (Array.isArray(raw) ? raw[0] : raw)?.trim().toLowerCase();
  const normalized = headerProvider === 'claude' ? 'anthropic' : headerProvider;
  const rawModel = headers['x-llm-model'];
  const model = (Array.isArray(rawModel) ? rawModel[0] : rawModel)?.trim() || undefined;
  if (normalized && isProviderId(normalized)) {
    const key = resolveProviderKey(normalized, headers);
    if (key) return { provider: normalized, apiKey: key, model };
  }
  for (const p of PROVIDER_IDS) {
    const key = resolveProviderKey(p, headers);
    if (key) return { provider: p, apiKey: key, model };
  }
  return { model };
}

router.post('/dag-draft', async (req: Request, res: Response) => {
  const body = (req.body ?? {}) as { prompt?: unknown; agentIds?: unknown };
  const prompt = typeof body.prompt === 'string' ? body.prompt.trim() : '';
  if (!prompt) {
    res.status(400).json({ error: { code: 'INVALID_BODY', message: 'prompt (non-empty string) required' } });
    return;
  }
  const agentIds = Array.isArray(body.agentIds)
    ? body.agentIds.filter((a): a is string => typeof a === 'string' && a.trim().length > 0)
    : [];
  const roster = agentIds.length > 0 ? await resolveDraftRoster(agentIds) : [];

  const { provider, apiKey, model } = pickDraftProvider(req.headers);
  if (!provider || !apiKey) {
    res.status(502).json({
      error: { code: 'LLM_NO_KEY', message: 'no BYOK provider key available for dag drafting' },
    });
    return;
  }

  const ac = new AbortController();
  // 监听 res 的提前 close（客户端断连）——req 的 'close' 在 JSON body 收完
  // 就会触发，会误杀正常的 LLM 调用。
  res.on('close', () => {
    if (!res.writableEnded) ac.abort();
  });

  const outcome = await generateDagDraft({
    userText: prompt,
    agents: roster,
    provider,
    apiKey,
    model,
    signal: ac.signal,
  });
  if (!outcome.ok) {
    if (outcome.kind === 'llm') {
      res.status(502).json({ error: { code: 'LLM_FAILED', message: outcome.message } });
      return;
    }
    res.status(422).json({
      error: {
        code: 'DAG_DRAFT_INVALID',
        message: 'generated draft failed DAG validation (after one retry)',
        errors: outcome.errors,
      },
    });
    return;
  }
  res.json({ data: { draft: outcome.draft }, meta: {} });
});

export default router;
