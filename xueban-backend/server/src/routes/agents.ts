/**
 * agents.ts — thin forwarder to Python FastAPI (port 8000) + local yaml merge.
 *
 * 2026-05-28 split-brain fix (see memory/bug_quick_hire_dangling_agent.md).
 *
 * Background: Node used to own /api/agents POST/GET/DELETE on its own sqlite
 * store (`storage/agents.ts`, randomUUID() ids). But /api/teams falls through
 * to Python (this router only owns DAG sub-paths), so every team got created
 * on Python with `agent_ids` pointing at Node-only UUIDs that Python had never
 * heard of. Result: every "帮我创建一个开发工程师" team had a dangling
 * agent_id, the chat group thought it had a member but the member did not
 * exist in the team store's universe.
 *
 * Fix: agents move to Python (single source of truth, matches the
 * CLAUDE.md dual-backend rule that Python owns teams/groups/inbox). Node's
 * /api/agents POST + DELETE now forward to Python. GET still merges Python's
 * agent list with local `.xueban/agents/*.agent.yaml` templates so the
 * existing "agents + design-time soul templates" UI keeps working without a
 * Python-side schema change.
 *
 * The legacy sqlite store (storage/agents.ts) is now dormant but kept in
 * place — internal callers / tests still import it, and zero rows is a safe
 * state. A follow-up sweep can remove it once nothing else reads it.
 *
 * Frontend client: src/api/agents.ts
 *   - All success responses wrap payload in { data, meta } (Envelope<T>).
 *   - DELETE returns 204 with no body.
 */

import { Router, Request, Response } from 'express';
import { listAgents as listYamlAgents, deleteAgent as deleteYamlAgent } from '../lib/agent-yaml';
import {
  applyAgentOverrides,
  loadAgentOverrides,
  saveAgentOverrides,
  type AgentOverrideField,
} from '../lib/agent-overrides';
import type { AgentRecord } from '../storage/agents';

const router = Router();

const PYTHON_BASE =
  process.env.PYTHON_BACKEND_URL ?? 'http://localhost:8000';

/** Map a yaml template to the AgentRecord shape FE expects (mirrors the
 *  yaml-template branch of the old `listAllAgents` for byte-compatibility). */
function yamlToRecord(workspaceId: string | undefined): AgentRecord[] {
  const yamlResult = listYamlAgents();
  return yamlResult.agents.map((a) => ({
    agent_id: a.id,
    name: a.title,
    soul:
      a.persona.split('\n').find((l) => l.trim().length > 0)?.trim() ??
      a.sub ??
      '',
    workspace_id: workspaceId ?? 'default',
    blueprint: {
      capabilities: { tools: a.tools.picked },
      llm_provider: 'claude',
      model: a.model.id,
      persona_ref: a.anchors.persona.ref,
      yaml_source_file: a.source_file,
    },
    status: 'idle' as const,
    source: 'yaml-template' as const,
    created_at: '1970-01-01T00:00:00.000Z',
  }));
}

/** yaml 模板 record + 运行时 overrides（Claw 绑定 / 头像）合并后的展示 record。 */
function yamlToRecordWithOverrides(workspaceId: string | undefined): AgentRecord[] {
  return yamlToRecord(workspaceId).map((r) =>
    applyAgentOverrides(r as unknown as Record<string, unknown>, loadAgentOverrides(r.agent_id)) as unknown as AgentRecord,
  );
}

/** Forward arbitrary body to Python, return its raw response. */
async function forwardJson(
  method: 'POST' | 'DELETE',
  pathSuffix: string,
  body: unknown,
): Promise<{ status: number; payload: unknown }> {
  const url = `${PYTHON_BASE}/api/agents${pathSuffix}`;
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined && method !== 'DELETE') {
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const text = await res.text();
  let payload: unknown = text;
  if (text.length > 0) {
    try {
      payload = JSON.parse(text);
    } catch {
      // not JSON — bubble up raw text under a synthetic error envelope
      payload = { error: { code: 'PYTHON_NON_JSON', message: text } };
    }
  }
  return { status: res.status, payload };
}

// ─── GET /api/agents — Python agents + local yaml templates merged ──────────
router.get('/', async (req: Request, res: Response) => {
  const workspaceId =
    typeof req.query.workspace_id === 'string'
      ? req.query.workspace_id
      : undefined;
  // ?source=sqlite is the legacy "no yaml merge" flag — now means
  // "Python-only, skip yaml templates".
  const noYamlMerge = req.query.source === 'sqlite';

  // Python agents (real, persisted)
  let pythonAgents: AgentRecord[] = [];
  try {
    const qs = workspaceId
      ? `?workspace_id=${encodeURIComponent(workspaceId)}`
      : '';
    const pyRes = await fetch(`${PYTHON_BASE}/api/agents${qs}`);
    if (pyRes.ok) {
      const env = (await pyRes.json()) as { data?: AgentRecord[] };
      pythonAgents = Array.isArray(env.data) ? env.data : [];
    } else {
      // Python down or unhappy — return what we can (yaml-only) but log.
      // eslint-disable-next-line no-console
      console.warn(
        `[agents-route] Python /api/agents returned ${pyRes.status}; falling back to yaml-only`,
      );
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      '[agents-route] Python /api/agents fetch failed; falling back to yaml-only:',
      err,
    );
  }

  if (noYamlMerge) {
    res.json({ data: pythonAgents, meta: {} });
    return;
  }

  // Merge with local yaml templates. Python wins on id collision (defensive —
  // in practice they don't collide: Python uses `agent-*`, yaml uses semantic
  // names like "reader", "pm").
  const pyIds = new Set(pythonAgents.map((a) => a.agent_id));
  const yamlAgents = yamlToRecordWithOverrides(workspaceId).filter(
    (a) => !pyIds.has(a.agent_id),
  );
  res.json({ data: [...pythonAgents, ...yamlAgents], meta: {} });
});

// ─── GET /api/agents/:id — Python first, fall back to local yaml template ───
// 2026-10-31 — 此前缺这个路由：单个 GET 落到 proxyFallback 直打 Python，
// Python 只认 .xueban/agents/*.json（agent-*），Node 侧合并进列表的
// .agent.yaml 模板（dev/arch/writer 等语义 id）全部 404 —— 右栏「灵魂」tab
// 打开即 Agent API error 404。现对齐 GET '/' 的合并语义：Python 命中返回；
// 404/网络失败 → 查 yaml 模板；都没有 → 404。
router.get('/:id', async (req: Request, res: Response) => {
  const id = req.params.id;
  const workspaceId =
    typeof req.query.workspace_id === 'string' ? req.query.workspace_id : undefined;

  // registry / routing-log 是 DAG 子路径资源，不属于单个 agent。
  if (id === 'registry' || id === 'routing-log') {
    res.status(404).json({ error: { code: 'AGENT_NOT_FOUND', message: `Agent '${id}' not found` } });
    return;
  }

  // Python first（真实持久化 agent-*）
  try {
    const qs = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : '';
    const pyRes = await fetch(`${PYTHON_BASE}/api/agents/${encodeURIComponent(id)}${qs}`);
    if (pyRes.ok) {
      res.status(pyRes.status).json(await pyRes.json());
      return;
    }
    if (pyRes.status !== 404 && pyRes.status !== 400) {
      // 非「不存在」类错误也回落 yaml（列表语义如此，保持一致）
      // eslint-disable-next-line no-console
      console.warn(`[agents-route] Python GET /:id returned ${pyRes.status}; falling back to yaml`);
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[agents-route] Python GET /:id failed; falling back to yaml:', err);
  }

  // yaml 模板回落（语义 id：dev / arch / writer / pm …）；overrides（头像等
  // 运行时字段）一并贴回，保证 GET /:id 与列表语义一致。
  const tpl = yamlToRecordWithOverrides(workspaceId).find((a) => a.agent_id === id);
  if (tpl) {
    res.json({ data: tpl, meta: {} });
    return;
  }
  res.status(404).json({ error: { code: 'AGENT_NOT_FOUND', message: `Agent '${id}' not found` } });
});

// ─── POST /api/agents — forward to Python ───────────────────────────────────
router.post('/', async (req: Request, res: Response) => {
  try {
    const { status, payload } = await forwardJson('POST', '', req.body);
    res.status(status).json(payload);
  } catch (err) {
    res.status(502).json({
      error: {
        code: 'PYTHON_UNREACHABLE',
        message: `Cannot reach Python at ${PYTHON_BASE}: ${
          err instanceof Error ? err.message : String(err)
        }`,
      },
    });
  }
});

// ─── DELETE /api/agents/:id — Python first, fall back to local yaml ──────────
// Real Python agents (`agent-*`) delete via Python. Local yaml soul-templates
// (semantic ids like "reader"/"arch" surfaced by the GET merge) aren't in
// Python → it 404s → we delete their .xueban/agents/<id>.agent.yaml file
// instead, so the talent pool's delete works for both kinds.
router.delete('/:id', async (req: Request, res: Response) => {
  const id = req.params.id;
  try {
    const { status, payload } = await forwardJson(
      'DELETE',
      `/${encodeURIComponent(id)}`,
      undefined,
    );
    if (status === 204 || status === 200) {
      res.status(204).send();
      return;
    }
    // Not a Python agent — try the local yaml template.
    if (status === 404 && deleteYamlAgent(id)) {
      res.status(204).send();
      return;
    }
    res.status(status).json(payload);
  } catch (err) {
    // Python unreachable — a yaml-template delete can still succeed locally.
    if (deleteYamlAgent(id)) {
      res.status(204).send();
      return;
    }
    res.status(502).json({
      error: {
        code: 'PYTHON_UNREACHABLE',
        message: `Cannot reach Python at ${PYTHON_BASE}: ${
          err instanceof Error ? err.message : String(err)
        }`,
      },
    });
  }
});

// ─── PATCH /api/agents/:id — Python first, fall back to yaml overrides ──────
// 2026-11-04 — 此前缺 PATCH：保存头像 / Claw 绑定（灵魂页「保存头像」钮）落到
// proxyFallback 直打 Python，Python 只认 agent-*.json，语义 id（pm / dev /
// reader…）一律 404 AGENT_NOT_FOUND → 前端红条 Agent API error 404。GET 有
// yaml 回退而 PATCH 没有，属合并语义缺口。现对齐：Python 命中转发；404/不可达
// → 该 id 是 yaml 模板时，把白名单字段（Claw 绑定 / 头像）写 sidecar
// <id>.overrides.json（GET 合并时贴回）。设计时字段（name/soul/skills/tools）
// 不进 sidecar —— yaml 模板本体仍由模板文件/re-seed 管理。
interface AgentPatchBody {
  name?: unknown;
  soul?: unknown;
  skills?: unknown;
  tools?: unknown;
  claw_id?: unknown;
  avatar_color?: unknown;
  avatar_style?: unknown;
  avatar_seed?: unknown;
  avatar_image?: unknown;
}

const OVERRIDE_BODY_KEYS: AgentOverrideField[] = [
  'claw_id',
  'avatar_color',
  'avatar_style',
  'avatar_seed',
  'avatar_image',
];

router.patch('/:id', async (req: Request, res: Response) => {
  const id = req.params.id;
  const body = (req.body ?? {}) as AgentPatchBody;

  // Python first（真实持久化 agent-*：PATCH 语义全字段走 Python 原端点）
  try {
    const pyRes = await fetch(`${PYTHON_BASE}/api/agents/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (pyRes.ok) {
      res.status(pyRes.status).json(await pyRes.json());
      return;
    }
    if (pyRes.status !== 404 && pyRes.status !== 400) {
      // 非「不存在」类错误原样透传（保持 Python 错误语义，不吞）
      res.status(pyRes.status).json(await pyRes.json().catch(() => ({ error: { code: 'PYTHON_ERROR', message: `Python PATCH returned ${pyRes.status}` } })));
      return;
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[agents-route] Python PATCH /:id failed; falling back to yaml overrides:', err);
  }

  // yaml 模板回落：仅白名单展示层字段可覆盖；name/soul 等设计时字段明确拒绝
  const isYamlTemplate = listYamlAgents().agents.some((a) => a.id === id);
  if (!isYamlTemplate) {
    res.status(404).json({ error: { code: 'AGENT_NOT_FOUND', message: `Agent '${id}' not found` } });
    return;
  }
  const hasDesignTimeFields = (
    ['name', 'soul', 'skills', 'tools'] as const
  ).some((k) => body[k] !== undefined);
  if (hasDesignTimeFields) {
    res.status(422).json({
      error: {
        code: 'YAML_TEMPLATE_READONLY',
        message: `Agent '${id}' is a yaml soul-template: name/soul/skills/tools are design-time fields — edit the .agent.yaml file instead. Avatar / Claw binding fields are supported.`,
      },
    });
    return;
  }
  const patch: Partial<Record<AgentOverrideField, string | null>> = {};
  for (const key of OVERRIDE_BODY_KEYS) {
    const v = body[key];
    if (v === undefined) continue;
    if (typeof v !== 'string' && v !== null) {
      res.status(422).json({ error: { code: 'INVALID_FIELD', message: `${key} must be a string or null` } });
      return;
    }
    patch[key as AgentOverrideField] = v as string | null;
  }
  const saved = saveAgentOverrides(id, patch);
  if (!saved) {
    res.status(500).json({ error: { code: 'OVERRIDES_WRITE_FAILED', message: `Failed to write overrides for '${id}'` } });
    return;
  }
  // 返回与 GET /:id 相同形状的合并 record（前端 patchAgent 用返回值刷新状态）
  const record = yamlToRecordWithOverrides(undefined).find((a) => a.agent_id === id);
  res.json({ data: record, meta: {} });
});

export default router;
