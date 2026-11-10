/**
 * team-persistence.ts — 组装落库（L2 闭环：team + agents 写入 Python 存储）
 *
 * 背景（2026-08-22 缺口盘点确诊）：chat-first 团队组装（agent-team-assembly
 * skill 的 5 步流，assembler.ts Branch 1 离线 emit / Branch 2 orchestrate /
 * Branch 0 recipe）此前只 emit 蓝图 SSE 事件 + 写 session artifact，从未把
 * 组装产物落到 Python 存储（.xueban/teams/*.json + .xueban/agents/*.json）
 * —— SKILL.md 声称的「节点 5 后 daemon 落库 team+agents」实际缺失。
 *
 * 本模块补这一环：组装完成（complete）时，把 roster 经 HTTP 写入 Python 后端
 * （POST /api/agents × N → POST /api/teams），与 groups-chat.ts 直调
 * PYTHON_BACKEND_URL 的既有架构一致。Python 侧 D2 写时校验（team 引用的 agent
 * 必须存在于 store）由「先建 agent、再建 team」的顺序天然满足。
 *
 * 纪律：所有失败路径只 console.warn + 返回 null —— 落库是组装的附带动作，
 * 绝不允许它打断或污染 SSE 事件流。
 *
 * 位置说明：本模块打 HTTP（非纯逻辑），按 lib/AGENTS.md「纯逻辑不动 network」
 * 的规则放在 src/ 根（与 assembler.ts / python-backend.ts 同层）。
 */

import { PYTHON_BACKEND_URL } from './python-backend';

/** 组装产出的单个 agent 草稿（来自 <sf:node> 事件 / compiled teamConfig / recipe）。 */
export interface AssembledAgentDraft {
  node_id: string;
  title: string;
  /** 完整 persona（<sf:agent-persona> / node.persona）；缺省时落库回退 title。 */
  persona?: string;
  model?: string;
  tools?: string[];
}

/** Python POST /api/teams 返回的 record 子集（我们只消费这几个字段）。 */
export interface PersistedTeam {
  team_id: string;
  name: string;
  agent_ids: string[];
  workspace_id: string;
}

interface Envelope<T> {
  data?: T;
}

/** 薄封装：POST Python，非 2xx / 网络错误都归一为 null（调用方兜底）。 */
async function pyPost<T>(path: string, body: unknown): Promise<T | null> {
  try {
    const res = await fetch(`${PYTHON_BACKEND_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      console.warn(`[team-persistence] POST ${path} → HTTP ${res.status}`);
      return null;
    }
    const json = (await res.json()) as Envelope<T> | T;
    return ((json as Envelope<T>).data ?? json) as T;
  } catch (err) {
    console.warn(`[team-persistence] POST ${path} failed: ${(err as Error).message ?? err}`);
    return null;
  }
}

/** 团队名兜底：goal 首个非空行截断（Python CreateTeamRequest 上限 120 字符）。 */
export function deriveTeamName(goal: string): string {
  const firstLine = (goal ?? '')
    .split('\n')
    .map((l) => l.trim())
    .find((l) => l.length > 0);
  return (firstLine || '新团队').slice(0, 40);
}

/**
 * persistAssembledTeam — 把一次组装的 roster 落库。
 *
 * 顺序：逐个 POST /api/agents（quick create；name=title，soul=完整 persona，
 * 契约对齐 src/api/agents.ts 的「组建保存」注释）→ 收集 agent_id →
 * POST /api/teams。单个 agent 创建失败只跳过该成员；全败或 team 创建失败
 * 返回 null。roster 按 node_id 去重（同 id 后到的覆盖先到的，与前端 reducer
 * 的 upsert 语义一致）。
 */
export async function persistAssembledTeam(opts: {
  session_id: string;
  goal: string;
  workspace_id?: string;
  /** 显式团队名（如 compiled teamConfig.name）；缺省由 goal 派生。 */
  name?: string;
  roster: AssembledAgentDraft[];
}): Promise<PersistedTeam | null> {
  const byNodeId = new Map<string, AssembledAgentDraft>();
  for (const r of opts.roster) {
    if (r && typeof r.node_id === 'string' && r.node_id) byNodeId.set(r.node_id, r);
  }
  const roster = [...byNodeId.values()];
  if (roster.length === 0) return null;

  const agentIds: string[] = [];
  for (const a of roster) {
    const title = (a.title || a.node_id).trim().slice(0, 100) || a.node_id;
    const soul = (a.persona?.trim() || a.title || a.node_id).slice(0, 8000);
    const created = await pyPost<{ agent_id?: string }>('/api/agents', {
      name: title,
      soul,
      ...(opts.workspace_id ? { workspace_id: opts.workspace_id } : {}),
      ...(a.model ? { model: a.model } : {}),
      ...(a.tools && a.tools.length > 0 ? { tools: a.tools.slice(0, 64) } : {}),
    });
    if (created?.agent_id) {
      agentIds.push(created.agent_id);
    } else {
      console.warn(`[team-persistence] session=${opts.session_id} agent "${a.node_id}" 落库失败，跳过该成员`);
    }
  }
  if (agentIds.length === 0) {
    console.warn(`[team-persistence] session=${opts.session_id} 无 agent 落库成功，放弃建 team`);
    return null;
  }

  const team = await pyPost<PersistedTeam>('/api/teams', {
    name: (opts.name?.trim() || deriveTeamName(opts.goal)).slice(0, 120),
    description: (opts.goal ?? '').slice(0, 500),
    agent_ids: agentIds,
    ...(opts.workspace_id ? { workspace_id: opts.workspace_id } : {}),
  });
  if (!team?.team_id) return null;
  console.log(
    `[team-persistence] session=${opts.session_id} 落库 team=${team.team_id} "${team.name}" (${agentIds.length} agents)`,
  );
  return team;
}

/**
 * tapAssemblyTeamPersistence — 包在组装 SSE 流外面的轻量 tap（Branch 2
 * orchestrate 路用，那里的 roster 是 LLM 经 <sf:node> 流式 emit 的，只能在
 * 事件流上边过边收）。收集 node 事件（按 node_id upsert），见到 complete 时
 * 先把 team+agents 落库、emit 一帧 `team-saved`（带 PersistedTeam），再放行
 * complete —— 顺序保证前端收到 complete 时团队已可查询。
 *
 * 非组装流（无 node 事件）原样透传，零副作用。
 */
export async function* tapAssemblyTeamPersistence<
  T extends { event: string; data: unknown },
>(
  stream: AsyncIterable<T>,
  ctx: { session_id: string; goal: string; workspace_id?: string },
): AsyncGenerator<T> {
  const roster = new Map<string, AssembledAgentDraft>();
  for await (const ev of stream) {
    if (ev.event === 'node') {
      const d = (ev.data ?? {}) as Record<string, unknown>;
      const nodeId = typeof d.node_id === 'string' ? d.node_id : '';
      if (nodeId) {
        const toolsPicked = Array.isArray(d.tools_picked)
          ? (d.tools_picked as unknown[]).filter((x): x is string => typeof x === 'string' && x.length > 0)
          : undefined;
        roster.set(nodeId, {
          node_id: nodeId,
          title: typeof d.title === 'string' && d.title ? d.title : nodeId,
          persona:
            (typeof d.persona === 'string' && d.persona) ||
            (typeof d.sub === 'string' && d.sub) ||
            undefined,
          model: typeof d.model === 'string' && d.model ? d.model : undefined,
          tools: toolsPicked && toolsPicked.length > 0 ? toolsPicked : undefined,
        });
      }
    } else if (ev.event === 'complete' && roster.size > 0) {
      const team = await persistAssembledTeam({
        session_id: ctx.session_id,
        goal: ctx.goal,
        workspace_id: ctx.workspace_id,
        roster: [...roster.values()],
      });
      if (team) {
        yield { event: 'team-saved', data: { ...team } } as T;
      }
    }
    yield ev;
  }
}
