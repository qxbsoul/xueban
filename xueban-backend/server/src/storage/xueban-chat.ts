import { randomUUID } from 'node:crypto';
import { getDb } from './sqlite';
import type { XuebanChatActivity } from '../services/xueban-chat-stream';

export type XuebanChatKind = 'single' | 'group';
export type XuebanMessageKind = 'user' | 'ceo' | 'agent' | 'system';
export type XuebanMessageChannel = 'chat' | 'workflow';
export type XuebanMessageStatus = 'pending' | 'waiting_approval' | 'running' | 'done' | 'failed' | 'blocked' | 'rejected';
export type XuebanApprovalDecision = 'approve' | 'reject' | 'custom';
export type XuebanApprovalOutcome = 'continue' | 'reject';

export interface XuebanChatApproval {
  id: string;
  company_id: string;
  conversation_id: string;
  run_id: string;
  task_id: string;
  task_title: string;
  /** 需要老板审批的实际内容（工单正文），不是只有标题。 */
  content: string;
  status: 'pending' | 'decided';
  decision: XuebanApprovalDecision | null;
  custom_text: string;
  outcome: XuebanApprovalOutcome | null;
  created_at: string;
  decided_at: string | null;
}

export interface XuebanChatMessageMetadata {
  strategy?: string;
  source?: string;
  assignments?: Array<{ agent_id: string; name: string; title: string; depends_on: string[] }>;
  dag?: { nodes: Array<Record<string, unknown>> };
  activity?: XuebanChatActivity;
  approval?: XuebanChatApproval;
  [key: string]: unknown;
}

export interface XuebanConversation {
  id: string;
  company_id: string;
  kind: XuebanChatKind;
  title: string;
  created_at: string;
  updated_at: string;
  /** 消息条数：前端据此判断“新对话是否还是空的”。 */
  message_count?: number;
}

export interface XuebanChatMessage {
  id: string;
  conversation_id: string;
  sender_kind: XuebanMessageKind;
  sender_name: string;
  agent_id: string | null;
  content: string;
  channel: XuebanMessageChannel;
  run_id: string | null;
  task_id: string | null;
  task_title: string | null;
  thinking: string;
  status: XuebanMessageStatus | null;
  duration_ms: number | null;
  metadata_json: string | null;
  created_at: string;
}

function init(): void {
  const db = getDb();
  db.exec(`
    CREATE TABLE IF NOT EXISTS xueban_chat_conversations (
      id TEXT PRIMARY KEY,
      company_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('single','group')),
      title TEXT NOT NULL DEFAULT '新对话',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_xueban_chat_company ON xueban_chat_conversations(company_id, updated_at);
    CREATE TABLE IF NOT EXISTS xueban_chat_messages (
      id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL,
      sender_kind TEXT NOT NULL,
      sender_name TEXT NOT NULL,
      agent_id TEXT,
      content TEXT NOT NULL,
      channel TEXT NOT NULL DEFAULT 'chat',
      run_id TEXT,
      task_id TEXT,
      task_title TEXT,
      thinking TEXT NOT NULL DEFAULT '',
      status TEXT,
      duration_ms INTEGER,
      metadata_json TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(conversation_id) REFERENCES xueban_chat_conversations(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_xueban_chat_messages ON xueban_chat_messages(conversation_id, created_at);
  `);
  const columns = db.prepare('PRAGMA table_info(xueban_chat_messages)').all() as Array<{ name: string }>;
  const additions: Array<[string, string]> = [
    ['channel', "TEXT NOT NULL DEFAULT 'chat'"],
    ['run_id', 'TEXT'],
    ['task_id', 'TEXT'],
    ['task_title', 'TEXT'],
    ['thinking', "TEXT NOT NULL DEFAULT ''"],
    ['status', 'TEXT'],
    ['duration_ms', 'INTEGER'],
    ['metadata_json', 'TEXT'],
  ];
  for (const [name, definition] of additions) {
    if (!columns.some(column => column.name === name)) {
      db.exec(`ALTER TABLE xueban_chat_messages ADD COLUMN ${name} ${definition}`);
    }
  }
  db.exec('CREATE INDEX IF NOT EXISTS idx_xueban_chat_runs ON xueban_chat_messages(conversation_id, run_id, created_at)');
}

export function listXuebanConversations(companyId: string): XuebanConversation[] {
  init();
  return getDb().prepare(`
    SELECT c.*, (SELECT COUNT(*) FROM xueban_chat_messages m WHERE m.conversation_id = c.id) AS message_count
    FROM xueban_chat_conversations c
    WHERE c.company_id = ?
    ORDER BY c.updated_at DESC
  `).all(companyId) as XuebanConversation[];
}

export function getXuebanConversation(id: string, companyId: string): XuebanConversation | null {
  init();
  return (getDb().prepare('SELECT * FROM xueban_chat_conversations WHERE id = ? AND company_id = ?').get(id, companyId) as XuebanConversation | undefined) ?? null;
}

export function createXuebanConversation(companyId: string, kind: XuebanChatKind, title = '新对话'): XuebanConversation {
  init();
  const now = new Date().toISOString();
  const row: XuebanConversation = { id: randomUUID(), company_id: companyId, kind, title, created_at: now, updated_at: now };
  getDb().prepare('INSERT INTO xueban_chat_conversations (id,company_id,kind,title,created_at,updated_at) VALUES (?,?,?,?,?,?)')
    .run(row.id, row.company_id, row.kind, row.title, row.created_at, row.updated_at);
  return row;
}

export function deleteXuebanConversation(id: string, companyId: string): boolean {
  init();
  return getDb().prepare('DELETE FROM xueban_chat_conversations WHERE id = ? AND company_id = ?').run(id, companyId).changes > 0;
}

/**
 * 历史审批没存正文时的兜底：优先用 DAG 节点里的「给老板看的审批摘要」；
 * 没有摘要时只回工单标题，绝不把岗位系统提示词（“你是…/老板要求…”）透给老板。
 */
function dagTaskPrompt(conversationId: string, runId: string | null, taskId: string): string {
  if (!runId) return '';
  const row = getDb().prepare(`SELECT metadata_json FROM xueban_chat_messages
    WHERE conversation_id = ? AND run_id = ? AND metadata_json LIKE '%"dag"%'
    ORDER BY created_at LIMIT 1`).get(conversationId, runId) as { metadata_json: string } | undefined;
  if (!row?.metadata_json) return '';
  try {
    const metadata = JSON.parse(row.metadata_json) as { dag?: { nodes?: Array<{ id?: string; title?: string; approval_brief?: string }> } };
    const node = metadata.dag?.nodes?.find(item => item.id === taskId);
    if (!node) return '';
    const brief = typeof node.approval_brief === 'string' ? node.approval_brief.trim() : '';
    if (brief) return brief;
    return node.title ? `工单：${node.title}` : '';
  } catch {
    return '';
  }
}

export function listXuebanMessages(conversationId: string): XuebanChatMessage[] {
  init();
  const rows = getDb().prepare('SELECT * FROM xueban_chat_messages WHERE conversation_id = ? ORDER BY created_at, rowid')
    .all(conversationId) as XuebanChatMessage[];
  // 旧审批消息没有 content：读时用 DAG 分工里的工单正文补齐，避免“没有具体内容”。
  return rows.map(message => {
    if (!message.metadata_json?.includes('"approval"')) return message;
    try {
      const metadata = JSON.parse(message.metadata_json) as XuebanChatMessageMetadata;
      const approval = metadata.approval;
      if (!approval || approval.content) return message;
      const prompt = dagTaskPrompt(conversationId, approval.run_id ?? message.run_id ?? null, approval.task_id);
      if (!prompt) return message;
      return { ...message, metadata_json: JSON.stringify({ ...metadata, approval: { ...approval, content: prompt } }) };
    } catch {
      return message;
    }
  });
}

export function updateXuebanMessage(
  id: string,
  patch: { content?: string; status?: XuebanMessageStatus | null; metadata?: XuebanChatMessageMetadata | null },
): XuebanChatMessage | null {
  init();
  const current = getDb().prepare('SELECT * FROM xueban_chat_messages WHERE id = ?').get(id) as XuebanChatMessage | undefined;
  if (!current) return null;
  const next: XuebanChatMessage = {
    ...current,
    content: patch.content ?? current.content,
    status: patch.status === undefined ? current.status : patch.status,
    metadata_json: patch.metadata === undefined
      ? current.metadata_json
      : (patch.metadata ? JSON.stringify(patch.metadata) : null),
  };
  getDb().prepare('UPDATE xueban_chat_messages SET content = ?, status = ?, metadata_json = ? WHERE id = ?')
    .run(next.content, next.status, next.metadata_json, id);
  return next;
}

export function appendXuebanMessage(
  conversationId: string,
  input: {
    sender_kind: XuebanMessageKind;
    sender_name: string;
    agent_id?: string | null;
    content: string;
    channel?: XuebanMessageChannel;
    run_id?: string | null;
    task_id?: string | null;
    task_title?: string | null;
    thinking?: string;
    status?: XuebanMessageStatus | null;
    duration_ms?: number | null;
    metadata?: XuebanChatMessageMetadata | null;
  },
): XuebanChatMessage {
  init();
  const now = new Date().toISOString();
  const row: XuebanChatMessage = {
    id: randomUUID(), conversation_id: conversationId, sender_kind: input.sender_kind,
    sender_name: input.sender_name, agent_id: input.agent_id ?? null, content: input.content,
    channel: input.channel ?? 'chat', run_id: input.run_id ?? null, task_id: input.task_id ?? null,
    task_title: input.task_title ?? null, thinking: input.thinking ?? '', status: input.status ?? null,
    duration_ms: input.duration_ms ?? null, metadata_json: input.metadata ? JSON.stringify(input.metadata) : null,
    created_at: now,
  };
  const db = getDb();
  db.transaction(() => {
    db.prepare('INSERT INTO xueban_chat_messages (id,conversation_id,sender_kind,sender_name,agent_id,content,channel,run_id,task_id,task_title,thinking,status,duration_ms,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)')
      .run(row.id, row.conversation_id, row.sender_kind, row.sender_name, row.agent_id, row.content, row.channel, row.run_id, row.task_id, row.task_title, row.thinking, row.status, row.duration_ms, row.metadata_json, row.created_at);
    db.prepare('UPDATE xueban_chat_conversations SET updated_at = ? WHERE id = ?').run(now, conversationId);
  })();
  return row;
}

export function renameXuebanConversation(id: string, title: string): void {
  init();
  // 定时任务对话的标题前缀是「标注」，不能被首条消息覆盖掉。
  getDb().prepare(`UPDATE xueban_chat_conversations
    SET title = CASE WHEN title LIKE '定时任务｜%' THEN '定时任务｜' || ? ELSE ? END
    WHERE id = ?`).run(title.slice(0, 32), title.slice(0, 40), id);
}


function initApprovals(): void {
  init();
  getDb().exec(`
    CREATE TABLE IF NOT EXISTS xueban_chat_approvals (
      id TEXT PRIMARY KEY,
      company_id TEXT NOT NULL,
      conversation_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      task_title TEXT NOT NULL,
      content TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL CHECK(status IN ('pending','decided')),
      decision TEXT,
      custom_text TEXT NOT NULL DEFAULT '',
      outcome TEXT,
      created_at TEXT NOT NULL,
      decided_at TEXT,
      UNIQUE(run_id, task_id),
      FOREIGN KEY(conversation_id) REFERENCES xueban_chat_conversations(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_xueban_approvals_company ON xueban_chat_approvals(company_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_xueban_approvals_conversation ON xueban_chat_approvals(conversation_id, run_id, created_at);
  `);
  // 旧库补列：审批内容（工单正文）。历史记录没有内容时保持空串，UI 会如实提示。
  const columns = getDb().prepare('PRAGMA table_info(xueban_chat_approvals)').all() as Array<{ name: string }>;
  if (!columns.some(column => column.name === 'content')) {
    getDb().exec("ALTER TABLE xueban_chat_approvals ADD COLUMN content TEXT NOT NULL DEFAULT ''");
  }
}

export function createXuebanApproval(input: {
  company_id: string;
  conversation_id: string;
  run_id: string;
  task_id: string;
  task_title: string;
  content?: string;
}): XuebanChatApproval {
  initApprovals();
  const row: XuebanChatApproval = {
    id: randomUUID(), ...input, content: input.content ?? '', status: 'pending', decision: null, custom_text: '',
    outcome: null, created_at: new Date().toISOString(), decided_at: null,
  };
  getDb().prepare(`INSERT INTO xueban_chat_approvals
    (id,company_id,conversation_id,run_id,task_id,task_title,content,status,decision,custom_text,outcome,created_at,decided_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`)
    .run(row.id, row.company_id, row.conversation_id, row.run_id, row.task_id, row.task_title, row.content,
      row.status, row.decision, row.custom_text, row.outcome, row.created_at, row.decided_at);
  return row;
}

/** 公司级审批列表（含对话标题），供左侧“人工审批”页跨对话查看。 */
export function listCompanyXuebanApprovals(companyId: string, limit = 100): Array<XuebanChatApproval & { conversation_title: string }> {
  initApprovals();
  const rows = getDb().prepare(`SELECT a.*, COALESCE(c.title, '') AS conversation_title
    FROM xueban_chat_approvals a
    LEFT JOIN xueban_chat_conversations c ON c.id = a.conversation_id
    WHERE a.company_id = ?
    ORDER BY a.created_at DESC, a.rowid DESC
    LIMIT ?`).all(companyId, Math.min(Math.max(1, limit), 200)) as Array<XuebanChatApproval & { conversation_title: string }>;
  // 历史记录没有存正文：读时从 DAG 工单补齐，保证每条审批都有可读内容。
  return rows.map(row => row.content
    ? row
    : { ...row, content: dagTaskPrompt(row.conversation_id, row.run_id, row.task_id) });
}

export interface XuebanDeliverable {
  id: string;
  title: string;
  kind: string;
  updated_at: string;
  final: { content: string; created_at: string } | null;
  employees: Array<{ id: string; name: string; task_title: string; content: string; status: string; created_at: string }>;
  pending_approvals: number;
}

/**
 * 交付成果按“对话”聚合：最终成果取总经理最后一次正式回答，员工输出作为次要记录。
 * 公司边界由 conversation.company_id 保证。
 */
export function listXuebanDeliverableConversations(companyId: string, limit = 40): XuebanDeliverable[] {
  init();
  initApprovals();
  const capped = Math.min(Math.max(1, limit), 100);
  const conversations = getDb().prepare(`SELECT id, title, kind, updated_at FROM xueban_chat_conversations
    WHERE company_id = ? ORDER BY updated_at DESC LIMIT ?`).all(companyId, capped) as Array<{ id: string; title: string; kind: string; updated_at: string }>;
  if (!conversations.length) return [];
  const ids = conversations.map(item => item.id);
  const placeholders = ids.map(() => '?').join(',');
  const messages = getDb().prepare(`SELECT id, conversation_id, sender_kind, sender_name, task_title, content, status, channel, created_at, metadata_json
    FROM xueban_chat_messages WHERE conversation_id IN (${placeholders}) ORDER BY created_at, rowid`).all(...ids) as Array<{
      id: string; conversation_id: string; sender_kind: string; sender_name: string; task_title: string | null;
      content: string; status: string | null; channel: string; created_at: string; metadata_json: string | null;
    }>;
  const pending = new Map<string, number>();
  for (const row of getDb().prepare(`SELECT conversation_id, COUNT(*) AS total FROM xueban_chat_approvals
    WHERE company_id = ? AND status = 'pending' GROUP BY conversation_id`).all(companyId) as Array<{ conversation_id: string; total: number }>) {
    pending.set(row.conversation_id, row.total);
  }
  const cap = (text: string) => (text.length > 20_000 ? `${text.slice(0, 20_000)}\n\n…（内容过长已截断）` : text);
  const items: XuebanDeliverable[] = [];
  for (const conversation of conversations) {
    const own = messages.filter(message => message.conversation_id === conversation.id);
    // 刚建还没说过话的对话不算交付成果，避免列表里出现空卡片。
    if (!own.length) continue;
    const ceo = own.filter(message => message.sender_kind === 'ceo');
    // 只有「非 workflow」且不是 DAG 分工消息的总经理发言才算最终交付。
    // 任务被中途打断时宁可不显示最终成果，也不能把分工计划冒充成交付。
    const finalMessage = ceo
      .filter(message => message.channel !== 'workflow' && !String(message.metadata_json ?? '').includes('"dag"'))
      .at(-1) ?? null;
    const employees = own
      .filter(message => message.sender_kind === 'agent')
      .map(message => ({
        id: message.id,
        name: message.sender_name,
        task_title: message.task_title ?? '',
        content: cap(message.content),
        status: message.status ?? 'done',
        created_at: message.created_at,
      }));
    items.push({
      id: conversation.id,
      title: conversation.title,
      kind: conversation.kind,
      updated_at: conversation.updated_at,
      final: finalMessage ? { content: cap(finalMessage.content), created_at: finalMessage.created_at } : null,
      employees,
      pending_approvals: pending.get(conversation.id) ?? 0,
    });
  }
  return items;
}

export function getXuebanApproval(id: string, companyId: string): XuebanChatApproval | null {
  initApprovals();
  return (getDb().prepare('SELECT * FROM xueban_chat_approvals WHERE id = ? AND company_id = ?')
    .get(id, companyId) as XuebanChatApproval | undefined) ?? null;
}

export function decideXuebanApproval(
  id: string,
  companyId: string,
  decision: XuebanApprovalDecision,
  customText: string,
  outcome: XuebanApprovalOutcome,
): XuebanChatApproval | null {
  initApprovals();
  const decidedAt = new Date().toISOString();
  const result = getDb().prepare(`UPDATE xueban_chat_approvals
    SET status = 'decided', decision = ?, custom_text = ?, outcome = ?, decided_at = ?
    WHERE id = ? AND company_id = ? AND status = 'pending'`)
    .run(decision, customText, outcome, decidedAt, id, companyId);
  return result.changes === 1 ? getXuebanApproval(id, companyId) : null;
}

export function listXuebanApprovals(conversationId: string): XuebanChatApproval[] {
  initApprovals();
  return getDb().prepare('SELECT * FROM xueban_chat_approvals WHERE conversation_id = ? ORDER BY created_at, rowid')
    .all(conversationId) as XuebanChatApproval[];
}

export function listXuebanLogConversations(
  companyId: string,
  limit: number,
  cursor?: { updated_at: string; id: string },
): XuebanConversation[] {
  init();
  const db = getDb();
  if (cursor) {
    return db.prepare(`SELECT * FROM xueban_chat_conversations
      WHERE company_id = ? AND (updated_at < ? OR (updated_at = ? AND id < ?))
      ORDER BY updated_at DESC, id DESC LIMIT ?`)
      .all(companyId, cursor.updated_at, cursor.updated_at, cursor.id, limit) as XuebanConversation[];
  }
  return db.prepare(`SELECT * FROM xueban_chat_conversations
    WHERE company_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?`)
    .all(companyId, limit) as XuebanConversation[];
}

export interface XuebanChatCheckpoint {
  id: string;
  company_id: string;
  conversation_id: string;
  run_id: string;
  task_id: string;
  task_title: string;
  sequence: number;
  snapshot_json: string;
  created_at: string;
}

export interface XuebanCheckpointSnapshot {
  messages: XuebanChatMessage[];
  approvals: XuebanChatApproval[];
  state: unknown;
}

function initCheckpoints(): void {
  initApprovals();
  getDb().exec(`
    CREATE TABLE IF NOT EXISTS xueban_chat_checkpoints (
      id TEXT PRIMARY KEY,
      company_id TEXT NOT NULL,
      conversation_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      task_title TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      snapshot_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(conversation_id) REFERENCES xueban_chat_conversations(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_xueban_checkpoints_conversation
      ON xueban_chat_checkpoints(conversation_id, sequence);
  `);
}

export function createXuebanCheckpoint(input: {
  company_id: string;
  conversation_id: string;
  run_id: string;
  task_id: string;
  task_title: string;
  snapshot: XuebanCheckpointSnapshot;
}): XuebanChatCheckpoint {
  initCheckpoints();
  const db = getDb();
  return db.transaction(() => {
    const previous = db.prepare('SELECT COALESCE(MAX(sequence), 0) AS sequence FROM xueban_chat_checkpoints WHERE conversation_id = ?')
      .get(input.conversation_id) as { sequence: number };
    const row: XuebanChatCheckpoint = {
      id: randomUUID(), company_id: input.company_id, conversation_id: input.conversation_id,
      run_id: input.run_id, task_id: input.task_id, task_title: input.task_title,
      sequence: previous.sequence + 1, snapshot_json: JSON.stringify(input.snapshot),
      created_at: new Date().toISOString(),
    };
    db.prepare(`INSERT INTO xueban_chat_checkpoints
      (id,company_id,conversation_id,run_id,task_id,task_title,sequence,snapshot_json,created_at)
      VALUES (?,?,?,?,?,?,?,?,?)`)
      .run(row.id, row.company_id, row.conversation_id, row.run_id, row.task_id, row.task_title,
        row.sequence, row.snapshot_json, row.created_at);
    return row;
  })();
}

export function getXuebanCheckpoint(id: string, companyId: string): XuebanChatCheckpoint | null {
  initCheckpoints();
  return (getDb().prepare('SELECT * FROM xueban_chat_checkpoints WHERE id = ? AND company_id = ?')
    .get(id, companyId) as XuebanChatCheckpoint | undefined) ?? null;
}

/** 原子恢复消息与审批；目标 Checkpoint 保留，失效的后续 Checkpoint 删除。 */
export function restoreXuebanCheckpoint(id: string, companyId: string, conversationId: string): { checkpoint: XuebanChatCheckpoint; snapshot: XuebanCheckpointSnapshot } {
  initCheckpoints();
  const checkpoint = getXuebanCheckpoint(id, companyId);
  if (!checkpoint || checkpoint.conversation_id !== conversationId) throw new Error('CHECKPOINT_NOT_FOUND');
  const snapshot = JSON.parse(checkpoint.snapshot_json) as XuebanCheckpointSnapshot;
  if (!Array.isArray(snapshot.messages) || !Array.isArray(snapshot.approvals)) throw new Error('CHECKPOINT_INVALID');
  // 回滚只恢复执行状态，不删除对话消息：
  // - 被重跑任务的旧产出标记 superseded，作为历史版本留在产物栏；
  // - 并行分支的成果原样保留（不因为回滚 A 而删掉 B）。
  return { checkpoint, snapshot };
}

/** 仅允许修改数据库中最后一条老板发言，并按 rowid 截断其后的整轮内容。 */
export function editLatestXuebanUserMessage(input: {
  conversationId: string;
  companyId: string;
  messageId: string;
  content: string;
}): XuebanChatMessage {
  initCheckpoints();
  const db = getDb();
  return db.transaction(() => {
    const target = db.prepare(`SELECT rowid AS message_rowid, * FROM xueban_chat_messages
      WHERE id = ? AND conversation_id = ? AND sender_kind = 'user' AND channel = 'chat'
        AND EXISTS (SELECT 1 FROM xueban_chat_conversations c WHERE c.id = conversation_id AND c.company_id = ?)`)
      .get(input.messageId, input.conversationId, input.companyId) as (XuebanChatMessage & { message_rowid: number }) | undefined;
    if (!target) throw new Error('MESSAGE_NOT_FOUND');
    const latest = db.prepare(`SELECT id FROM xueban_chat_messages
      WHERE conversation_id = ? AND sender_kind = 'user' AND channel = 'chat' ORDER BY rowid DESC LIMIT 1`)
      .get(input.conversationId) as { id: string } | undefined;
    if (latest?.id !== target.id) throw new Error('ONLY_LATEST_USER_MESSAGE_EDITABLE');
    db.prepare('UPDATE xueban_chat_messages SET content = ? WHERE rowid = ?').run(input.content, target.message_rowid);
    db.prepare('DELETE FROM xueban_chat_messages WHERE conversation_id = ? AND rowid > ?')
      .run(input.conversationId, target.message_rowid);
    if (target.run_id) {
      db.prepare('DELETE FROM xueban_chat_approvals WHERE conversation_id = ? AND run_id = ?')
        .run(input.conversationId, target.run_id);
      db.prepare('DELETE FROM xueban_chat_checkpoints WHERE conversation_id = ? AND run_id = ?')
        .run(input.conversationId, target.run_id);
    }
    const now = new Date().toISOString();
    db.prepare('UPDATE xueban_chat_conversations SET updated_at = ? WHERE id = ?').run(now, input.conversationId);
    return db.prepare('SELECT * FROM xueban_chat_messages WHERE id = ?').get(target.id) as XuebanChatMessage;
  })();
}
