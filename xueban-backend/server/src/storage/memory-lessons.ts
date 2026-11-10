/**
 * memory-lessons.ts — 经验记忆（2026-11-05，移植自 XUEBAN-OS PR#3 memory.py 设计）。
 *
 * 为什么要有它
 * ------------
 * River Memory（storage/memory-entries.ts）是人手动加的：用户不写就没有。
 * 本模块补的是另一段：系统自己在 run 过程中把经验记下来，下次同域 run
 * 自动带进提示词。与 River Memory 并存双区、互不覆盖。
 *
 * 三种记录来源，全部从已经存在的事实里挖，不额外调 LLM（省成本、不怕幻觉）：
 *   1. run 失败    —— 失败原因就是一次教训的原文（source='run_failed'）。
 *   2. 用户中断    —— 用户为什么不想让它跑完，是重要偏好（source='user_abort'）。
 *   3. 手动补充    —— 用户/前端直接写一条（source='manual'）。
 *
 * 分域规则（对齐对方 (role, service_id) → 我们 (scope_key, task_type)）：
 *   scope_key  = 技能名或 agent 角色；空 = 通用经验。
 *   task_type  = 一期留空（全通用），预留细化。
 *   召回语义（对齐对方 _in_scope）：库里域为空=通用谁都能吃；库里有域则只有
 *   同域请求能召回——不知道自己是什么域的请求，一条专属记忆都不给。
 *
 * 边界（有意为之，对齐对方设计）：
 *   - 只记结构化教训（≤600 字符），不记原文全文——记忆塞满提示词反而干扰。
 *   - 同一（scope_key+task_type）最多 8 条，超了按"最近+最有用"软归档（留痕）。
 *   - 记忆只影响提示词，不直接改产出。
 */

import { randomUUID } from 'crypto';
import { getDb } from './sqlite';
import { getSetting } from './settings';

// 一次最多带几条记忆进提示词
export const RECALL_TOP_K = 4;

// 同一个（scope_key+task_type）最多留几条
export const MAX_PER_SCOPE = 8;

// 记忆种类
export const KIND_LESSON = 'lesson'; // 教训：这次为什么失败/被打断
export const KIND_PREF = 'preference'; // 偏好：用户/业务上的倾向
export const KIND_WEAK = 'weakness'; // 弱项：反复出现的质量缺陷

export const KIND_LABEL: Record<string, string> = {
  [KIND_LESSON]: '教训',
  [KIND_PREF]: '偏好',
  [KIND_WEAK]: '弱项',
};

export const CONTENT_MAX = 600;

export interface MemoryLesson {
  id: string;
  kind: string;
  scope_key: string;
  task_type: string;
  title: string;
  content: string;
  source: string;
  source_ref: string;
  score: number;
  hits: number;
  archived: number;
  created_at: number;
  updated_at: number;
}

export class MemoryLessonError extends Error {
  status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.status = status;
  }
}

// ---------------------------------------------------------------- 写入

/**
 * 记一条经验。同一条内容重复记时不新增，只给它加权。
 *
 * 重复检测用「scope_key+task_type+内容」三元组——同一个域反复栽同一个跟头，
 * 说明这条经验值得加重，不是新知识。内容比对去掉标点空白，避免
 * 「太官方了。」和「太官方了」算两条（对齐对方 _same）。
 */
export function remember(input: {
  content: string;
  kind?: string;
  scope_key?: string;
  task_type?: string;
  source?: string;
  source_ref?: string;
  title?: string;
  score?: number;
}): MemoryLesson {
  let content = (input.content ?? '').trim();
  if (!content) throw new MemoryLessonError('记忆内容不能为空');
  if (content.length > CONTENT_MAX) content = content.slice(0, CONTENT_MAX);
  const kind = input.kind || KIND_LESSON;
  const scope_key = input.scope_key || '';
  const task_type = input.task_type || '';
  const score = typeof input.score === 'number' ? input.score : 0.6;

  const rows = getDb()
    .prepare(
      `SELECT id, content, score FROM memory_lessons
       WHERE scope_key = ? AND task_type = ? AND archived = 0`,
    )
    .all(scope_key, task_type) as Array<Pick<MemoryLesson, 'id' | 'content' | 'score'>>;

  for (const row of rows) {
    if (sameContent(row.content, content)) {
      // 已有的那条加权：分越高越靠前，但不超过 1.0
      const newScore = Math.min(1.0, Math.round(((row.score || 0.6) + 0.1) * 1000) / 1000);
      getDb()
        .prepare(
          `UPDATE memory_lessons SET score = ?, hits = hits + 1, updated_at = ? WHERE id = ?`,
        )
        .run(newScore, Date.now(), row.id);
      return getLesson(row.id)!;
    }
  }

  const id = randomUUID();
  const now = Date.now();
  getDb()
    .prepare(
      `INSERT INTO memory_lessons (id, kind, scope_key, task_type, title, content,
                                   source, source_ref, score, hits, archived, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?)`,
    )
    .run(
      id,
      kind,
      scope_key,
      task_type,
      input.title || autoTitle(content),
      content,
      input.source || '',
      input.source_ref || '',
      Math.round(score * 1000) / 1000,
      now,
      now,
    );
  trimScope(scope_key, task_type);
  return getLesson(id)!;
}

function sameContent(a: string, b: string): boolean {
  const norm = (s: string) =>
    (s || '').replace(/[\s，。、！？；：,.!?;:（）()「」"']/g, '');
  return norm(a) === norm(b);
}

function autoTitle(content: string): string {
  const head = content.trim().split(/[。！？\n]/)[0];
  return (head || content).slice(0, 30);
}

/** 同一域最多留 MAX_PER_SCOPE 条，多了软归档最没用的一条。 */
function trimScope(scope_key: string, task_type: string): void {
  const rows = getDb()
    .prepare(
      `SELECT id FROM memory_lessons
       WHERE scope_key = ? AND task_type = ? AND archived = 0
       ORDER BY score DESC, hits DESC, created_at DESC`,
    )
    .all(scope_key, task_type) as Array<{ id: string }>;
  for (const row of rows.slice(MAX_PER_SCOPE)) {
    getDb()
      .prepare(`UPDATE memory_lessons SET archived = 1, updated_at = ? WHERE id = ?`)
      .run(Date.now(), row.id);
  }
}

// ---------------------------------------------------------------- 召回

/** 取这次该带进提示词的记忆。分域口径见文件头注释。 */
export function recallLessons(
  scope_key = '',
  task_type = '',
  topK = RECALL_TOP_K,
): MemoryLesson[] {
  const rows = getDb()
    .prepare(
      `SELECT * FROM memory_lessons
       WHERE archived = 0
       ORDER BY score DESC, hits DESC, updated_at DESC
       LIMIT 60`,
    )
    .all() as MemoryLesson[];
  const hits: MemoryLesson[] = [];
  for (const row of rows) {
    if (!inScope(row, scope_key, task_type)) continue;
    hits.push(row);
    if (hits.length >= topK) break;
  }
  return hits;
}

function inScope(row: MemoryLesson, scope_key: string, task_type: string): boolean {
  // 域：空=通用；不匹配=不召回（对齐对方 _in_scope 语义）
  if (row.scope_key && (!scope_key || row.scope_key !== scope_key)) return false;
  if (row.task_type && (!task_type || row.task_type !== task_type)) return false;
  return true;
}

/** 进提示词的那段话。空字符串表示没有可用记忆（不塞废话）。 */
export function contextBlock(scope_key = '', task_type = '', topK = RECALL_TOP_K): string {
  return formatRows(recallLessons(scope_key, task_type, topK));
}

function formatRows(hits: MemoryLesson[]): string {
  if (!hits.length) return '';
  return hits.map((row) => `- [${KIND_LABEL[row.kind] ?? '经验'}] ${row.content}`).join('\n');
}

/**
 * 进提示词的经验块（含「## 过往经验」标题；2026-11-07 抽为统一入口）。
 *
 * 单域语义与旧内联实现逐字一致：开关 memory.autoLearn（关=不召回）、条数
 * memory.recallTopK（1..12 缺省 4）、空（无记忆）返回 ''、绝不抛错。
 * 传数组 = 多域合并召回（去重后整体按 score/hits/updated_at 排序，再截 topK）：
 * 通用（域空）条目天然重复出现于各域结果，合并时按 id 去重。
 */
export function promptBlock(scope: string | string[] = '', task_type = ''): string {
  try {
    if (getSetting('memory.autoLearn') === false) return '';
    const topK = getSetting('memory.recallTopK');
    const k = typeof topK === 'number' && topK > 0 ? Math.min(Math.round(topK), 12) : RECALL_TOP_K;
    const scopes = Array.isArray(scope) ? scope : [scope];
    if (scopes.length === 1) {
      const block = contextBlock(scopes[0], task_type, k);
      return block ? `\n\n## 过往经验（自动沉淀，供参考）\n\n${block}` : '';
    }
    const seen = new Set<string>();
    const merged: MemoryLesson[] = [];
    for (const s of scopes) {
      for (const row of recallLessons(s, task_type, k)) {
        if (seen.has(row.id)) continue;
        seen.add(row.id);
        merged.push(row);
      }
    }
    merged.sort(
      (a, b) => b.score - a.score || b.hits - a.hits || b.updated_at - a.updated_at,
    );
    const block = formatRows(merged.slice(0, k));
    return block ? `\n\n## 过往经验（自动沉淀，供参考）\n\n${block}` : '';
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------- 管理

export function getLesson(id: string): MemoryLesson | undefined {
  return getDb().prepare(`SELECT * FROM memory_lessons WHERE id = ?`).get(id) as
    | MemoryLesson
    | undefined;
}

export function listLessons(opts: {
  scope_key?: string;
  kind?: string;
  includeArchived?: boolean;
  limit?: number;
}): MemoryLesson[] {
  let sql = `SELECT * FROM memory_lessons WHERE 1=1`;
  const args: unknown[] = [];
  if (opts.scope_key) {
    sql += ` AND scope_key = ?`;
    args.push(opts.scope_key);
  }
  if (opts.kind) {
    sql += ` AND kind = ?`;
    args.push(opts.kind);
  }
  if (!opts.includeArchived) sql += ` AND archived = 0`;
  sql += ` ORDER BY score DESC, updated_at DESC LIMIT ?`;
  args.push(Math.min(Math.max(opts.limit ?? 100, 1), 500));
  return getDb().prepare(sql).all(...args) as MemoryLesson[];
}

export function lessonStats(): {
  total: number;
  by_kind: Record<string, number>;
  top_k: number;
  max_per_scope: number;
} {
  const rows = getDb()
    .prepare(
      `SELECT kind, COUNT(*) AS n FROM memory_lessons WHERE archived = 0 GROUP BY kind`,
    )
    .all() as Array<{ kind: string; n: number }>;
  const by_kind: Record<string, number> = {};
  let total = 0;
  for (const r of rows) {
    by_kind[r.kind] = r.n;
    total += r.n;
  }
  return { total, by_kind, top_k: RECALL_TOP_K, max_per_scope: MAX_PER_SCOPE };
}

/** 删掉一条记忆（记错了就该能删）。归档而非物理删除，留痕。 */
export function forgetLesson(id: string): MemoryLesson {
  const row = getLesson(id);
  if (!row) throw new MemoryLessonError('记忆不存在', 404);
  getDb()
    .prepare(`UPDATE memory_lessons SET archived = 1, updated_at = ? WHERE id = ?`)
    .run(Date.now(), id);
  return getLesson(id)!;
}
