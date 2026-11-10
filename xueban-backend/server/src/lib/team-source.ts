/**
 * team-source.ts — Phase 1 单一真源读取层。
 *
 * D1(plan-eng-review 已锁):team 的 edges/policy 以 Python JSON
 * (`.xueban/teams/<id>.json`)为唯一真源。本模块把该 JSON 映射成 runDag /
 * 批 2 chat 执行器共用的 run shape `{members, edges, policy_matrix}`,并提供
 * 读盘 loader。Express `.team.yaml` 仅保留 dag_layout(坐标),不再作为 edges/policy 来源。
 */
import fs from 'fs';
import path from 'path';
import type { TeamEdgeV1, EdgeKind } from './team-yaml';
import { VALID_ID_RE, ROOT_TEAMS_DIR, LOCAL_TEAMS_DIR } from './team-yaml';

export interface TeamRunShape {
  members: string[];
  edges: TeamEdgeV1[];
  policy_matrix: Record<string, Record<string, string>>;
  /** 团队展示名（Python JSON name 字段透传；缺失时调用方回退 team_id）。 */
  name?: string;
}

interface PyWorkflowNode { id?: string; data?: { agentId?: string } }
interface PyWorkflowEdge { source?: string; target?: string; data?: { mode?: string; max_retries?: number } }
interface PyTeamJson {
  team_id?: string;
  name?: string;
  agent_ids?: string[];
  workflow?: { nodes?: PyWorkflowNode[]; edges?: PyWorkflowEdge[] };
  policy_matrix?: Record<string, Record<string, string>>;
}

function mapMode(mode: string | undefined): EdgeKind {
  if (mode === 'conditional') return 'conditional';
  if (mode === 'parallel') return 'parallel';
  // 2026-11-05 — approve 不再折叠成 sequential：审批语义此前在读路径丢失
  // （chat 执行器无从知道该边要人工审批）。透传后由 runTeamDagFanout 消费。
  if (mode === 'approve') return 'approve';
  return 'sequential'; // 'direct' / 未知 → 串行
}

export function mapPythonTeamToRunShape(json: PyTeamJson): TeamRunShape {
  const members = Array.isArray(json.agent_ids) ? json.agent_ids.slice() : [];
  const nodes = json.workflow?.nodes ?? [];
  const nodeToAgent = new Map<string, string>();
  for (const n of nodes) {
    if (n.id && n.data?.agentId) nodeToAgent.set(n.id, n.data.agentId);
  }
  const edges: TeamEdgeV1[] = [];
  for (const e of json.workflow?.edges ?? []) {
    // max_retries 透传(B4 per-edge 重试预算)。注:当前 RunSession 写 workflow 时尚未写 edge.max_retries,
    // 故生产路实际多走 runTeamDagFanout 的 defaultMaxRetries;一旦组装期写入该字段,此处自动接通。
    const from = e.source ? nodeToAgent.get(e.source) : undefined;
    const to = e.target ? nodeToAgent.get(e.target) : undefined;
    if (!from || !to) continue; // 悬空边跳过
    const edge: TeamEdgeV1 = { from, to, kind: mapMode(e.data?.mode) };
    if (typeof e.data?.max_retries === 'number') edge.max_retries = e.data.max_retries;
    edges.push(edge);
  }
  return { members, edges, policy_matrix: json.policy_matrix ? { ...json.policy_matrix } : {},
    ...(typeof json.name === 'string' && json.name.trim() ? { name: json.name.trim() } : {}) };
}

// VALID_ID_RE / ROOT_TEAMS_DIR / LOCAL_TEAMS_DIR 统一从 team-yaml.ts 导入,避免双 loader 校验分叉。
// Python `<id>.json` 与 Node `<id>.team.yaml` 存放在同一目录。
const DEFAULT_DIRS = [ROOT_TEAMS_DIR, LOCAL_TEAMS_DIR];

export interface LoadTeamForRunResult {
  team: TeamRunShape | null;
  errors: string[];
}

export function loadTeamForRun(teamId: string, dirs: string[] = DEFAULT_DIRS): LoadTeamForRunResult {
  if (!VALID_ID_RE.test(teamId)) {
    return { team: null, errors: [`invalid team id: ${teamId}`] };
  }
  for (const dir of dirs) {
    const fp = path.join(dir, `${teamId}.json`);
    if (!fs.existsSync(fp)) continue;
    try {
      const json = JSON.parse(fs.readFileSync(fp, 'utf-8')) as PyTeamJson;
      return { team: mapPythonTeamToRunShape(json), errors: [] };
    } catch (e) {
      // 该 dir 文件存在但坏:立即返回(不继续找后面的 dir),让坏文件显形而非被静默跳过。
      return { team: null, errors: [`parse failed: ${(e as Error).message}`] };
    }
  }
  return { team: null, errors: [`team json not found: ${teamId}.json`] };
}
