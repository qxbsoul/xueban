/**
 * team-dag-draft.ts — 自然语言 → Team DAG 草稿（对标 StaffDeck 状态机 SOP 上半段）。
 *
 * 用户一句话描述协作流程，LLM 生成 DAG 草稿（只返回不落库），降低
 * 「从零手搭团队拓扑」的门槛。与 orchestrator-parse.ts 同构：
 *
 *   1. `composeDagDraftPrompt` — 组装指令（用户描述 + 可选的现有 agent 名单
 *      含 role），要求模型输出严格 JSON { nodes, edges }；
 *   2. `parseDagDraftJson` — 纯函数：剥 ``` 围栏、容忍前后杂文、归一化边
 *      mode（未知值收敛到 direct）、节点 id 去重（首个生效）；
 *   3. `validateDagDraft` — 纯函数，镜像 D1/D2 规则（Python
 *      team_validation.validate_workflow / 前端 teamDagValidation 的同构）：
 *      well-formed 边、禁自环、端点必须指向已知节点、顺序边（direct/approve）
 *      不成环（conditional 是合法回边，不参与环检测）；D2：调用方给了 roster
 *      时，node.agentId 必须来自名单；
 *   4. `generateDagDraft` — IO：走 BYOK provider（callProvider，与
 *      /api/llm/messages 同一后端），校验不合法把错误清单回喂模型重试一次，
 *      仍不合法 → kind:'invalid'；LLM 自身失败 → kind:'llm'（路由映射 502）。
 *
 * 纯函数（compose/parse/validate）与 IO（generate）分离，便于单测。
 */
import { callProvider } from '../transport/api-clients';

/** 草稿边模式。conditional 是合法回边（分支/重试），不参与环检测。 */
export type DagDraftEdgeMode = 'direct' | 'approve' | 'conditional';

export interface DagDraftNode {
  id: string;
  /** 引用现有 agent（roster 内 id）；LLM 新设想角色时缺省。 */
  agentId?: string;
  name?: string;
  role?: string;
}

export interface DagDraftEdge {
  source: string;
  target: string;
  mode: DagDraftEdgeMode;
}

export interface DagDraft {
  nodes: DagDraftNode[];
  edges: DagDraftEdge[];
}

/** 给 LLM 的现有 agent 名单条目（role = soul/职责简介摘要）。 */
export interface DagDraftAgent {
  id: string;
  name: string;
  role?: string;
}

export type DagDraftOutcome =
  | { ok: true; draft: DagDraft }
  | { ok: false; kind: 'llm'; message: string }
  | { ok: false; kind: 'invalid'; errors: string[] };

/** 归一化边 mode：approve/conditional 严格匹配，其余（含 undefined）→ direct。 */
export function normalizeDraftEdgeMode(mode: unknown): DagDraftEdgeMode {
  if (mode === 'approve') return 'approve';
  if (mode === 'conditional') return 'conditional';
  return 'direct';
}

/** Compose the drafting instruction. Exported for unit tests. */
export function composeDagDraftPrompt(
  userText: string,
  agents: DagDraftAgent[],
  prevErrors?: string[],
): string {
  const roster = agents
    .map((a) => {
      const roleExcerpt = (a.role ?? '').trim().slice(0, 120);
      return `- id: ${a.id} | name: ${a.name}${roleExcerpt ? ` | role: ${roleExcerpt}` : ''}`;
    })
    .join('\n');
  const lines = [
    '你是多 Agent 团队的拓扑设计师。根据用户的一句话描述，生成一个团队协作 DAG 草稿。',
    '',
  ];
  if (agents.length > 0) {
    lines.push(
      '## 可用 Agent 名单（节点的 agentId 必须来自这里；用不上的不要列入）',
      roster,
      '',
    );
  } else {
    lines.push(
      '## Agent',
      '没有给定现有 agent 名单：为每个节点起一个语义化 id 和 name/role（即将创建的新角色）。',
      '',
    );
  }
  lines.push(
    '## 用户描述',
    userText,
    '',
    '## 输出要求',
    '只输出一个 JSON 对象（可包在 ```json 围栏里），不要其他文字：',
    '{ "nodes": [{ "id": "<节点 id>", "agentId": "<名单内 agent id，可选>", "name": "<显示名>", "role": "<职责一句话>" }], "edges": [{ "source": "<节点 id>", "target": "<节点 id>", "mode": "direct|approve|conditional" }] }',
    '规则：mode 只能是 direct（直接流转）/ approve（流转前人工审批）/ conditional（条件回边，允许指回上游）；',
    '边的 source/target 必须是 nodes 里的 id；禁止自环；direct/approve 边不能成环（conditional 除外）。',
  );
  if (prevErrors && prevErrors.length > 0) {
    lines.push(
      '',
      '## 上一稿的校验错误（必须全部修正后再输出）',
      ...prevErrors.map((e) => `- ${e}`),
    );
  }
  return lines.join('\n');
}

/**
 * Extract + normalize the LLM's draft JSON. Tolerates ```json fences and
 * surrounding prose. Unknown edge modes fold to 'direct'; duplicate node ids
 * keep the first occurrence. Returns null when no usable draft remains
 * (unparseable, or zero nodes).
 */
export function parseDagDraftJson(raw: string): DagDraft | null {
  const text = raw.trim();
  if (!text) return null;

  const candidates: string[] = [];
  const fence = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
  if (fence?.[1]) candidates.push(fence[1].trim());
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start !== -1 && end > start) candidates.push(text.slice(start, end + 1));
  candidates.push(text);

  for (const candidate of candidates) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(candidate);
    } catch {
      continue;
    }
    if (!parsed || typeof parsed !== 'object') continue;
    const obj = parsed as Record<string, unknown>;
    if (!Array.isArray(obj.nodes)) continue;

    const seen = new Set<string>();
    const nodes: DagDraftNode[] = [];
    for (const item of obj.nodes) {
      if (!item || typeof item !== 'object') continue;
      const n = item as Record<string, unknown>;
      const id = typeof n.id === 'string' ? n.id.trim() : '';
      if (!id || seen.has(id)) continue;
      seen.add(id);
      nodes.push({
        id,
        ...(typeof n.agentId === 'string' && n.agentId.trim() ? { agentId: n.agentId.trim() } : {}),
        ...(typeof n.name === 'string' && n.name.trim() ? { name: n.name.trim() } : {}),
        ...(typeof n.role === 'string' && n.role.trim() ? { role: n.role.trim() } : {}),
      });
    }
    if (nodes.length === 0) continue;

    const edges: DagDraftEdge[] = [];
    if (Array.isArray(obj.edges)) {
      for (const item of obj.edges) {
        if (!item || typeof item !== 'object') continue;
        const e = item as Record<string, unknown>;
        if (typeof e.source !== 'string' || typeof e.target !== 'string') continue;
        if (!e.source.trim() || !e.target.trim()) continue;
        edges.push({
          source: e.source.trim(),
          target: e.target.trim(),
          mode: normalizeDraftEdgeMode(e.mode),
        });
      }
    }
    return { nodes, edges };
  }
  return null;
}

/** DFS 找环，返回环路径（收尾节点重复）或 null。镜像 team_validation._find_cycle。 */
function findDraftCycle(nodeIds: string[], edges: Array<{ source: string; target: string }>): string[] | null {
  const graph = new Map<string, string[]>();
  for (const id of nodeIds) graph.set(id, []);
  for (const e of edges) {
    const arr = graph.get(e.source) ?? [];
    arr.push(e.target);
    graph.set(e.source, arr);
  }

  const visited = new Set<string>();
  const stack = new Set<string>();
  const path: string[] = [];

  const dfs = (n: string): string[] | null => {
    if (stack.has(n)) return [...path.slice(path.indexOf(n)), n];
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
  };

  for (const n of [...graph.keys()]) {
    if (!visited.has(n)) {
      const r = dfs(n);
      if (r) return r;
    }
  }
  return null;
}

/**
 * D1/D2 draft validation (mirror of Python validate_workflow + validate_agent_ids):
 *   1. no self-loop
 *   2. edge endpoints must be node ids in the draft
 *   3. no cycle via sequential edges (direct/approve); conditional excluded
 *   4. D2 — when a roster was supplied, every node.agentId must come from it
 * Returns human-readable error strings (empty == valid). Never throws.
 */
export function validateDagDraft(draft: DagDraft, knownAgentIds?: readonly string[]): string[] {
  const errors: string[] = [];
  const nodeIds = draft.nodes.map((n) => n.id);
  const nodeSet = new Set(nodeIds);

  const seqEdges: Array<{ source: string; target: string }> = [];
  for (const e of draft.edges) {
    if (e.source === e.target) {
      errors.push(`self-loop edge "${e.source}" → "${e.target}" is not allowed`);
      continue;
    }
    if (!nodeSet.has(e.source)) {
      errors.push(`edge "${e.source}" → "${e.target}": "${e.source}" is not a node in the draft`);
    }
    if (!nodeSet.has(e.target)) {
      errors.push(`edge "${e.source}" → "${e.target}": "${e.target}" is not a node in the draft`);
    }
    // conditional 是合法回边，不参与环检测（镜像 _map_mode 语义）
    if (e.mode !== 'conditional') seqEdges.push({ source: e.source, target: e.target });
  }

  const cycle = findDraftCycle(nodeIds, seqEdges);
  if (cycle) {
    errors.push(`cycle detected via sequential edges: ${cycle.join(' → ')}`);
  }

  // D2: roster 给了就必须从名单里引用 agent
  if (knownAgentIds && knownAgentIds.length > 0) {
    const known = new Set(knownAgentIds);
    for (const n of draft.nodes) {
      if (!n.agentId) {
        errors.push(`node "${n.id}" has no agentId from the provided roster`);
      } else if (!known.has(n.agentId)) {
        errors.push(`node "${n.id}" references agent "${n.agentId}" not in the provided roster`);
      }
    }
  }

  return errors;
}

export interface DagDraftOptions {
  userText: string;
  agents: DagDraftAgent[];
  provider: string;
  apiKey: string;
  model?: string;
  signal?: AbortSignal;
  /** Override the provider call for tests. */
  callImpl?: typeof callProvider;
  maxTokens?: number;
}

async function callDraftLlm(
  opts: DagDraftOptions,
  prompt: string,
): Promise<{ text: string } | { error: string }> {
  const call = opts.callImpl ?? callProvider;
  let text = '';
  try {
    for await (const chunk of call(opts.provider, {
      systemPrompt:
        '你是多 Agent 团队的拓扑设计师。你的唯一职责：根据用户描述输出严格 JSON 的团队 DAG 草稿，无任何多余文字。',
      userMessage: prompt,
      model: opts.model,
      max_tokens: opts.maxTokens ?? 2000,
      temperature: 0.2,
      api_key: opts.apiKey,
      signal: opts.signal,
    })) {
      if (opts.signal?.aborted) return { error: 'aborted' };
      if (chunk.type === 'text-delta' && typeof chunk.text === 'string') {
        text += chunk.text;
      } else if (chunk.type === 'error') {
        return { error: chunk.message };
      }
    }
  } catch (err) {
    return { error: (err as Error).message ?? String(err) };
  }
  return { text };
}

/**
 * Generate a DAG draft from a one-line description. On parse/validation
 * failure the error list is fed back to the model for exactly ONE retry;
 * still invalid → { kind:'invalid', errors } (route maps to 422). Any LLM
 * failure → { kind:'llm' } (route maps to 502). Draft is never persisted.
 */
export async function generateDagDraft(opts: DagDraftOptions): Promise<DagDraftOutcome> {
  let prevErrors: string[] | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    const res = await callDraftLlm(opts, composeDagDraftPrompt(opts.userText, opts.agents, prevErrors));
    if ('error' in res) return { ok: false, kind: 'llm', message: res.error };

    const draft = parseDagDraftJson(res.text);
    if (!draft) {
      prevErrors = ['output was not a parseable JSON draft with a non-empty "nodes" array'];
      continue;
    }
    const errors = validateDagDraft(draft, opts.agents.map((a) => a.id));
    if (errors.length === 0) return { ok: true, draft };
    prevErrors = errors;
  }
  return { ok: false, kind: 'invalid', errors: prevErrors ?? ['draft validation failed'] };
}
