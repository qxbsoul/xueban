/**
 * agent-hops.ts — agent 间任务交接记录（2026-11-05，设计移植自 CopilotKit/OpenBot
 * 的 hop / work_items 模型，适配本仓库 SQLite + chat DAG 架构）。
 *
 * 为什么要有它
 * ------------
 * 群聊 DAG 路径里 A 的产出交给 B（一条边 = 一次交接），过去只活在两个地方：
 *   1. run-team-dag.ts 的 outputs Map —— 内存，回合结束即消失；
 *   2. 群消息流 —— A/B 各自的回复可见，但「谁把什么交给了谁、上游为什么
 *      失败、下游有没有因此知情」没有记录，跨回合完全不可查。
 * 本表把每次交接落库，给功能线②补齐「异常处理 + 任务交接记录」两个评分缺口：
 *   - 异常处理：上游 failed/timeout/empty 时 hop 带 failure 记录，下游注入
 *     系统通知（失败传播），不再带着残缺上下文瞎跑；
 *   - 任务交接记录：hop 全生命周期（open→running→settled）落库可审计，
 *     回合结束后按 group 现算紧凑交接摘要（零 LLM，对齐「记忆交接」体验）。
 *
 * OpenBot 设计要点的对齐与取舍
 * ---------------------------
 *   - work_items 跟踪未完成 hop、支持排队/结算/超时 → 本表 status 全生命周期：
 *     pending → running → done | failed | timeout | empty | denied | aborted；
 *     null 终态 = 异常路径漏结算，便于审计发现。
 *   - 部分索引（只索引未完成行）→ idx_agent_hops_open ON (status) WHERE
 *     settled = 0（SQLite 支持 partial index）。
 *   - hop 按 thread 隔离 → 本地版按 group_id + turn_seq 隔离。
 *   - relay 排队/结算 → 本地 DAG 是层序驱动（computeDagLayers 已保证顺序），
 *     不需要队列；保留 settle 语义即可。
 *
 * 写入点：run-team-dag.ts 的 deps.hops 回调（groups-chat.ts 注入）；
 * 读取点：GET /api/groups/:id/hops + 交接摘要 postMessage。
 */

import { randomUUID } from 'crypto';
import { getDb } from './sqlite';

/** hop 状态机：pending(未开始) → running → 终态之一。 */
export type HopStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'timeout'
  | 'empty'
  | 'denied'
  | 'aborted';

export interface AgentHop {
  id: string;
  /** 哪个群发生的交接。 */
  group_id: string;
  /** 回合序号（同群递增），跨回合查询时可只看最新回合。 */
  turn_seq: number;
  /** 交接发起方（上游 agent）。 */
  from_agent: string;
  /** 接收方（下游 agent）。 */
  to_agent: string;
  status: HopStatus;
  /** 结算时上游产出的字符数（0 = 无产出），全文不落库（消息流里已有）。 */
  output_chars: number;
  /** 终态原因：错误信息 / denied 原因 / aborted 来源，running 前为空。 */
  detail: string;
  created_at: number;
  settled_at: number | null;
}

/** 运行期的可变 hop 句柄 —— DAG 起边时 open，拿到 id 后随节点结算。 */
export interface OpenHop {
  group_id: string;
  turn_seq: number;
  from_agent: string;
  to_agent: string;
}

export function nextTurnSeq(groupId: string): number {
  const row = getDb()
    .prepare(`SELECT COALESCE(MAX(turn_seq), 0) AS n FROM agent_hops WHERE group_id = ?`)
    .get(groupId) as { n: number };
  return row.n + 1;
}

/** 一条边开一个 hop（DAG 每条前向边在目标节点起跑时 open）。 */
export function openHop(hop: OpenHop): string {
  const id = randomUUID();
  getDb()
    .prepare(
      `INSERT INTO agent_hops (id, group_id, turn_seq, from_agent, to_agent, status, output_chars, detail, created_at, settled_at)
       VALUES (?, ?, ?, ?, ?, 'pending', 0, '', ?, NULL)`,
    )
    .run(id, hop.group_id, hop.turn_seq, hop.from_agent, hop.to_agent, Date.now());
  return id;
}

export function startHop(id: string): void {
  getDb()
    .prepare(
      `UPDATE agent_hops SET status = 'running', created_at = created_at WHERE id = ? AND status = 'pending'`,
    )
    .run(id);
}

/** 结算一个 hop —— status 必须是终态。幂等：已结算的不再覆盖。 */
export function settleHop(
  id: string,
  status: Exclude<HopStatus, 'pending' | 'running'>,
  opts: { outputChars?: number; detail?: string } = {},
): void {
  getDb()
    .prepare(
      `UPDATE agent_hops
       SET status = ?, output_chars = ?, detail = ?, settled_at = ?
       WHERE id = ? AND settled_at IS NULL`,
    )
    .run(
      status,
      Math.max(0, Math.round(opts.outputChars ?? 0)),
      (opts.detail ?? '').slice(0, 500),
      Date.now(),
      id,
    );
}

/** 某回合的全部交接记录（时间升序 = 层序近似）。 */
export function listHopsByTurn(groupId: string, turnSeq: number): AgentHop[] {
  return getDb()
    .prepare(
      `SELECT * FROM agent_hops WHERE group_id = ? AND turn_seq = ? ORDER BY created_at, id`,
    )
    .all(groupId, turnSeq) as AgentHop[];
}

/** 某群最近 N 个回合的交接记录（新回合在前）。 */
export function listRecentHops(groupId: string, turns = 1): AgentHop[] {
  const rows = getDb()
    .prepare(
      `SELECT DISTINCT turn_seq FROM agent_hops WHERE group_id = ? ORDER BY turn_seq DESC LIMIT ?`,
    )
    .all(groupId, Math.min(Math.max(turns, 1), 20)) as Array<{ turn_seq: number }>;
  if (!rows.length) return [];
  const out: AgentHop[] = [];
  for (const r of rows) out.push(...listHopsByTurn(groupId, r.turn_seq));
  return out;
}

/**
 * 交接摘要 —— 回合结束 postMessage 用的那段话（零 LLM，现算）。
 * 空群/空回合返回空串（不发废话消息）。
 */
export function summarizeHops(groupId: string, turnSeq: number): string {
  const hops = listHopsByTurn(groupId, turnSeq);
  if (!hops.length) return '';
  const lines: string[] = [];
  for (const h of hops) {
    const arrow = `${h.from_agent} → ${h.to_agent}`;
    switch (h.status) {
      case 'done':
        lines.push(`✅ ${arrow}：交接 ${h.output_chars} 字产出`);
        break;
      case 'denied':
        lines.push(`⛔ ${arrow}：被策略拦截${h.detail ? `（${h.detail}）` : ''}`);
        break;
      case 'aborted':
        lines.push(`⏹ ${arrow}：回合中止，交接未完成`);
        break;
      case 'running':
      case 'pending':
        lines.push(`⏳ ${arrow}：交接未结算（异常路径，值得排查）`);
        break;
      default:
        lines.push(`❌ ${arrow}：${hopStatusLabel(h.status)}${h.detail ? ` — ${h.detail}` : ''}`);
    }
  }
  return `[交接记录] 本回合 ${hops.length} 次交接：\n${lines.join('\n')}`;
}

export function hopStatusLabel(s: HopStatus): string {
  switch (s) {
    case 'done': return '完成';
    case 'failed': return '失败';
    case 'timeout': return '超时';
    case 'empty': return '空回复';
    case 'denied': return '被拦截';
    case 'aborted': return '中止';
    case 'running': return '进行中';
    case 'pending': return '待开始';
  }
}

/**
 * 启动清扫 — 把上一进程遗留的孤儿 hop（pending/running）补结算为 aborted
 * （2026-11-05 迭代三，B2 中断恢复收尾）。
 *
 * 为什么安全：本服务器单进程，启动时刻不存在任何活 DAG 回合——所有未结算
 * hop 都是上一进程崩溃残留（正常路径由 run-team-dag 收尾兜底结算，见
 * run-team-dag.ts「不留悬空」）。与 run-event-bus 的启动 hydration（中断
 * run 补 canceled 帧）配套，sqlite 侧审计链同样不悬空。
 *
 * 返回清扫行数。幂等：已结算行（settled_at 非空）永不再动。
 */
export function sweepOrphanHops(opts: { detail?: string } = {}): number {
  const res = getDb()
    .prepare(
      `UPDATE agent_hops
       SET status = 'aborted', detail = ?, settled_at = ?
       WHERE settled_at IS NULL`,
    )
    .run(
      (opts.detail ?? '进程重启中断，启动清扫补结算').slice(0, 500),
      Date.now(),
    );
  return res.changes;
}

/** 测试隔离用。 */
export function _resetHopsForTests(): void {
  getDb().prepare(`DELETE FROM agent_hops`).run();
}
