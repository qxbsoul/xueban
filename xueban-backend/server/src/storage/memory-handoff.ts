/**
 * memory-handoff.ts — 功能线③「记忆与交接」的存储层（2026-11-05）。
 *
 * 定位：这是挂在现有 Node 后端上的一个外接模块，不是新服务。它只做三件事：
 *   1. agent 间记忆交接（A 下班 → B 接班能读上下文）；
 *   2. 长期画像写回（当前值 + append-only 变更流水，零 LLM）；
 *   3. 记忆消费留痕（contextFor 每次被调用落一条访问记录，证明记忆被用上了）。
 *
 * 与既有存储层的边界（有意为之，避免动主链路）：
 *   - 快照从已有事实里挖：recallLessons() 的分域经验 + run_index 近 7 天失败目标
 *     + 画像当前值。不调模型、不复制原文。
 *   - 注入的上下文副本写进自己的 memory_handoff_items，绝不写回 memory_lessons，
 *     因此不会挤占每域 8 条上限、也不会污染统一「记忆」列表。
 *   - 分域语义沿用 memory-lessons.ts 的 _in_scope：空=通用谁都能读；
 *     库里有域则必须「同 scope_key + 同 task_type」才召回；跨域一条专属都不给。
 *
 * 长期画像写回规则：高置信覆盖低置信；同置信不同说法记 conflict 但保留原值
 * （不猜谁对，留痕等人工裁决）；低置信记 keep。每次写都追加一条事件，可回溯。
 */

import { createHash, randomUUID } from 'crypto';
import { getDb } from './sqlite';
import { recallLessons } from './memory-lessons';

const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_TEXT = 600;
/** 每个画像主体保留的流水条数上限；超出滚动删除最老的（当前值表不受影响）。 */
const PROFILE_EVENT_KEEP = 200;
/** 记忆消费留痕保留的最近条数上限；是访问日志性质，超出滚动删除最老的。 */
const CONTEXT_READ_KEEP = 200;

export class MemoryHandoffError extends Error {
  status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.status = status;
  }
}

export interface HandoffContext {
  generated_at: number;
  shift: {
    shift_id: string;
    from_agent: string;
    to_agent: string;
    scope_key: string;
    task_type: string;
  };
  lessons: Array<{
    id: string;
    kind: string;
    scope_key: string;
    task_type: string;
    content: string;
    score: number;
    source: string;
  }>;
  open_items: Array<{ goal: string; n: number }>;
  profiles: Array<{ subject_id: string; key: string; value: string; confidence: number }>;
}

export interface Handoff {
  id: string;
  shift_id: string;
  from_agent: string;
  to_agent: string;
  scope_key: string;
  task_type: string;
  summary: string;
  status: 'pending' | 'accepted';
  created_at: number;
  accepted_at: number | null;
  context_items: number;
  context?: HandoffContext;
}

interface HandoffRow {
  id: string;
  shift_id: string;
  from_agent: string;
  to_agent: string;
  scope_key: string;
  task_type: string;
  summary: string;
  status: string;
  context_json: string;
  created_at: number;
  accepted_at: number | null;
}

function hydrate(row: HandoffRow, withContext = false): Handoff {
  let context: HandoffContext | undefined;
  try {
    context = JSON.parse(row.context_json) as HandoffContext;
  } catch {
    context = undefined;
  }
  return {
    id: row.id,
    shift_id: row.shift_id,
    from_agent: row.from_agent,
    to_agent: row.to_agent,
    scope_key: row.scope_key,
    task_type: row.task_type,
    summary: row.summary,
    status: row.status === 'accepted' ? 'accepted' : 'pending',
    created_at: row.created_at,
    accepted_at: row.accepted_at,
    context_items: context?.lessons?.length ?? 0,
    ...(withContext ? { context } : {}),
  };
}

// ---------------------------------------------------------------- 交接包

/** 从库内已有事实自动汇总快照——不手抄、不调 LLM。 */
function buildContext(shift: HandoffContext['shift']): HandoffContext {
  const lessons = recallLessons(shift.scope_key, shift.task_type, 20).map((l) => ({
    id: l.id,
    kind: l.kind,
    scope_key: l.scope_key,
    task_type: l.task_type,
    content: l.content,
    score: l.score,
    source: l.source,
  }));
  const openItems = getDb()
    .prepare(
      `SELECT COALESCE(goal, '') AS goal, COUNT(*) AS n FROM run_index
       WHERE status = 'failed' AND created_at >= ?
       GROUP BY goal ORDER BY n DESC LIMIT 5`,
    )
    .all(Date.now() - 7 * DAY_MS) as Array<{ goal: string; n: number }>;
  const profiles = getDb()
    .prepare(
      `SELECT subject_id, key, value, confidence FROM memory_profile_values
       ORDER BY updated_at DESC LIMIT 5`,
    )
    .all() as HandoffContext['profiles'];
  return { generated_at: Date.now(), shift, lessons, open_items: openItems, profiles };
}

export function createHandoff(input: {
  shift_id: string;
  from_agent: string;
  to_agent: string;
  scope_key?: string;
  task_type?: string;
  summary?: string;
}): { handoff: Handoff; idempotent: boolean } {
  const shift_id = (input.shift_id ?? '').trim();
  const from_agent = (input.from_agent ?? '').trim();
  const to_agent = (input.to_agent ?? '').trim();
  if (!shift_id || !from_agent || !to_agent) {
    throw new MemoryHandoffError('shift_id / from_agent / to_agent 不能为空');
  }
  if (from_agent === to_agent) {
    throw new MemoryHandoffError('交接双方不能是同一个 agent');
  }
  const existing = getDb()
    .prepare(
      `SELECT * FROM memory_handoffs WHERE shift_id = ? AND from_agent = ? AND to_agent = ?`,
    )
    .get(shift_id, from_agent, to_agent) as HandoffRow | undefined;
  if (existing) return { handoff: hydrate(existing, true), idempotent: true };

  const shift = {
    shift_id,
    from_agent,
    to_agent,
    scope_key: (input.scope_key ?? '').trim(),
    task_type: (input.task_type ?? '').trim(),
  };
  const context = buildContext(shift);
  const id = randomUUID();
  const now = Date.now();
  getDb()
    .prepare(
      `INSERT INTO memory_handoffs
       (id, shift_id, from_agent, to_agent, scope_key, task_type, summary,
        status, context_json, created_at, accepted_at)
       VALUES (?,?,?,?,?,?,?,'pending',?,?,NULL)`,
    )
    .run(
      id, shift_id, from_agent, to_agent, shift.scope_key, shift.task_type,
      (input.summary ?? '').slice(0, MAX_TEXT), JSON.stringify(context), now,
    );
  return { handoff: getHandoff(id)!, idempotent: false };
}

/** 按 shift_id 精确取一条交接包（群回合 sink 幂等查重用）。 */
export function findHandoffByShift(shift_id: string): Handoff | undefined {
  const row = getDb()
    .prepare(`SELECT * FROM memory_handoffs WHERE shift_id = ? LIMIT 1`)
    .get((shift_id ?? '').trim()) as HandoffRow | undefined;
  return row ? hydrate(row, true) : undefined;
}

/**
 * 取 shift_id 形如「<prefix><正整数>」的最大数字（无匹配 → 0）。
 * 群回合交接锚预留用：无 hop 落库的回合（无边团队等）turn 号不被 agent_hops
 * 记账，靠已用交接锚防撞号——撞号会让后一轮交接包被幂等查重误跳过。
 * 只认纯数字尾（负时间戳锚如 chat:g:-17… 视同无锚）；查询失败软化为 0。
 */
export function maxShiftTurnAnchor(prefix: string): number {
  if (!prefix) return 0;
  try {
    const rows = getDb()
      .prepare(`SELECT shift_id FROM memory_handoffs WHERE shift_id LIKE ?`)
      .all(`${prefix}%`) as Array<{ shift_id: string }>;
    let max = 0;
    for (const r of rows) {
      // LIKE 把 prefix 里的 %/_ 当通配符，粗筛后再精确前缀 + 纯数字尾过滤。
      if (!r.shift_id.startsWith(prefix)) continue;
      const m = /^(\d+)$/.exec(r.shift_id.slice(prefix.length));
      if (m) max = Math.max(max, Number(m[1]));
    }
    return max;
  } catch {
    return 0;
  }
}

export function getHandoff(id: string): Handoff | undefined {
  const row = getDb().prepare(`SELECT * FROM memory_handoffs WHERE id = ?`).get(id) as
    | HandoffRow
    | undefined;
  return row ? hydrate(row, true) : undefined;
}

export function listHandoffs(opts: {
  agent?: string;
  status?: string;
  limit?: number;
} = {}): Handoff[] {
  let sql = `SELECT * FROM memory_handoffs WHERE 1 = 1`;
  const args: unknown[] = [];
  if (opts.agent) {
    sql += ` AND (from_agent = ? OR to_agent = ?)`;
    args.push(opts.agent, opts.agent);
  }
  if (opts.status === 'pending' || opts.status === 'accepted') {
    sql += ` AND status = ?`;
    args.push(opts.status);
  }
  sql += ` ORDER BY created_at DESC LIMIT ?`;
  args.push(Math.min(Math.max(Math.trunc(opts.limit ?? 30), 1), 200));
  const rows = getDb().prepare(sql).all(...args) as HandoffRow[];
  return rows.map((r) => hydrate(r));
}

/** B 确认接班：把快照注入成 B 名下的只读副本（A 的原件不动）。幂等。 */
export function acceptHandoff(
  id: string,
  to_agent: string,
): { handoff: Handoff; injected: number; idempotent: boolean } {
  const db = getDb();
  const run = db.transaction(() => {
    const row = db.prepare(`SELECT * FROM memory_handoffs WHERE id = ?`).get(id) as
      | HandoffRow
      | undefined;
    if (!row) throw new MemoryHandoffError('交接包不存在', 404);
    if (row.to_agent !== to_agent) {
      throw new MemoryHandoffError(`该交接包指定交给 ${row.to_agent}`, 403);
    }
    if (row.status === 'accepted') return { injected: 0, idempotent: true };
    if (row.status !== 'pending') {
      throw new MemoryHandoffError(`交接包状态为 ${row.status}`, 409);
    }
    let context: HandoffContext;
    try {
      context = JSON.parse(row.context_json) as HandoffContext;
    } catch {
      throw new MemoryHandoffError('交接包快照损坏', 409);
    }
    const now = Date.now();
    const insert = db.prepare(
      `INSERT INTO memory_handoff_items
       (id, handoff_id, kind, scope_key, task_type, content, score, created_at)
       VALUES (?,?,?,?,?,?,?,?)`,
    );
    for (const lesson of context.lessons ?? []) {
      insert.run(
        randomUUID(), id, lesson.kind, lesson.scope_key, lesson.task_type,
        lesson.content, lesson.score, now,
      );
    }
    db.prepare(`UPDATE memory_handoffs SET status = 'accepted', accepted_at = ? WHERE id = ?`).run(now, id);
    return { injected: (context.lessons ?? []).length, idempotent: false };
  });
  const result = run();
  return { handoff: getHandoff(id)!, ...result };
}

/** 交接包级作用域：两维都空 = 通用；否则必须同域。 */
function handoffInScope(row: HandoffRow | Handoff, scope_key: string, task_type: string): boolean {
  if (!row.scope_key && !row.task_type) return true;
  return Boolean(scope_key && task_type && row.scope_key === scope_key && row.task_type === task_type);
}

/** 上下文条目级作用域，语义与 memory-lessons.ts 的 _in_scope 一致。 */
function itemInScope(
  row: { scope_key: string; task_type: string },
  scope_key: string,
  task_type: string,
): boolean {
  if (row.scope_key && (!scope_key || row.scope_key !== scope_key)) return false;
  if (row.task_type && (!task_type || row.task_type !== task_type)) return false;
  return true;
}

// ---------------------------------------------------------------- 记忆消费留痕
//
// ③ 的闭环不只有「写回 / 交接」供给侧，还得证明「交接出来的记忆被用上了」：
// contextFor() 是记忆的消费入口，每次调用落一条轻量访问留痕（谁 / 什么作用域 /
// 带出几条 / 来源集合），证据材料据此展示「记忆不是摆设」。
// 消费留痕是访问日志性质的写入：写不进去绝不影响读取本身（contextFor 里 try/catch）。
// 只有落痕这一条写路径会建表（幂等 DDL），contextReads() 等查询纯只读、不建表。

const CONTEXT_READS_DDL = `
CREATE TABLE IF NOT EXISTS memory_context_reads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  scope_key TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL DEFAULT '',
  item_count INTEGER NOT NULL DEFAULT 0,
  sources_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_context_reads_at
  ON memory_context_reads(created_at);`;

/** _resetForTests() 会换 db 实例，所以认实例而不是认模块级布尔（与 feedbackDb 同法）。 */
let contextReadsDdOwner: unknown = null;
function contextReadsDb(): Conn {
  const db = getDb();
  if (contextReadsDdOwner !== db) {
    db.exec(CONTEXT_READS_DDL);
    contextReadsDdOwner = db;
  }
  return db;
}

export interface ContextItem {
  type: 'memory' | 'handoff' | 'lesson';
  source: string;
  content: string;
  kind?: string;
  from_agent?: string;
  shift_id?: string;
  score?: number;
}

/** B 当前能读到的上下文：已接收交接副本 + 交接包摘要 + 通用经验，逐条标来源。 */
export function contextFor(
  agent: string,
  scope_key = '',
  task_type = '',
  limit = 20,
): { agent: string; scope: { scope_key: string; task_type: string }; items: ContextItem[]; count: number; sources: string[] } {
  const db = getDb();
  const capped = Math.min(Math.max(Math.trunc(limit) || 20, 1), 100);
  const accepted = db
    .prepare(
      `SELECT * FROM memory_handoffs WHERE to_agent = ? AND status = 'accepted'
       ORDER BY accepted_at DESC LIMIT 10`,
    )
    .all(agent) as HandoffRow[];
  const items: ContextItem[] = [];
  for (const h of accepted) {
    if (!handoffInScope(h, scope_key, task_type)) continue;
    const rows = db
      .prepare(`SELECT * FROM memory_handoff_items WHERE handoff_id = ?`)
      .all(h.id) as Array<{
      kind: string;
      scope_key: string;
      task_type: string;
      content: string;
      score: number;
    }>;
    for (const r of rows) {
      if (!itemInScope(r, scope_key, task_type)) continue;
      items.push({
        type: 'memory',
        source: `handoff:${h.id}`,
        content: r.content,
        kind: r.kind,
        from_agent: h.from_agent,
        shift_id: h.shift_id,
        score: r.score,
      });
    }
    items.push({
      type: 'handoff',
      source: `handoff:${h.id}`,
      content: h.summary || `来自 ${h.from_agent} 的交接（${rows.length} 条上下文）`,
      from_agent: h.from_agent,
      shift_id: h.shift_id,
    });
  }
  // 通用经验（两维皆空）不受交接影响，任何 agent 都能带，标 lesson 来源。
  const common = db
    .prepare(
      `SELECT kind, content, score FROM memory_lessons
       WHERE archived = 0 AND scope_key = '' AND task_type = ''
       ORDER BY score DESC, updated_at DESC LIMIT 5`,
    )
    .all() as Array<{ kind: string; content: string; score: number }>;
  for (const r of common) {
    items.push({ type: 'lesson', source: 'lesson:common', content: r.content, kind: r.kind, score: r.score });
  }
  const sliced = items.slice(0, capped);
  const sources = Array.from(new Set(sliced.map((i) => i.source))).sort();
  // 消费留痕：访问日志性质，失败绝不影响「读上下文」本身。
  try {
    const db = contextReadsDb();
    db.prepare(
      `INSERT INTO memory_context_reads (agent, scope_key, task_type, item_count, sources_json, created_at)
       VALUES (?,?,?,?,?,?)`,
    ).run(agent, scope_key, task_type, sliced.length, JSON.stringify(sources), Date.now());
    db.prepare(
      `DELETE FROM memory_context_reads WHERE id NOT IN
         (SELECT id FROM memory_context_reads ORDER BY id DESC LIMIT ?)`,
    ).run(CONTEXT_READ_KEEP);
  } catch {
    // 留痕失败静默跳过：读取不能因为访问日志写不进去而失败。
  }
  return {
    agent,
    scope: { scope_key, task_type },
    items: sliced,
    count: sliced.length,
    sources,
  };
}

export interface ContextReadItem {
  id: number;
  agent: string;
  scope_key: string;
  task_type: string;
  /** 这次读取带出的上下文条数（0 = 该作用域没有可用记忆，也如实记 0） */
  item_count: number;
  sources: string[];
  created_at: number;
}

/**
 * 消费留痕审计（纯只读，不建表）：谁在什么时候读走了哪些来源的记忆。
 * 表不存在（还没人读过记忆）时返回全 0 空列表，与「计数为 0 明写待补」口径一致。
 */
export function contextReads(opts: { limit?: number; from?: number | null; to?: number | null } = {}): {
  total: number;
  by_agent: Record<string, number>;
  items: ContextReadItem[];
} {
  const empty = { total: 0, by_agent: {} as Record<string, number>, items: [] as ContextReadItem[] };
  const db = getDb();
  const exists = !!db
    .prepare(`SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_context_reads'`)
    .get();
  if (!exists) return empty;
  const params: unknown[] = [];
  let where = 'WHERE 1 = 1';
  if (Number.isFinite(opts.from)) {
    where += ' AND created_at >= ?';
    params.push(opts.from);
  }
  if (Number.isFinite(opts.to)) {
    where += ' AND created_at < ?';
    params.push(opts.to);
  }
  const total = (db.prepare(`SELECT COUNT(*) AS n FROM memory_context_reads ${where}`).get(...params) as { n: number }).n;
  const by_agent: Record<string, number> = {};
  const groups = db
    .prepare(`SELECT agent, COUNT(*) AS n FROM memory_context_reads ${where} GROUP BY agent`)
    .all(...params) as Array<{ agent: string; n: number }>;
  for (const g of groups) by_agent[g.agent] = g.n;
  const rows = db
    .prepare(
      `SELECT id, agent, scope_key, task_type, item_count, sources_json, created_at
         FROM memory_context_reads ${where} ORDER BY id DESC LIMIT ?`,
    )
    .all(...params, capNum(opts.limit, 20, 200)) as Array<{
    id: number;
    agent: string;
    scope_key: string;
    task_type: string;
    item_count: number;
    sources_json: string;
    created_at: number;
  }>;
  // 明细按时间正序展示（与画像流水同口径），窗口总数不受 limit 影响。
  const items = rows.reverse().map(({ sources_json, ...rest }) => ({ ...rest, sources: jsonArray(sources_json) }));
  return { total, by_agent, items };
}

// ---------------------------------------------------------------- 长期画像写回

export type WritebackAction = 'create' | 'update' | 'conflict' | 'keep' | 'override' | 'dismiss';

/** 写路径共用：每个主体只留最近 PROFILE_EVENT_KEEP 条事件（当前值表不受影响）。 */
function trimProfileEvents(db: ReturnType<typeof getDb>, subjectType: string, subjectId: string): void {
  db.prepare(
    `DELETE FROM memory_profile_events
      WHERE subject_type = ? AND subject_id = ?
        AND id NOT IN (
          SELECT id FROM memory_profile_events
           WHERE subject_type = ? AND subject_id = ?
           ORDER BY id DESC LIMIT ?
        )`,
  ).run(subjectType, subjectId, subjectType, subjectId, PROFILE_EVENT_KEEP);
}

export interface WritebackResult {
  subject_type: string;
  subject_id: string;
  key: string;
  action: WritebackAction;
  changed: boolean;
  value: string;
  confidence: number;
}

export function writeback(input: {
  subject_type?: string;
  subject_id: string;
  key: string;
  value: string;
  confidence?: number;
  source?: string;
  evidence?: string;
  actor?: string;
  /**
   * 人工裁决：绕过「置信度高者胜」的自动合并，直接覆盖。
   *
   * 为什么必须有这个开关：同一置信度、不同说法的两次写回会落成 action='conflict'
   * 并且**不覆盖**——设计上是对的（不敢替人拍板），但此前没有任何出口能拍板，
   * 客户画像会永远停在第一次的说法上。现在由人或上游系统显式 force，动作记成
   * 'override' 并保留 old_value 与新证据，审计链不断。
   */
  force?: boolean;
}): WritebackResult {
  const subject_type = (input.subject_type || 'customer').trim();
  const subject_id = (input.subject_id ?? '').trim();
  const key = (input.key ?? '').trim();
  const value = (input.value ?? '').trim();
  if (!subject_id || !key || !value) {
    throw new MemoryHandoffError('subject_id / key / value 不能为空');
  }
  const confidence = typeof input.confidence === 'number' ? input.confidence : 0.7;
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new MemoryHandoffError('confidence 必须在 0~1 之间');
  }
  const source = (input.source || 'manual').slice(0, 64);
  const evidence = (input.evidence ?? '').slice(0, MAX_TEXT);
  const actor = (input.actor || 'system').slice(0, 64);
  const force = input.force === true;
  const db = getDb();
  const run = db.transaction(() => {
    const cur = db
      .prepare(
        `SELECT * FROM memory_profile_values
         WHERE subject_type = ? AND subject_id = ? AND key = ?`,
      )
      .get(subject_type, subject_id, key) as
      | { value: string; confidence: number }
      | undefined;
    const now = Date.now();
    let action: WritebackAction;
    if (!cur) {
      action = 'create';
      db.prepare(
        `INSERT INTO memory_profile_values
         (subject_type, subject_id, key, value, confidence, source, evidence, updated_at)
         VALUES (?,?,?,?,?,?,?,?)`,
      ).run(subject_type, subject_id, key, value, confidence, source, evidence, now);
    } else if (force) {
      // 值没变就没事可做，别把「确认一次」记成一次覆盖
      action = value === cur.value ? 'keep' : 'override';
      if (action === 'override') {
        db.prepare(
          `UPDATE memory_profile_values
           SET value = ?, confidence = ?, source = ?, evidence = ?, updated_at = ?
           WHERE subject_type = ? AND subject_id = ? AND key = ?`,
        ).run(value, Math.max(confidence, cur.confidence), source, evidence, now, subject_type, subject_id, key);
      }
    } else if (confidence > cur.confidence) {
      action = 'update';
      db.prepare(
        `UPDATE memory_profile_values
         SET value = ?, confidence = ?, source = ?, evidence = ?, updated_at = ?
         WHERE subject_type = ? AND subject_id = ? AND key = ?`,
      ).run(value, confidence, source, evidence, now, subject_type, subject_id, key);
    } else if (confidence === cur.confidence && value !== cur.value) {
      action = 'conflict'; // 同置信不同说法：不覆盖，只留痕（可用 force 裁决）
    } else {
      action = 'keep';
    }
    db.prepare(
      `INSERT INTO memory_profile_events
       (subject_type, subject_id, key, old_value, new_value, old_confidence,
        confidence, action, source, evidence, actor, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
    ).run(
      subject_type, subject_id, key,
      cur ? cur.value : null, value,
      cur ? cur.confidence : null,
      confidence, action, source, evidence, actor, now,
    );
    // 流水只留最近 PROFILE_EVENT_KEEP 条：append-only 不等于无限增长，
    // 长期跑下来（每个客户几百次写回）单表会拖慢 profile() 的历史查询。
    // 当前值在 memory_profile_values 里，裁剪历史不影响画像本身。
    trimProfileEvents(db, subject_type, subject_id);
    return {
      action,
      value: action === 'create' || action === 'update' || action === 'override' ? value : cur!.value,
      confidence:
        action === 'create' || action === 'update'
          ? confidence
          : action === 'override'
            ? Math.max(confidence, cur!.confidence)
            : cur!.confidence,
    };
  });
  const r = run();
  return {
    subject_type,
    subject_id,
    key,
    action: r.action,
    changed: r.action === 'create' || r.action === 'update' || r.action === 'override',
    value: r.value,
    confidence: r.confidence,
  };
}

/**
 * 待裁决的画像冲突。
 *
 * 「同置信、不同说法」的写回不覆盖（不敢替人拍板），但以前只写进 events 表，
 * 没有任何出口能把它捞出来 —— 冲突等于烂在流水里。
 *
 * 判定铁律：该 (主体, key) 的**最近一次写回事件**必须就是这条 conflict。
 * 以前只在 conflict 事件里取 MAX(id)，于是「冲突之后又被更高置信的写回更新了值」
 * 或「已经 force 裁决过」的旧冲突还会挂在列表里，人工反复处理同一条。
 * 改成看全体事件的 MAX(id) 后，任何后续写回（update / override / dismiss / keep）
 * 都会让旧提议自然失效 —— 裁决动作因此不需要额外的状态列。
 */
export interface ProfileConflict {
  subject_type: string;
  subject_id: string;
  key: string;
  current_value: string;
  current_confidence: number;
  proposed_value: string;
  proposed_confidence: number;
  source: string;
  evidence: string;
  created_at: number;
}

const CONFLICT_FIELDS = `e.subject_type, e.subject_id, e.key,
       v.value      AS current_value,
       v.confidence AS current_confidence,
       e.new_value  AS proposed_value,
       e.confidence AS proposed_confidence,
       e.source, e.evidence, e.created_at`;
const CONFLICT_BASE = `FROM memory_profile_events e
      JOIN memory_profile_values v
        ON v.subject_type = e.subject_type
       AND v.subject_id   = e.subject_id
       AND v.key          = e.key
     WHERE e.action = 'conflict'
       AND v.value <> e.new_value`;
const CONFLICT_LATEST = `AND e.id = (
         SELECT MAX(x.id) FROM memory_profile_events x
          WHERE x.subject_type = e.subject_type
            AND x.subject_id   = e.subject_id
            AND x.key          = e.key
       )`;

type Conn = ReturnType<typeof getDb>;

/** extra 里的 ? 参数放在 params，cap 永远追加在最后。列表与单条查询同源。 */
function conflictRows(db: Conn, extra: string, params: unknown[], cap: number): ProfileConflict[] {
  return db
    .prepare(
      `SELECT ${CONFLICT_FIELDS} ${CONFLICT_BASE} ${extra} ${CONFLICT_LATEST}
        ORDER BY e.created_at DESC LIMIT ?`,
    )
    .all(...params, cap) as ProfileConflict[];
}

/** 待裁决条数（不受列表 500 上限影响，stats / profile 用它）。 */
function pendingConflictCount(db: Conn, extra = '', params: unknown[] = []): number {
  return (
    db.prepare(`SELECT COUNT(*) AS n ${CONFLICT_BASE} ${extra} ${CONFLICT_LATEST}`).get(...params) as {
      n: number;
    }
  ).n;
}

export function listConflicts(input: { limit?: number; subject_id?: string } = {}): ProfileConflict[] {
  const raw = Number(input.limit ?? 50);
  const cap = Number.isFinite(raw) ? Math.min(Math.max(Math.trunc(raw), 1), 500) : 50;
  const subject = (input.subject_id ?? '').trim();
  return subject
    ? conflictRows(getDb(), 'AND e.subject_id = ?', [subject], cap)
    : conflictRows(getDb(), '', [], cap);
}

export type ConflictDecision = 'accept' | 'dismiss';

export interface ConflictResolution {
  subject_type: string;
  subject_id: string;
  key: string;
  decision: ConflictDecision;
  action: WritebackAction;
  /** 裁决后的当前画像值 */
  value: string;
  actor: string;
  resolved_at: number;
}

/**
 * 人工裁决一条待裁决冲突：采纳提议 / 驳回提议（保留当前值）。
 *
 * 为什么不是直接让前端拼 force 写回：调用方得先自己查出提议值、置信度、证据，
 * 拼错了就会把「驳回」写成一次覆盖；而且裁决本身需要留痕（谁、什么时候、驳回了什么）。
 * 这里在一个事务里完成「查待裁决 → 落裁决 → 清列表」，重复点按第二次直接 404。
 */
export function resolveConflict(input: {
  subject_type?: string;
  subject_id: string;
  key: string;
  decision: string;
  actor?: string;
}): ConflictResolution {
  const subject_type = (input.subject_type || 'customer').trim();
  const subject_id = (input.subject_id ?? '').trim();
  const key = (input.key ?? '').trim();
  if (!subject_id || !key) throw new MemoryHandoffError('subject_id / key 不能为空');
  const rawDecision = input.decision;
  if (rawDecision !== 'accept' && rawDecision !== 'dismiss') {
    throw new MemoryHandoffError('decision must be accept|dismiss');
  }
  // 显式收窄成字面量联合：下面在事务闭包里返回，TS 不会把 if 的窄化带进闭包。
  const decision: ConflictDecision = rawDecision;
  const actor = (input.actor || 'system').slice(0, 64);
  const db = getDb();

  return db.transaction(() => {
    const conflict = conflictRows(
      db,
      'AND e.subject_type = ? AND e.subject_id = ? AND e.key = ?',
      [subject_type, subject_id, key],
      1,
    )[0];
    if (!conflict) throw new MemoryHandoffError('该画像没有待裁决的冲突', 404);

    if (decision === 'accept') {
      const result = writeback({
        subject_type,
        subject_id,
        key,
        value: conflict.proposed_value,
        confidence: conflict.proposed_confidence,
        source: 'conflict_resolution',
        evidence: `采纳冲突提议：${conflict.evidence}`.slice(0, MAX_TEXT),
        actor,
        force: true,
      });
      return {
        subject_type, subject_id, key, decision,
        action: result.action,
        value: result.value,
        actor,
        resolved_at: Date.now(),
      };
    }

    const now = Date.now();
    db.prepare(
      `INSERT INTO memory_profile_events
       (subject_type, subject_id, key, old_value, new_value, old_confidence,
        confidence, action, source, evidence, actor, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
    ).run(
      subject_type, subject_id, key,
      conflict.current_value, conflict.current_value,
      conflict.current_confidence, conflict.current_confidence,
      'dismiss', 'conflict_resolution',
      `驳回提议「${conflict.proposed_value}」`.slice(0, MAX_TEXT),
      actor, now,
    );
    trimProfileEvents(db, subject_type, subject_id);
    return {
      subject_type, subject_id, key, decision,
      action: 'dismiss' as WritebackAction,
      value: conflict.current_value,
      actor,
      resolved_at: now,
    };
  })();
}

export function profile(subjectId: string, subjectType = 'customer'): {
  subject: { type: string; id: string };
  profile: Array<{ key: string; value: string; confidence: number; source: string; evidence: string; updated_at: number }>;
  history: Array<{ key: string; old_value: string | null; new_value: string; action: string; confidence: number; source: string; actor: string; created_at: number }>;
  writes: number;
  /** 历史累计冲突次数（含已裁决的），用于回溯 */
  conflicts: number;
  /** 当前还挂在待裁决列表里的条数 —— 与上面的历史累计不是一回事 */
  pending_conflicts: number;
} {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT key, value, confidence, source, evidence, updated_at
       FROM memory_profile_values WHERE subject_type = ? AND subject_id = ? ORDER BY key`,
    )
    .all(subjectType, subjectId) as Array<{
    key: string; value: string; confidence: number; source: string; evidence: string; updated_at: number;
  }>;
  const history = db
    .prepare(
      `SELECT key, old_value, new_value, action, confidence, source, actor, created_at
       FROM memory_profile_events WHERE subject_type = ? AND subject_id = ? ORDER BY id`,
    )
    .all(subjectType, subjectId) as Array<{
    key: string; old_value: string | null; new_value: string; action: string;
    confidence: number; source: string; actor: string; created_at: number;
  }>;
  return {
    subject: { type: subjectType, id: subjectId },
    profile: rows,
    history,
    writes: history.length,
    conflicts: history.filter((h) => h.action === 'conflict').length,
    pending_conflicts: pendingConflictCount(
      db,
      'AND e.subject_type = ? AND e.subject_id = ?',
      [subjectType, subjectId],
    ),
  };
}

/** 规则词表：客户反馈进来自动提炼写回长期画像，零模型、零额度消耗。 */
export const FEEDBACK_RULES: ReadonlyArray<readonly [string, readonly string[]]> = [
  ['preference', ['偏好', '喜欢', '更倾向', '不要', '别用', '排斥', '简洁']],
  ['budget', ['预算', '价位', '报价', '多少钱', '价格接受']],
  ['industry', ['行业', '主营', '做的是', '赛道']],
  ['deadline', ['截止', 'deadline', '时限', '交期']],
  ['weakness', ['返工', '投诉', '不满意', '反复', '太慢']],
];

/**
 * 反馈自动写回队列（2026-11-06，功能线③「自动化」补齐）。
 *
 * 以前 ingestFeedback 是「人贴一段文本 → 立刻写回」：业务系统不点按钮就不会动，
 * 同一条反馈重投几次就写几次，写回结果（create/conflict/keep）没有任何留存，
 * 失败了也没法重跑。这里把反馈改成一条 append-only 的队列记录：
 *   投递 → 内容哈希去重 → 自动提炼写回 → 记录每个 key 的动作 → 冲突自动进裁决台。
 *
 * 三条规定：
 *   1. 幂等：UNIQUE(主体, text_hash)，同一条反馈无论重投多少次都只写回一次；
 *   2. 零模型：仍走 FEEDBACK_RULES 确定性规则，不调 LLM、不花额度；
 *   3. 可审计：队列留原文、来源、source_ref、命中 key、每个写回动作、失败原因。
 *
 * 表在模块内自建（幂等 DDL），不修改 sqlite.ts、不碰别人的表。
 */
const FEEDBACK_DDL = `
CREATE TABLE IF NOT EXISTS memory_feedback_queue (
  id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL DEFAULT 'customer',
  subject_id TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'api',
  source_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  keys_json TEXT NOT NULL DEFAULT '[]',
  actions_json TEXT NOT NULL DEFAULT '[]',
  conflicts INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'system',
  created_at INTEGER NOT NULL,
  processed_at INTEGER,
  UNIQUE(subject_type, subject_id, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_feedback_status
  ON memory_feedback_queue(status, created_at);`;

/** _resetForTests() 会换 db 实例，所以认实例而不是认模块级布尔。 */
let feedbackDdOwner: unknown = null;
function feedbackDb(): Conn {
  const db = getDb();
  if (feedbackDdOwner !== db) {
    db.exec(FEEDBACK_DDL);
    feedbackDdOwner = db;
  }
  return db;
}

export type FeedbackStatus = 'pending' | 'processed' | 'skipped' | 'failed';

export interface FeedbackQueueItem {
  id: string;
  subject_type: string;
  subject_id: string;
  text: string;
  source: string;
  source_ref: string;
  status: FeedbackStatus;
  profile_keys: string[];
  actions: string[];
  conflicts: number;
  error: string;
  actor: string;
  created_at: number;
  processed_at: number | null;
}

export interface FeedbackReceipt {
  id: string;
  /** true = 这条反馈已经投过（重投或补跑），本次没有重复写回。 */
  dedup: boolean;
  status: FeedbackStatus;
  profile_keys: string[];
  actions: string[];
  conflicts: number;
}

interface FeedbackRow {
  id: string;
  subject_type: string;
  subject_id: string;
  text: string;
  source: string;
  source_ref: string;
  status: FeedbackStatus;
  keys_json: string;
  actions_json: string;
  conflicts: number;
  error: string;
  actor: string;
  created_at: number;
  processed_at: number | null;
}

function jsonArray(raw: string): string[] {
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function hydrateFeedback(row: FeedbackRow): FeedbackQueueItem {
  return {
    id: row.id,
    subject_type: row.subject_type,
    subject_id: row.subject_id,
    text: row.text,
    source: row.source,
    source_ref: row.source_ref,
    status: row.status,
    profile_keys: jsonArray(row.keys_json),
    actions: jsonArray(row.actions_json),
    conflicts: row.conflicts,
    error: row.error,
    actor: row.actor,
    created_at: row.created_at,
    processed_at: row.processed_at,
  };
}

function feedbackReceipt(row: FeedbackRow, dedup: boolean): FeedbackReceipt {
  return {
    id: row.id,
    dedup,
    status: row.status,
    profile_keys: jsonArray(row.keys_json),
    actions: jsonArray(row.actions_json),
    conflicts: row.conflicts,
  };
}

/**
 * 画像里存「命中关键词的那一小句」，而不是整段反馈原文。
 * 以前整段塞进去，同一客户说两句不同话就必然同置信冲突，且画像读起来是一大段流水账；
 * 现在按标点切句取命中片段，零模型、结果确定，同一说法重复投递也能落成 keep。
 */
function feedbackSnippet(text: string, words: readonly string[]): string {
  const parts = text.split(/[，,。；;！!？?\n]+/).map((s) => s.trim()).filter(Boolean);
  const hit = parts.find((p) => words.some((w) => p.includes(w)));
  return (hit ?? text).trim().slice(0, 120);
}

function capNum(v: unknown, fallback: number, max: number): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.min(Math.trunc(n), max) : fallback;
}

/**
 * 处理一条队列记录：已 processed/skipped 的直接返回（幂等，不再触发写回）。
 * 命中不了任何规则记 skipped（不是失败，只是这条反馈没有可提炼的画像信息）；
 * 写回抛错记 failed，留 error 等补跑。conflict 是**正常结果**，不算失败 ——
 * 它由既有的「待裁决冲突」出口接住，等人定夺。
 */
function processFeedbackRow(row: FeedbackRow): FeedbackRow {
  if (row.status === 'processed' || row.status === 'skipped') return row;
  const matches = FEEDBACK_RULES.filter(([, words]) => words.some((w) => row.text.includes(w)));
  const keys: string[] = [];
  const actions: string[] = [];
  let conflicts = 0;
  let status: FeedbackStatus = matches.length ? 'processed' : 'skipped';
  let error = '';
  try {
    for (const [key, words] of matches) {
      const out = writeback({
        subject_type: row.subject_type,
        subject_id: row.subject_id,
        key,
        value: feedbackSnippet(row.text, words),
        confidence: 0.7,
        source: 'feedback',
        evidence: `[feedback:${row.id}] ${row.text}`.slice(0, MAX_TEXT),
        actor: row.actor || 'system',
      });
      keys.push(key);
      actions.push(out.action);
      if (out.action === 'conflict') conflicts++;
    }
  } catch (err) {
    status = 'failed';
    error = (err as Error).message.slice(0, 300);
  }
  const now = Date.now();
  const keysJson = JSON.stringify(keys);
  const actionsJson = JSON.stringify(actions);
  feedbackDb()
    .prepare(
      `UPDATE memory_feedback_queue
          SET status = ?, keys_json = ?, actions_json = ?, conflicts = ?, error = ?, processed_at = ?
        WHERE id = ?`,
    )
    .run(status, keysJson, actionsJson, conflicts, error, now, row.id);
  return { ...row, status, keys_json: keysJson, actions_json: actionsJson, conflicts, error, processed_at: now };
}

/** 业务系统的接入点：投一条反馈，立刻自动提炼写回；重复投递不重复写。 */
export function submitFeedback(input: {
  subject_id: string;
  text: string;
  subject_type?: string;
  source?: string;
  source_ref?: string;
  actor?: string;
}): FeedbackReceipt {
  const text = (input.text ?? '').trim().slice(0, 1000);
  const subject_id = (input.subject_id ?? '').trim();
  if (!text || !subject_id) throw new MemoryHandoffError('subject_id / text 不能为空');
  const subject_type = (input.subject_type || 'customer').trim().slice(0, 32) || 'customer';
  const source = (input.source || 'api').trim().slice(0, 64) || 'api';
  const sourceRef = (input.source_ref ?? '').trim().slice(0, 120);
  const actor = (input.actor || 'system').trim().slice(0, 64) || 'system';
  const db = feedbackDb();
  const textHash = createHash('sha256')
    .update(`${subject_type}\u0000${subject_id}\u0000${text}`)
    .digest('hex')
    .slice(0, 32);
  const inserted = db
    .prepare(
      `INSERT OR IGNORE INTO memory_feedback_queue
        (id, subject_type, subject_id, text_hash, text, source, source_ref, status, actor, created_at)
       VALUES (?,?,?,?,?,?,?,'pending',?,?)`,
    )
    .run(randomUUID(), subject_type, subject_id, textHash, text, source, sourceRef, actor, Date.now());
  const row = db
    .prepare(
      `SELECT * FROM memory_feedback_queue
        WHERE subject_type = ? AND subject_id = ? AND text_hash = ?`,
    )
    .get(subject_type, subject_id, textHash) as FeedbackRow;
  return feedbackReceipt(processFeedbackRow(row), inserted.changes === 0);
}

/** 补跑：进程重启后残留的 pending，或之前写回失败的 failed。 */
export function runFeedbackQueue(input: { limit?: number; include_failed?: boolean } = {}): {
  scanned: number;
  processed: number;
  skipped: number;
  failed: number;
  conflicts: number;
} {
  const db = feedbackDb();
  const cap = capNum(input.limit, 100, 500);
  const clause = input.include_failed === false ? "status = 'pending'" : "status IN ('pending','failed')";
  const rows = db
    .prepare(`SELECT * FROM memory_feedback_queue WHERE ${clause} ORDER BY created_at ASC LIMIT ?`)
    .all(cap) as FeedbackRow[];
  const out = { scanned: rows.length, processed: 0, skipped: 0, failed: 0, conflicts: 0 };
  for (const row of rows) {
    const done = processFeedbackRow(row);
    out[done.status === 'processed' ? 'processed' : done.status === 'skipped' ? 'skipped' : 'failed']++;
    out.conflicts += done.conflicts;
  }
  return out;
}

/** 队列审计：统计 + 最近记录（可按状态筛）。前端据此显示自动写回的运行状态。 */
export function feedbackQueue(input: { status?: string; limit?: number } = {}): {
  stats: {
    total: number; pending: number; processed: number; skipped: number;
    failed: number; conflicts: number;
  };
  items: FeedbackQueueItem[];
} {
  const db = feedbackDb();
  const groups = db
    .prepare(`SELECT status, COUNT(*) AS n, COALESCE(SUM(conflicts), 0) AS c
                FROM memory_feedback_queue GROUP BY status`)
    .all() as Array<{ status: FeedbackStatus; n: number; c: number }>;
  const stats = {
    total: 0, pending: 0, processed: 0, skipped: 0, failed: 0, conflicts: 0,
  };
  for (const g of groups) {
    stats.total += g.n;
    stats.conflicts += g.c;
    if (g.status === 'pending' || g.status === 'processed' || g.status === 'skipped' || g.status === 'failed') {
      stats[g.status] += g.n;
    }
  }
  const allowed: FeedbackStatus[] = ['pending', 'processed', 'skipped', 'failed'];
  const status = allowed.includes(input.status as FeedbackStatus) ? (input.status as FeedbackStatus) : null;
  const rows = db
    .prepare(
      `SELECT * FROM memory_feedback_queue ${status ? 'WHERE status = ?' : ''}
        ORDER BY created_at DESC LIMIT ?`,
    )
    .all(...(status ? [status, capNum(input.limit, 20, 200)] : [capNum(input.limit, 20, 200)])) as FeedbackRow[];
  return { stats, items: rows.map(hydrateFeedback) };
}

/**
 * 兼容老入口：面板上的「反馈自动提炼」和任何旧调用都走同一条队列，
 * 因此手动贴文本与业务系统投递的画像口径、去重规则完全一致。
 */
export function ingestFeedback(input: {
  subject_id: string;
  text: string;
  subject_type?: string;
  actor?: string;
}): {
  subject_id: string;
  profile_keys: string[];
  extracted: number;
  rules: number;
  feedback_id: string;
  dedup: boolean;
  status: FeedbackStatus;
} {
  const out = submitFeedback({
    subject_id: input.subject_id,
    text: input.text,
    subject_type: input.subject_type,
    source: 'feedback',
    source_ref: 'manual-ingest',
    actor: input.actor,
  });
  return {
    subject_id: (input.subject_id ?? '').trim(),
    profile_keys: out.profile_keys,
    extracted: out.profile_keys.length,
    rules: FEEDBACK_RULES.length,
    feedback_id: out.id,
    dedup: out.dedup,
    status: out.status,
  };
}

export function handoffStats(): {
  handoffs: number;
  pending: number;
  accepted: number;
  handoff_items: number;
  profiles: number;
  profile_events: number;
  /** 历史累计冲突次数；要看「还有几条要人处理」请用 pending_conflicts。 */
  conflicts: number;
  pending_conflicts: number;
  /** 反馈自动写回队列（2026-11-06）：总量 / 待处理 / 失败可补跑。 */
  feedback_total: number;
  feedback_pending: number;
  feedback_failed: number;
  /** 记忆消费留痕（2026-11-06）：contextFor 被调用的累计次数，证明记忆被用上。 */
  context_reads: number;
} {
  const db = getDb();
  const feedback = feedbackQueue({ limit: 1 }).stats;
  const n = (sql: string): number =>
    (db.prepare(sql).get() as { n: number }).n;
  return {
    feedback_total: feedback.total,
    feedback_pending: feedback.pending,
    feedback_failed: feedback.failed,
    context_reads: contextReads({ limit: 1 }).total,
    handoffs: n(`SELECT COUNT(*) AS n FROM memory_handoffs`),
    pending: n(`SELECT COUNT(*) AS n FROM memory_handoffs WHERE status = 'pending'`),
    accepted: n(`SELECT COUNT(*) AS n FROM memory_handoffs WHERE status = 'accepted'`),
    handoff_items: n(`SELECT COUNT(*) AS n FROM memory_handoff_items`),
    profiles: n(`SELECT COUNT(*) AS n FROM memory_profile_values`),
    profile_events: n(`SELECT COUNT(*) AS n FROM memory_profile_events`),
    conflicts: n(`SELECT COUNT(*) AS n FROM memory_profile_events WHERE action = 'conflict'`),
    pending_conflicts: pendingConflictCount(db),
  };
}
