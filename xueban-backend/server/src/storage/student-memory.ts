/**
 * student-memory.ts — 学生长期记忆存储层（2026-11-12）。
 *
 * 一条账号 = 一个学生：所有读写按 owner_id 隔离，不引入租户层级。
 * 三个层次严格分开：
 *   student_learning_events  真实发生的学习行为/互动事实（不随记忆归档删除）
 *   student_memory_candidates 待审核/待自动接收的原子候选
 *   student_memories         正式记忆（active/archived/superseded）+ 证据 + 版本
 *
 * 记忆写入只通过候选审核入口完成；更新先写版本快照，再改当前值。
 */
import { randomUUID } from 'node:crypto';
import { getDb } from './sqlite';

export const MEMORY_CATEGORIES = [
  'profile',
  'learning_goal',
  'preference',
  'learning_style',
  'weakness',
  'misconception',
  'learning_context',
  'feedback',
] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export type MemoryStatus = 'active' | 'archived' | 'superseded';
export type CandidateStatus = 'pending' | 'conflict' | 'accepted' | 'rejected';

export interface MemoryEvidenceInput {
  message_id: string;
  quote: string;
  conversation_id?: string;
}

export interface CandidateInput {
  owner_id: string;
  category: MemoryCategory;
  subject?: string;
  topic?: string;
  memory_key: string;
  title?: string;
  content: string;
  confidence: number;
  explicitness: 'explicit' | 'inferred';
  evidence: MemoryEvidenceInput[];
  suggested_action?: 'create' | 'update';
  reason?: string;
}

export interface StudentMemory {
  id: string;
  owner_id: string;
  category: MemoryCategory;
  subject: string;
  topic: string;
  memory_key: string;
  title: string;
  content: string;
  confidence: number;
  explicitness: string;
  status: MemoryStatus;
  version: number;
  evidence_count: number;
  source_conversation_id: string;
  source_message_id: string;
  created_at: number;
  updated_at: number;
  last_confirmed_at: number;
  expires_at: number | null;
}

export interface StudentMemoryCandidate {
  id: string;
  owner_id: string;
  category: MemoryCategory;
  subject: string;
  topic: string;
  memory_key: string;
  title: string;
  content: string;
  confidence: number;
  explicitness: string;
  evidence: MemoryEvidenceInput[];
  suggested_action: string;
  target_memory_id: string;
  status: CandidateStatus;
  reason: string;
  created_at: number;
  reviewed_at: number | null;
}

export interface MemoryJob {
  id: string;
  owner_id: string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  subject: string;
  provider: string;
  model: string;
  base_url: string;
  status: 'pending' | 'running' | 'waiting_credentials' | 'done' | 'failed';
  started_at: number | null;
  attempts: number;
  last_error: string;
  created_at: number;
  updated_at: number;
}

interface CandidateRow {
  id: string;
  owner_id: string;
  category: string;
  subject: string;
  topic: string;
  memory_key: string;
  title: string;
  content: string;
  confidence: number;
  explicitness: string;
  evidence_json: string;
  suggested_action: string;
  target_memory_id: string;
  status: string;
  reason: string;
  created_at: number;
  reviewed_at: number | null;
}

function now(): number {
  return Date.now();
}

function normalizeContent(value: string): string {
  return (value || '').replace(/[\s，。、！？；：,.!?;:（）()「」"']/g, '').toLowerCase();
}

function parseEvidence(raw: string): MemoryEvidenceInput[] {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is MemoryEvidenceInput => Boolean(item) && typeof item === 'object')
      .map((item) => ({
        message_id: String(item.message_id ?? ''),
        quote: String(item.quote ?? ''),
        conversation_id: item.conversation_id ? String(item.conversation_id) : undefined,
      }))
      .filter((item) => item.message_id && item.quote);
  } catch {
    return [];
  }
}

function hydrateCandidate(row: CandidateRow): StudentMemoryCandidate {
  return {
    ...row,
    category: row.category as MemoryCategory,
    evidence: parseEvidence(row.evidence_json),
    status: row.status as CandidateStatus,
  };
}

export function getMemory(ownerId: string, id: string): StudentMemory | undefined {
  return getDb()
    .prepare(`SELECT * FROM student_memories WHERE id = ? AND owner_id = ?`)
    .get(id, ownerId) as StudentMemory | undefined;
}

export function listMemories(input: {
  owner_id: string;
  subject?: string;
  category?: string;
  status?: string;
  limit?: number;
}): StudentMemory[] {
  let sql = `SELECT * FROM student_memories WHERE owner_id = ?`;
  const args: unknown[] = [input.owner_id];
  if (input.subject !== undefined && input.subject !== '') {
    sql += ` AND (subject = ? OR subject = '')`;
    args.push(input.subject);
  }
  if (input.category) {
    sql += ` AND category = ?`;
    args.push(input.category);
  }
  sql += ` AND status = ?`;
  args.push(input.status || 'active');
  sql += ` ORDER BY updated_at DESC LIMIT ?`;
  args.push(Math.min(Math.max(Math.trunc(input.limit ?? 100), 1), 500));
  return getDb().prepare(sql).all(...args) as StudentMemory[];
}

export function findMemoryByKey(ownerId: string, memoryKey: string): StudentMemory | undefined {
  return getDb()
    .prepare(`SELECT * FROM student_memories WHERE owner_id = ? AND memory_key = ?`)
    .get(ownerId, memoryKey) as StudentMemory | undefined;
}

/** 给已存在的正式记忆补证据（重试/重复提取时不再造新候选）。 */
export function attachEvidenceToMemory(
  ownerId: string,
  memoryId: string,
  evidence: MemoryEvidenceInput[],
  conversationId = '',
): number {
  return addEvidence(memoryId, ownerId, evidence, conversationId);
}

export function memoryEvidence(ownerId: string, memoryId: string): Array<{ id: string; message_id: string; quote: string; conversation_id: string; created_at: number }> {
  return getDb()
    .prepare(`SELECT * FROM student_memory_evidence WHERE owner_id = ? AND memory_id = ? ORDER BY created_at`)
    .all(ownerId, memoryId) as Array<{ id: string; message_id: string; quote: string; conversation_id: string; created_at: number }>;
}

export function memoryVersions(ownerId: string, memoryId: string): Array<{ version: number; snapshot_json: string; changed_by: string; reason: string; created_at: number }> {
  return getDb()
    .prepare(`SELECT version, snapshot_json, changed_by, reason, created_at FROM student_memory_versions WHERE owner_id = ? AND memory_id = ? ORDER BY version`)
    .all(ownerId, memoryId) as Array<{ version: number; snapshot_json: string; changed_by: string; reason: string; created_at: number }>;
}

// ---------------------------------------------------------------- 学习事件

export function recordLearningEvent(input: {
  owner_id: string;
  conversation_id: string;
  message_id: string;
  event_type?: string;
  subject?: string;
  topic?: string;
  content: string;
  evidence_quote?: string;
}): string {
  const id = randomUUID();
  const eventType = input.event_type || 'chat_turn';
  const existing = getDb()
    .prepare(`SELECT id FROM student_learning_events WHERE owner_id = ? AND message_id = ? AND event_type = ?`)
    .get(input.owner_id, input.message_id, eventType) as { id: string } | undefined;
  if (existing) return existing.id;
  getDb()
    .prepare(
      `INSERT OR IGNORE INTO student_learning_events
       (id, owner_id, conversation_id, message_id, event_type, subject, topic, content, evidence_quote, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?)`,
    )
    .run(
      id, input.owner_id, input.conversation_id, input.message_id,
      eventType, input.subject || '', input.topic || '',
      (input.content || '').slice(0, 4000), (input.evidence_quote || '').slice(0, 1000), now(),
    );
  return id;
}

export function listLearningEvents(ownerId: string, limit = 50): Array<Record<string, unknown>> {
  return getDb()
    .prepare(`SELECT * FROM student_learning_events WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?`)
    .all(ownerId, Math.min(Math.max(limit, 1), 200)) as Array<Record<string, unknown>>;
}

// ---------------------------------------------------------------- 候选

export function createCandidate(input: CandidateInput): StudentMemoryCandidate {
  // 任务重试时同一条候选不要重复落库（同 key + 同内容且仍在待审/冲突中）。
  const duplicates = getDb()
    .prepare(`SELECT * FROM student_memory_candidates WHERE owner_id = ? AND memory_key = ? AND status IN ('pending','conflict')`)
    .all(input.owner_id, input.memory_key) as CandidateRow[];
  const same = duplicates.find((row) => normalizeContent(row.content) === normalizeContent(input.content));
  if (same) return hydrateCandidate(same);
  const id = randomUUID();
  getDb()
    .prepare(
      `INSERT INTO student_memory_candidates
       (id, owner_id, category, subject, topic, memory_key, title, content, confidence,
        explicitness, evidence_json, suggested_action, target_memory_id, status, reason, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)`,
    )
    .run(
      id, input.owner_id, input.category, input.subject || '', input.topic || '',
      input.memory_key.slice(0, 64), (input.title || input.content).slice(0, 80), input.content.slice(0, 500),
      input.confidence, input.explicitness, JSON.stringify(input.evidence.slice(0, 8)),
      input.suggested_action || 'create', '', input.reason || '', now(),
    );
  return getCandidate(input.owner_id, id)!;
}

export function getCandidate(ownerId: string, id: string): StudentMemoryCandidate | undefined {
  const row = getDb()
    .prepare(`SELECT * FROM student_memory_candidates WHERE id = ? AND owner_id = ?`)
    .get(id, ownerId) as CandidateRow | undefined;
  return row ? hydrateCandidate(row) : undefined;
}

export function listCandidates(ownerId: string, status: string | undefined, limit = 100): StudentMemoryCandidate[] {
  let sql = `SELECT * FROM student_memory_candidates WHERE owner_id = ?`;
  const args: unknown[] = [ownerId];
  if (status) {
    sql += ` AND status = ?`;
    args.push(status);
  }
  sql += ` ORDER BY created_at DESC LIMIT ?`;
  args.push(Math.min(Math.max(limit, 1), 300));
  return (getDb().prepare(sql).all(...args) as CandidateRow[]).map(hydrateCandidate);
}

function addEvidence(memoryId: string, ownerId: string, evidence: MemoryEvidenceInput[], conversationId: string): number {
  const insert = getDb().prepare(
    `INSERT OR IGNORE INTO student_memory_evidence
     (id, owner_id, memory_id, conversation_id, message_id, quote, created_at)
     VALUES (?,?,?,?,?,?,?)`,
  );
  let added = 0;
  for (const item of evidence) {
    const result = insert.run(
      randomUUID(), ownerId, memoryId, item.conversation_id || conversationId, item.message_id,
      item.quote.slice(0, 500), now(),
    );
    added += result.changes;
  }
  return added;
}

function snapshot(memory: StudentMemory, changedBy: string, reason: string): void {
  getDb()
    .prepare(
      `INSERT INTO student_memory_versions
       (id, owner_id, memory_id, version, snapshot_json, changed_by, reason, created_at)
       VALUES (?,?,?,?,?,?,?,?)`,
    )
    .run(
      randomUUID(), memory.owner_id, memory.id, memory.version,
      JSON.stringify(memory), changedBy, reason.slice(0, 300), now(),
    );
}

export interface AcceptResult {
  action: 'created' | 'merged' | 'updated' | 'conflict' | 'rejected';
  memory?: StudentMemory;
}

/**
 * 把候选落成正式记忆。
 * - 新 key：创建 active 记忆。
 * - 同 key 且内容等价：合并证据、刷新置信度，不新增。
 * - 同 key 内容不同：人工确认则更新并保留旧版本；自动模式只标记冲突，等确认。
 */
export function acceptCandidate(
  ownerId: string,
  candidateId: string,
  options: { changedBy?: 'system' | 'student' | 'manual'; auto?: boolean; reason?: string } = {},
): AcceptResult {
  const db = getDb();
  return db.transaction((): AcceptResult => {
    const candidate = getCandidate(ownerId, candidateId);
    if (!candidate || (candidate.status !== 'pending' && candidate.status !== 'conflict')) {
      return { action: 'rejected' };
    }
    const existing = db
      .prepare(`SELECT * FROM student_memories WHERE owner_id = ? AND memory_key = ?`)
      .get(ownerId, candidate.memory_key) as StudentMemory | undefined;
    const changedBy = options.changedBy || 'system';
    const markAccepted = () => db
      .prepare(`UPDATE student_memory_candidates SET status = 'accepted', reviewed_at = ? WHERE id = ?`)
      .run(now(), candidate.id);

    if (!existing) {
      const id = randomUUID();
      const ts = now();
      db.prepare(
        `INSERT INTO student_memories
         (id, owner_id, category, subject, topic, memory_key, title, content, confidence,
          explicitness, status, version, evidence_count, source_conversation_id, source_message_id,
          created_at, updated_at, last_confirmed_at, expires_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,'active',1,?,?,?,?,?,?,NULL)`,
      ).run(
        id, ownerId, candidate.category, candidate.subject, candidate.topic, candidate.memory_key,
        candidate.title, candidate.content, candidate.confidence, candidate.explicitness,
        candidate.evidence.length, candidate.evidence[0]?.conversation_id || '',
        candidate.evidence[0]?.message_id || '', ts, ts, ts,
      );
      addEvidence(id, ownerId, candidate.evidence, '');
      markAccepted();
      return { action: 'created', memory: getMemory(ownerId, id) };
    }

    const same = normalizeContent(existing.content) === normalizeContent(candidate.content);
    if (same) {
      addEvidence(existing.id, ownerId, candidate.evidence, '');
      const evidenceCount = (db.prepare(`SELECT COUNT(*) AS n FROM student_memory_evidence WHERE memory_id = ?`).get(existing.id) as { n: number }).n;
      db.prepare(
        `UPDATE student_memories
            SET confidence = MAX(confidence, ?), evidence_count = ?, last_confirmed_at = ?, updated_at = ?,
                status = 'active'
          WHERE id = ?`,
      ).run(candidate.confidence, evidenceCount, now(), now(), existing.id);
      markAccepted();
      return { action: 'merged', memory: getMemory(ownerId, existing.id) };
    }

    if (options.auto) {
      db.prepare(`UPDATE student_memory_candidates SET status = 'conflict', reason = ? WHERE id = ?`)
        .run('existing memory differs; needs review', candidate.id);
      return { action: 'conflict' };
    }

    snapshot(existing, changedBy, options.reason || 'candidate accepted');
    const evidenceCount = addEvidence(existing.id, ownerId, candidate.evidence, '') + existing.evidence_count;
    db.prepare(
      `UPDATE student_memories
          SET content = ?, title = ?, category = ?, subject = ?, topic = ?, confidence = ?,
              explicitness = ?, version = version + 1, evidence_count = ?, last_confirmed_at = ?,
              updated_at = ?, status = 'active'
        WHERE id = ?`,
    ).run(
      candidate.content, candidate.title, candidate.category, candidate.subject, candidate.topic,
      candidate.confidence, candidate.explicitness, evidenceCount, now(), now(), existing.id,
    );
    markAccepted();
    return { action: 'updated', memory: getMemory(ownerId, existing.id) };
  })();
}

export function rejectCandidate(ownerId: string, id: string, reason = ''): boolean {
  const result = getDb()
    .prepare(`UPDATE student_memory_candidates SET status = 'rejected', reason = ?, reviewed_at = ? WHERE id = ? AND owner_id = ? AND status = 'pending'`)
    .run(reason.slice(0, 300), now(), id, ownerId);
  return result.changes > 0;
}

// ---------------------------------------------------------------- 学生纠错 / 归档

export function correctMemory(
  ownerId: string,
  id: string,
  input: { content: string; reason?: string; confidence?: number },
): StudentMemory | undefined {
  // 先校验，再写版本快照：非法请求不能留下版本记录。
  const content = (input.content ?? '').trim().slice(0, 500);
  if (!content) return undefined;
  if (input.confidence !== undefined && (!Number.isFinite(input.confidence) || input.confidence < 0 || input.confidence > 1)) {
    return undefined;
  }
  const db = getDb();
  return db.transaction(() => {
    const memory = getMemory(ownerId, id);
    if (!memory) return undefined;
    snapshot(memory, 'student', input.reason || '学生纠正');
    db.prepare(
      `UPDATE student_memories
          SET content = ?, confidence = ?, version = version + 1, updated_at = ?, last_confirmed_at = ?
        WHERE id = ? AND owner_id = ?`,
    ).run(content, input.confidence ?? memory.confidence, now(), now(), id, ownerId);
    return getMemory(ownerId, id);
  })();
}

export function archiveMemory(ownerId: string, id: string, reason = ''): boolean {
  const db = getDb();
  return db.transaction(() => {
    const memory = getMemory(ownerId, id);
    if (!memory) return false;
    snapshot(memory, 'student', reason || '归档');
    const result = db
      .prepare(`UPDATE student_memories SET status = 'archived', updated_at = ? WHERE id = ? AND owner_id = ?`)
      .run(now(), id, ownerId);
    return result.changes > 0;
  })();
}

// ---------------------------------------------------------------- 召回

export interface RecalledMemory {
  memory: StudentMemory;
  score: number;
}

function keywords(text: string): string[] {
  const parts = text.toLowerCase().split(/[\s，。、！？；：,.!?;:（）()「」"'/]+/).filter(Boolean);
  const words = new Set<string>();
  for (const part of parts) {
    if (!part) continue;
    if (/[\u4e00-\u9fa5]/.test(part)) {
      // 中文没有空格：用 2/3 元组做轻量切分，避免整句匹配不到。
      for (const size of [2, 3]) {
        for (let i = 0; i + size <= part.length; i += 1) {
          words.add(part.slice(i, i + size));
        }
      }
    } else if (part.length >= 2) {
      words.add(part);
    }
    if (words.size >= 60) break;
  }
  return [...words];
}

function memoryLine(memory: StudentMemory): string {
  const scope = memory.subject ? (memory.topic ? `${memory.subject}·${memory.topic}` : memory.subject) : '通用';
  const mark = memory.explicitness === 'inferred' ? '（推断）' : '';
  return `- [${memory.category}${mark}][${scope}] ${memory.content}`;
}

/**
 * 结构化召回：只看 active 且未过期；按学科 / 知识点 / 关键词 / 置信度 / 证据 / 新鲜度排序。
 * 配额：全局 4 条、学科 4 条、知识点 2 条，总字符约 1500。
 */
export function recallMemories(input: {
  owner_id: string;
  subject?: string;
  topic?: string;
  query?: string;
  limit?: number;
  charLimit?: number;
}): RecalledMemory[] {
  const ownerId = input.owner_id;
  if (!ownerId) return [];
  const rows = getDb()
    .prepare(`SELECT * FROM student_memories WHERE owner_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 200`)
    .all(ownerId) as StudentMemory[];
  const ts = now();
  const queryWords = keywords(input.query || '');
  const subject = (input.subject || '').trim();
  const topic = (input.topic || '').trim();
  const scored: RecalledMemory[] = [];
  for (const memory of rows) {
    if (memory.expires_at && memory.expires_at <= ts) continue;
    if (subject && memory.subject && memory.subject !== subject) continue;
    let score = memory.confidence + Math.min(memory.evidence_count, 3) * 0.2;
    if (memory.subject === subject && subject) score += 3;
    if (!memory.subject) score += 1; // 全局记忆
    if (topic && memory.topic && (memory.topic.includes(topic) || topic.includes(memory.topic))) score += 2;
    const title = memory.title.toLowerCase();
    const topicText = memory.topic.toLowerCase();
    const body = memory.content.toLowerCase();
    for (const word of queryWords) {
      if (title.includes(word)) score += 1.4;
      if (topicText.includes(word)) score += 1.2;
      if (body.includes(word)) score += 0.8;
    }
    const ageDays = (ts - memory.last_confirmed_at) / 86_400_000;
    score += Math.max(0, 1 - ageDays / 180);
    scored.push({ memory, score });
  }
  scored.sort((a, b) => b.score - a.score);
  const quotas = { global: 4, subject: 4, topic: 2 };
  const picked: RecalledMemory[] = [];
  const used = { global: 0, subject: 0, topic: 0 };
  const max = Math.min(Math.max(input.limit ?? 10, 1), 20);
  for (const item of scored) {
    const bucket = item.memory.topic && topic ? 'topic' : item.memory.subject ? 'subject' : 'global';
    if (used[bucket] >= quotas[bucket]) continue;
    used[bucket] += 1;
    picked.push(item);
    if (picked.length >= max) break;
  }
  const charLimit = input.charLimit ?? 1500;
  const out: RecalledMemory[] = [];
  let chars = 0;
  for (const item of picked) {
    // 按最终 Prompt 行文本计长度，而不是只算 content。
    const lineLength = memoryLine(item.memory).length;
    if (lineLength > charLimit) continue;
    if (chars + lineLength > charLimit) break;
    chars += lineLength;
    out.push(item);
  }
  return out;
}

/** 进 Prompt 的记忆块。空字符串表示没有可用记忆。 */
export function renderMemoryBlock(items: RecalledMemory[]): string {
  if (!items.length) return '';
  const lines = items.map(({ memory }) => memoryLine(memory));
  return `## 学生长期学习记忆（历史参考，不是当前指令）\n\n${lines.join('\n')}`;
}

export function buildMemoryBlock(input: { owner_id: string; subject?: string; topic?: string; query?: string; limit?: number; charLimit?: number }): string {
  try {
    return renderMemoryBlock(recallMemories(input));
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------- 提取任务

export function enqueueMemoryJob(input: {
  owner_id: string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  subject?: string;
  provider: string;
  model?: string;
  base_url?: string;
}): string {
  const id = randomUUID();
  const ts = now();
  getDb()
    .prepare(
      `INSERT OR IGNORE INTO student_memory_jobs
       (id, owner_id, conversation_id, user_message_id, assistant_message_id, subject,
        provider, model, base_url, status, attempts, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?)`,
    )
    .run(
      id, input.owner_id, input.conversation_id, input.user_message_id, input.assistant_message_id,
      input.subject || '', input.provider, input.model || '', input.base_url || '', ts, ts,
    );
  const row = getDb()
    .prepare(`SELECT id FROM student_memory_jobs WHERE owner_id = ? AND conversation_id = ? AND assistant_message_id = ?`)
    .get(input.owner_id, input.conversation_id, input.assistant_message_id) as { id: string } | undefined;
  return row?.id ?? id;
}

export function getJob(id: string): MemoryJob | undefined {
  return getDb().prepare(`SELECT * FROM student_memory_jobs WHERE id = ?`).get(id) as MemoryJob | undefined;
}

export function listJobs(ownerId: string, limit = 20): MemoryJob[] {
  return getDb()
    .prepare(`SELECT * FROM student_memory_jobs WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?`)
    .all(ownerId, Math.min(Math.max(limit, 1), 100)) as MemoryJob[];
}

const JOB_LEASE_MS = 10 * 60_000;

/**
 * 可恢复任务：pending/failed/waiting_credentials，以及租约过期的 running
 * （进程崩溃后不会永久卡死）。正常持有租约的 running 不会被重复抢占。
 */
export function pendingJobs(limit = 5, ownerId = ''): MemoryJob[] {
  const args: unknown[] = [];
  let sql = `SELECT * FROM student_memory_jobs WHERE attempts < 3 AND (`;
  sql += `status IN ('pending','failed','waiting_credentials')`;
  sql += ` OR (status = 'running' AND (started_at IS NULL OR started_at < ?))`;
  sql += `)`;
  args.push(Date.now() - JOB_LEASE_MS);
  if (ownerId) {
    sql += ` AND owner_id = ?`;
    args.push(ownerId);
  }
  sql += ` ORDER BY created_at LIMIT ?`;
  args.push(Math.min(Math.max(limit, 1), 20));
  return getDb().prepare(sql).all(...args) as MemoryJob[];
}

export function markJobWaitingCredentials(id: string, message: string): void {
  getDb()
    .prepare(`UPDATE student_memory_jobs SET status = 'waiting_credentials', last_error = ?, updated_at = ? WHERE id = ?`)
    .run(message.slice(0, 300), Date.now(), id);
}

export function markJobRunning(id: string): boolean {
  const ts = now();
  const result = getDb()
    .prepare(
      `UPDATE student_memory_jobs SET status = 'running', started_at = ?, updated_at = ?
        WHERE id = ? AND (
          status IN ('pending','failed','waiting_credentials')
          OR (status = 'running' AND (started_at IS NULL OR started_at < ?))
        )`,
    )
    .run(ts, ts, id, ts - JOB_LEASE_MS);
  return result.changes > 0;
}

export function finishJob(id: string): void {
  getDb()
    .prepare(`UPDATE student_memory_jobs SET status = 'done', attempts = attempts + 1, last_error = '', started_at = NULL, updated_at = ? WHERE id = ?`)
    .run(now(), id);
}

export function failJob(id: string, error: string): void {
  getDb()
    .prepare(`UPDATE student_memory_jobs SET status = 'failed', attempts = attempts + 1, last_error = ?, started_at = NULL, updated_at = ? WHERE id = ?`)
    .run(error.slice(0, 500), now(), id);
}
