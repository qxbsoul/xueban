/**
 * dag-layers.ts — chat DAG 执行器的纯拓扑分层(批 2 Phase 2a)。
 *
 * 用 Kahn 算法把 team 成员按「前向边」分层:同一层无依赖、可并行;层间有序。
 * - conditional 边(= 回归/路由边,如 QA→Dev)**排除**出前向拓扑(其 rework 语义留给
 *   Phase 3 治理执行),计入 ignoredEdges。
 * - 引用非成员的边丢弃(防幽灵节点/悬空边)。
 * - 若排除 conditional 后仍有环(坏 DAG),剩余节点记入 cyclicNodes 并追加为末层,
 *   保证不丢节点(Phase 3 的编译期校验会在写入时挡坏 DAG;这里运行期兜底不崩)。
 */
import type { TeamEdgeV1 } from '../team-yaml';

export interface DagLayersResult {
  /** 拓扑层;同层可并行,层间有序。 */
  layers: string[][];
  /** 排除 conditional 后仍成环、无法分层的节点(兜底进末层)。 */
  cyclicNodes: string[];
  /** 被排除出前向拓扑的 conditional 边(留给 Phase 3)。 */
  ignoredEdges: TeamEdgeV1[];
}

export function computeDagLayers(members: string[], edges: TeamEdgeV1[]): DagLayersResult {
  const memberSet = new Set(members);
  const ignoredEdges: TeamEdgeV1[] = [];
  // 前向边 = 非 conditional、且两端都是成员。parallel 边也建立执行顺序约束,与 sequential 同等参与分层。
  const forward: TeamEdgeV1[] = [];
  for (const e of edges) {
    if (e.kind === 'conditional') { ignoredEdges.push(e); continue; }
    if (!memberSet.has(e.from) || !memberSet.has(e.to)) continue; // 丢弃幽灵边
    if (e.from === e.to) continue; // 丢弃自环(坏 DAG;组装期编译校验会挡,运行期不让它卡住节点)
    forward.push(e);
  }

  const indeg = new Map<string, number>();
  for (const m of members) indeg.set(m, 0);
  const adj = new Map<string, string[]>();
  for (const e of forward) {
    indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
    if (!adj.has(e.from)) adj.set(e.from, []);
    adj.get(e.from)!.push(e.to);
  }

  const layers: string[][] = [];
  const placed = new Set<string>();
  // 每轮取当前 indeg=0 且未放置的节点(按 members 输入序保持稳定),作为一层。
  while (true) {
    const layer = members.filter((m) => !placed.has(m) && (indeg.get(m) ?? 0) === 0);
    if (layer.length === 0) break;
    for (const m of layer) {
      placed.add(m);
      for (const to of adj.get(m) ?? []) indeg.set(to, (indeg.get(to) ?? 0) - 1);
    }
    layers.push(layer);
  }

  // 剩余未放置 = 环节点。兜底:追加为末层,避免运行期丢节点。
  const cyclicNodes = members.filter((m) => !placed.has(m));
  if (cyclicNodes.length > 0) layers.push(cyclicNodes.slice());

  return { layers, cyclicNodes, ignoredEdges };
}
