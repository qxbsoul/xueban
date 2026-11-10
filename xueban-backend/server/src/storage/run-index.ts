/**
 * run-index.ts — 后端合一·阶段0（2026-06-12）：持久 run 索引。
 *
 * 修实锤缺口：conversation→session 的映射原本只活在内存 sessionStore（JSON,
 * 活动制 TTL）——gateway session 被回收后，群 timeline 的历史过程卡随之消失
 * （事件日志 .xueban/runs/*.json 落盘还在，但没人记得它属于哪条会话）。
 * 本表把映射落进 SQLite，会话历史从此不依赖内存生命周期。
 *
 * 与 storage/runs.ts 的 `runs` 表无关——那是「完成 run 的工件存档」（run_id
 * 主键 + MAX_RUNS=100 截断）；本表是「session↔conversation 索引」（session_id
 * 主键、**永不截断**——索引消失=历史消失，正是要修的 bug）。
 *
 * 写入点：POST /api/run-sessions、registerGatewayRun；状态终态：runBus.finish
 * 两个调用面；读取点：listSessionsByConversation（B2 timeline 聚合）。
 */
import { getDb } from './sqlite';

export interface RunIndexRecord {
  session_id: string;
  conversation_id: string | null;
  goal: string;
  /** 终态：succeeded/failed/canceled；null=运行中或未知（活状态问 runBus）。 */
  status: string | null;
  created_at: number;
  updated_at: number;
}

export function upsertRunIndex(rec: {
  session_id: string;
  conversation_id?: string | null;
  goal?: string;
  status?: string | null;
  created_at?: number;
}): void {
  const now = Date.now();
  getDb()
    .prepare(
      `INSERT INTO run_index (session_id, conversation_id, goal, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(session_id) DO UPDATE SET
         conversation_id = COALESCE(excluded.conversation_id, run_index.conversation_id),
         goal = CASE WHEN excluded.goal != '' THEN excluded.goal ELSE run_index.goal END,
         status = COALESCE(excluded.status, run_index.status),
         updated_at = excluded.updated_at`,
    )
    .run(
      rec.session_id,
      rec.conversation_id ?? null,
      rec.goal ?? '',
      rec.status ?? null,
      rec.created_at ?? now,
      now,
    );
}

export function setRunIndexStatus(session_id: string, status: string): void {
  getDb()
    .prepare(`UPDATE run_index SET status = ?, updated_at = ? WHERE session_id = ?`)
    .run(status, Date.now(), session_id);
}

export function listRunIndexByConversation(conversation_id: string): RunIndexRecord[] {
  return getDb()
    .prepare(`SELECT * FROM run_index WHERE conversation_id = ? ORDER BY updated_at DESC`)
    .all(conversation_id) as RunIndexRecord[];
}

/**
 * 启动清扫 — 把上一进程遗留的 NULL 状态 run_index 行补成 canceled
 * （2026-11-05 迭代三，B2 中断恢复配套）。
 *
 * NULL = 「运行中或未知」（写入点 upsertRunIndex 不带 status；正常终态由
 * runBus.finish → setRunIndexStatus 回填）。单进程服务器启动时刻不存在任何
 * 活 run，NULL 行必是崩溃残留。run-event-bus 的 hydration 已把事件日志侧补
 * canceled 帧；本函数让 sqlite 索引侧状态一致。返回清扫行数。
 */
export function sweepOrphanRunIndex(): number {
  const res = getDb()
    .prepare(`UPDATE run_index SET status = 'canceled', updated_at = ? WHERE status IS NULL`)
    .run(Date.now());
  return res.changes;
}
