/**
 * groups-store.ts — 后端合一·阶段1a（2026-06-12）：Node SQLite 侧群存储。
 *
 * 目标：在 Node 侧实现与 Python `xueban/api/groups.py`（JSON-file 群记录）
 * 逐字段等价的数据层，供 1b 的路由消费。**1a 只产 record 对象，不碰路由/信封/开关。**
 *
 * 形状对齐约定（关键）：
 *   - keys 保持「蛇形」，与 Python record JSON 输出一致（group_id / agent_ids /
 *     policy_matrix / created_at / updated_at …），这样 1b 路由能直接把 record
 *     JSON 化丢给前端，无需转换。
 *   - list/dict 字段在表里以 TEXT(JSON) 存，进出做 parse/stringify。
 *   - id 用 uuid hex（无连字符），对齐 Python 的 `uuid4().hex`。
 *   - 时间戳用 ISO（new Date().toISOString()，带 'Z'），对齐 Python 的
 *     `datetime.now(timezone.utc).isoformat()`。
 *
 * 与 Python record 的字段差异：见模块末尾 NOTE。
 *
 * 邻居参考：storage/run-index.ts（阶段0）、storage/conversations.ts（B1）。
 * 事务用 db.transaction 包「追加消息 + 群 updated_at 同步」（同 conversations.ts:appendMessage）。
 */

import { randomUUID } from 'crypto';
import { getDb } from './sqlite';

// ── id / 时间 helpers ────────────────────────────────────────────────────────

/** uuid hex（去连字符），对齐 Python `uuid4().hex`（32 位小写 hex）。 */
function hexId(): string {
  return randomUUID().replace(/-/g, '');
}

function nowIso(): string {
  return new Date().toISOString();
}

// ── 类型 ─────────────────────────────────────────────────────────────────────

export type GroupKind = 'group' | 'dm';
export type SenderKind = 'user' | 'agent' | 'system';

/** 群消息（对齐 Python MessageItem）。 */
export interface MessageItem {
  message_id: string;
  sender_name: string;
  sender_kind: SenderKind;
  content: string;
  timestamp: string; // ISO
  reply_to: string | null;
  reactions: Record<string, string[]>; // emoji → [user_id]
  pinned: boolean;
}

/** 群/DM 会话记录（对齐 Python group record JSON 形状）。 */
export interface GroupRecord {
  group_id: string;
  name: string;
  template_id: string;
  group_template_id: string;
  agent_ids: string[];
  member_emails: string[];
  policy_matrix: Record<string, unknown>;
  workspace_id: string | null;
  workspace_dir: string | null;
  team_id: string | null;
  kind: GroupKind;
  conversation_id: string | null;
  announcement: string | null;
  archived: boolean;
  user_settings: Record<string, unknown>;
  created_at: string;
  updated_at: string | null;
  messages: MessageItem[];
}

/** createGroup 入参（对齐 Python CreateGroupRequest 的默认值）。 */
export interface CreateGroupInput {
  name: string;
  template_id?: string;
  group_template_id?: string;
  agent_ids?: string[];
  member_emails?: string[];
  policy_matrix?: Record<string, unknown>;
  workspace_id?: string | null;
  workspace_dir?: string | null;
  team_id?: string | null;
  kind?: GroupKind;
  conversation_id?: string | null;
}

export interface ListGroupsOptions {
  workspace_id?: string | null;
  /**
   * 要包含的 kind 集合。省略 = ['group']（默认排除 DM，对齐 Python 默认）。
   * 1b 路由把 ?kind=dm→['dm']、?kind=all→['group','dm'] 后传入。
   */
  kinds?: GroupKind[];
  /** 默认 false：隐藏 archived=true 的群（对齐 Python include_archived 默认）。 */
  include_archived?: boolean;
}

export interface PatchGroupInput {
  name?: string;
  announcement?: string | null;
  archived?: boolean;
  workspace_dir?: string | null;
  conversation_id?: string | null;
  /** Grok 式「添加成员」：整体替换成员列表（路由层已做去重/上限校验）。 */
  agent_ids?: string[];
  /**
   * 会话形态。单聊（kind='dm'）加人后升级为 'group'——不在 store 里隐式推断，
   * 由路由层显式下发，避免一次 PATCH 同时改成员与形态时顺序歧义。
   */
  kind?: GroupKind;
}

/** appendGroupMessage 入参（message_id/timestamp 由 store 生成）。 */
export interface AppendMessageInput {
  sender_name?: string;
  sender_kind?: SenderKind;
  content: string;
  reply_to?: string | null;
}

/** per-user-per-group 偏好（对齐 Python UserGroupSetting 默认值）。 */
export interface UserGroupSetting {
  muted: boolean;
  pinned: boolean;
  folded: boolean;
  show_nickname: boolean;
  my_nickname: string | null;
}

function defaultUserSetting(): UserGroupSetting {
  return {
    muted: false,
    pinned: false,
    folded: false,
    show_nickname: true,
    my_nickname: null,
  };
}

// ── 行 ↔ record 映射 ─────────────────────────────────────────────────────────

interface GroupRow {
  group_id: string;
  name: string;
  template_id: string;
  group_template_id: string;
  agent_ids: string; // JSON
  member_emails: string; // JSON
  policy_matrix: string; // JSON
  workspace_id: string | null;
  workspace_dir: string | null;
  team_id: string | null;
  kind: string;
  conversation_id: string | null;
  announcement: string | null;
  archived: number; // 0/1
  user_settings: string; // JSON
  created_at: string;
  updated_at: string | null;
}

interface MessageRow {
  message_id: string;
  group_id: string;
  sender_name: string;
  sender_kind: string;
  content: string;
  timestamp: string;
  reply_to: string | null;
  reactions: string; // JSON
  pinned: number; // 0/1
}

function parseJson<T>(raw: string | null | undefined, fallback: T): T {
  if (raw == null || raw === '') return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function toMessageItem(row: MessageRow): MessageItem {
  return {
    message_id: row.message_id,
    sender_name: row.sender_name,
    sender_kind: (row.sender_kind || 'user') as SenderKind,
    content: row.content,
    timestamp: row.timestamp,
    reply_to: row.reply_to ?? null,
    reactions: parseJson<Record<string, string[]>>(row.reactions, {}),
    pinned: !!row.pinned,
  };
}

function loadMessages(groupId: string): MessageItem[] {
  const rows = getDb()
    .prepare(
      `SELECT * FROM group_messages WHERE group_id = ? ORDER BY timestamp ASC`,
    )
    .all(groupId) as MessageRow[];
  return rows.map(toMessageItem);
}

function toGroupRecord(row: GroupRow): GroupRecord {
  return {
    group_id: row.group_id,
    name: row.name,
    template_id: row.template_id,
    group_template_id: row.group_template_id,
    agent_ids: parseJson<string[]>(row.agent_ids, []),
    member_emails: parseJson<string[]>(row.member_emails, []),
    policy_matrix: parseJson<Record<string, unknown>>(row.policy_matrix, {}),
    workspace_id: row.workspace_id ?? null,
    workspace_dir: row.workspace_dir ?? null,
    team_id: row.team_id ?? null,
    kind: (row.kind || 'group') as GroupKind,
    conversation_id: row.conversation_id ?? null,
    announcement: row.announcement ?? null,
    archived: !!row.archived,
    user_settings: parseJson<Record<string, unknown>>(row.user_settings, {}),
    created_at: row.created_at,
    updated_at: row.updated_at ?? null,
    messages: loadMessages(row.group_id),
  };
}

function getGroupRow(groupId: string): GroupRow | undefined {
  return getDb()
    .prepare(`SELECT * FROM groups WHERE group_id = ?`)
    .get(groupId) as GroupRow | undefined;
}

// ── Group CRUD ───────────────────────────────────────────────────────────────

/**
 * 建群。name 在写入前 trim（对齐 Python create_group 的 `body.name.strip()`）。
 * 校验留给 1b 路由（name 长度 ≤40、policy_matrix ≤200 键、name 非空 400 等）。
 */
export function createGroup(input: CreateGroupInput): GroupRecord {
  const group_id = hexId();
  const created_at = nowIso();
  const name = (input.name ?? '').trim();

  getDb()
    .prepare(
      `INSERT INTO groups
        (group_id, name, template_id, group_template_id, agent_ids, member_emails,
         policy_matrix, workspace_id, workspace_dir, team_id, kind, conversation_id,
         announcement, archived, user_settings, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      group_id,
      name,
      input.template_id ?? '',
      input.group_template_id ?? '',
      JSON.stringify(input.agent_ids ?? []),
      JSON.stringify(input.member_emails ?? []),
      JSON.stringify(input.policy_matrix ?? {}),
      input.workspace_id ?? null,
      input.workspace_dir ?? null,
      input.team_id ?? null,
      input.kind || 'group',
      input.conversation_id ?? null,
      null, // announcement — 建群时无（PATCH 才设；对齐 Python record 不含该 key）
      0, // archived
      JSON.stringify({}), // user_settings
      created_at,
      null, // updated_at — 建群时 Python record 无该 key（仅 PATCH/PUT 才写）
    );

  const row = getGroupRow(group_id)!;
  return toGroupRecord(row);
}

/**
 * 后端合一·阶段2（2026-06-13）— 幂等导入一条完整 Python group record（保留原
 * group_id + 历史消息），供 groups/*.json → SQLite 迁移脚本调用。重跑安全：
 * INSERT OR REPLACE group 行 + 删旧消息再插当前消息（消息以传入 record 为准）。
 * 无回复触发、无 id 重生成——纯数据搬运。
 */
export function importGroupRecord(rec: {
  group_id: string;
  name?: string;
  template_id?: string;
  group_template_id?: string;
  agent_ids?: string[];
  member_emails?: string[];
  policy_matrix?: Record<string, unknown>;
  workspace_id?: string | null;
  workspace_dir?: string | null;
  team_id?: string | null;
  kind?: string;
  conversation_id?: string | null;
  announcement?: string | null;
  archived?: boolean;
  user_settings?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string | null;
  messages?: Array<{
    message_id?: string;
    sender_name?: string;
    sender_kind?: string;
    content?: string;
    timestamp?: string;
    reply_to?: string | null;
    reactions?: Record<string, string[]> | null;
    pinned?: boolean;
  }>;
}): void {
  const db = getDb();
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT OR REPLACE INTO groups
        (group_id, name, template_id, group_template_id, agent_ids, member_emails,
         policy_matrix, workspace_id, workspace_dir, team_id, kind, conversation_id,
         announcement, archived, user_settings, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      rec.group_id,
      (rec.name ?? '').trim(),
      rec.template_id ?? '',
      rec.group_template_id ?? '',
      JSON.stringify(rec.agent_ids ?? []),
      JSON.stringify(rec.member_emails ?? []),
      JSON.stringify(rec.policy_matrix ?? {}),
      rec.workspace_id ?? null,
      rec.workspace_dir ?? null,
      rec.team_id ?? null,
      rec.kind || 'group',
      rec.conversation_id ?? null,
      rec.announcement ?? null,
      rec.archived ? 1 : 0,
      JSON.stringify(rec.user_settings ?? {}),
      rec.created_at ?? nowIso(),
      rec.updated_at ?? null,
    );
    db.prepare(`DELETE FROM group_messages WHERE group_id = ?`).run(rec.group_id);
    const insMsg = db.prepare(
      `INSERT INTO group_messages
        (message_id, group_id, sender_name, sender_kind, content, timestamp, reply_to, reactions, pinned)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    let idx = 0;
    for (const m of rec.messages ?? []) {
      insMsg.run(
        m.message_id || `${rec.group_id}_mig_${idx++}`,
        rec.group_id,
        m.sender_name ?? 'user',
        m.sender_kind ?? 'user',
        m.content ?? '',
        m.timestamp ?? nowIso(),
        m.reply_to ?? null,
        JSON.stringify(m.reactions ?? {}),
        m.pinned ? 1 : 0,
      );
    }
  });
  tx();
}

export function getGroup(groupId: string): GroupRecord | null {
  const row = getGroupRow(groupId);
  return row ? toGroupRecord(row) : null;
}

/**
 * 列群。三过滤对齐 Python `_list_groups_records` + list 端点：
 *   - kinds：省略 → ['group']（排除 DM）。
 *   - workspace_id：严格作用域——传入时只匹配 workspace_id 完全相等的；
 *     null/缺失的记录被排除（对齐 Python 2026-05-28 strict scoping）。
 *     不传 workspace_id（undefined）= 不按 workspace 过滤。
 *   - include_archived：默认 false → 隐藏 archived=true。
 */
export function listGroups(opts: ListGroupsOptions = {}): GroupRecord[] {
  const kinds = opts.kinds && opts.kinds.length > 0 ? opts.kinds : ['group'];
  const placeholders = kinds.map(() => '?').join(', ');

  // workspace_id：undefined = 不过滤；null/string = 严格相等（null 只匹配 NULL 行）。
  const filterWorkspace = opts.workspace_id !== undefined;

  let sql = `SELECT * FROM groups WHERE kind IN (${placeholders})`;
  const params: unknown[] = [...kinds];
  if (filterWorkspace) {
    if (opts.workspace_id === null) {
      sql += ` AND workspace_id IS NULL`;
    } else {
      sql += ` AND workspace_id = ?`;
      params.push(opts.workspace_id);
    }
  }
  if (!opts.include_archived) {
    sql += ` AND archived = 0`;
  }
  // Python 按文件名（group_id）排序遍历；这里按 created_at 升序求稳定输出。
  sql += ` ORDER BY created_at ASC`;

  const rows = getDb().prepare(sql).all(...params) as GroupRow[];
  return rows.map(toGroupRecord);
}

/**
 * PATCH 群级元数据（对齐 Python patch_group）。只更新提供的字段，并总是 bump
 * updated_at（空 patch 也 bump）。workspace_dir/conversation_id 的「空串 = 清除」
 * 归一由 1b 路由处理；本层按传入值直存（null = 清除）。返回更新后 record，群不存在 → null。
 */
export function patchGroup(
  groupId: string,
  patch: PatchGroupInput,
): GroupRecord | null {
  const row = getGroupRow(groupId);
  if (!row) return null;

  const sets: string[] = [];
  const params: unknown[] = [];
  if (patch.name !== undefined) {
    sets.push('name = ?');
    params.push(patch.name);
  }
  if (patch.announcement !== undefined) {
    sets.push('announcement = ?');
    params.push(patch.announcement);
  }
  if (patch.archived !== undefined) {
    sets.push('archived = ?');
    params.push(patch.archived ? 1 : 0);
  }
  if (patch.workspace_dir !== undefined) {
    sets.push('workspace_dir = ?');
    params.push(patch.workspace_dir);
  }
  if (patch.conversation_id !== undefined) {
    sets.push('conversation_id = ?');
    params.push(patch.conversation_id);
  }
  if (patch.agent_ids !== undefined) {
    sets.push('agent_ids = ?');
    params.push(JSON.stringify(patch.agent_ids));
  }
  if (patch.kind !== undefined) {
    sets.push('kind = ?');
    params.push(patch.kind);
  }
  // 总是 bump updated_at（对齐 Python：空 body 仍 bump）。
  sets.push('updated_at = ?');
  params.push(nowIso());

  params.push(groupId);
  getDb()
    .prepare(`UPDATE groups SET ${sets.join(', ')} WHERE group_id = ?`)
    .run(...params);

  return toGroupRecord(getGroupRow(groupId)!);
}

// ── 消息 ─────────────────────────────────────────────────────────────────────

/**
 * 追加一条消息（message_id/timestamp 由本层生成，对齐 Python post_group_message）。
 * 事务包「插消息 + 群 updated_at 同步」（同 conversations.ts:appendMessage）。
 * 群不存在 → 抛错（路由层应先校验 404）。
 */
export function appendGroupMessage(
  groupId: string,
  input: AppendMessageInput,
): MessageItem {
  const db = getDb();
  const message_id = hexId();
  const timestamp = nowIso();
  const msg: MessageItem = {
    message_id,
    sender_name: input.sender_name ?? 'user',
    sender_kind: input.sender_kind ?? 'user',
    content: input.content,
    timestamp,
    reply_to: input.reply_to ?? null,
    reactions: {},
    pinned: false,
  };

  const tx = db.transaction(() => {
    const exists = db
      .prepare(`SELECT 1 FROM groups WHERE group_id = ?`)
      .get(groupId);
    if (!exists) {
      throw new Error(`Group not found: ${groupId}`);
    }
    db.prepare(
      `INSERT INTO group_messages
        (message_id, group_id, sender_name, sender_kind, content, timestamp,
         reply_to, reactions, pinned)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      msg.message_id,
      groupId,
      msg.sender_name,
      msg.sender_kind,
      msg.content,
      msg.timestamp,
      msg.reply_to,
      JSON.stringify(msg.reactions),
      0,
    );
    db.prepare(`UPDATE groups SET updated_at = ? WHERE group_id = ?`).run(
      timestamp,
      groupId,
    );
  });
  tx();

  return msg;
}

/** 列群消息（按 timestamp 升序，对齐 Python 的插入序 = 时间序）。 */
export function listGroupMessages(groupId: string): MessageItem[] {
  return loadMessages(groupId);
}

function getMessageRow(
  groupId: string,
  messageId: string,
): MessageRow | undefined {
  return getDb()
    .prepare(
      `SELECT * FROM group_messages WHERE group_id = ? AND message_id = ?`,
    )
    .get(groupId, messageId) as MessageRow | undefined;
}

// ── DM find-or-create ────────────────────────────────────────────────────────

/**
 * 解析 DM 群（对齐 Python resolve_dm_conversation）：在 (workspace_id) 作用域内
 * 找 kind='dm' 且 agent_ids 恰为 [agentId] 的现有会话；没有则建一条。
 * 幂等——同参数重复调用返回同一群。name 取 agentId（Node 1a 不查 agent 名，
 * 1b 路由如需可补；Python 端会尝试 _load_agent_record 拿名）。
 */
export function resolveDmGroup(
  agentId: string,
  workspace_id?: string | null,
): GroupRecord {
  // find：复用 listGroups 的 kind/workspace 严格过滤（include_archived 也带上，
  // DM 不该因 archived 而重复创建）。
  const candidates = listGroups({
    workspace_id,
    kinds: ['dm'],
    include_archived: true,
  });
  for (const rec of candidates) {
    if (rec.agent_ids.length === 1 && rec.agent_ids[0] === agentId) {
      return rec;
    }
  }
  // create
  return createGroup({
    name: agentId,
    agent_ids: [agentId],
    workspace_id: workspace_id ?? null,
    kind: 'dm',
  });
}

// ── reactions / pin ──────────────────────────────────────────────────────────

/**
 * 设/删一个 emoji 反应（对齐 Python toggle_reaction 的去重 + 空桶剪枝）。
 * on=true → 加（去重）；on=false → 删。返回更新后的 reactions map；
 * 群/消息不存在 → null。
 */
export function setReaction(
  groupId: string,
  messageId: string,
  emoji: string,
  userId: string,
  on: boolean,
): Record<string, string[]> | null {
  const row = getMessageRow(groupId, messageId);
  if (!row) return null;

  const reactions = parseJson<Record<string, string[]>>(row.reactions, {});
  const users = new Set(reactions[emoji] ?? []);
  if (on) {
    users.add(userId);
  } else {
    users.delete(userId);
  }
  if (users.size > 0) {
    reactions[emoji] = Array.from(users);
  } else {
    delete reactions[emoji];
  }
  getDb()
    .prepare(
      `UPDATE group_messages SET reactions = ? WHERE group_id = ? AND message_id = ?`,
    )
    .run(JSON.stringify(reactions), groupId, messageId);
  return reactions;
}

/**
 * 置顶/取消置顶一条消息（对齐 Python pin_message）。pinned 省略 → 翻转当前态；
 * 显式 true/false → 直接设。返回新 pinned 值；群/消息不存在 → null。
 */
export function setPinned(
  groupId: string,
  messageId: string,
  pinned?: boolean,
): boolean | null {
  const row = getMessageRow(groupId, messageId);
  if (!row) return null;
  const newVal = pinned === undefined ? !row.pinned : pinned;
  getDb()
    .prepare(
      `UPDATE group_messages SET pinned = ? WHERE group_id = ? AND message_id = ?`,
    )
    .run(newVal ? 1 : 0, groupId, messageId);
  return newVal;
}

// ── per-user settings ────────────────────────────────────────────────────────

/**
 * 读某用户对某群的偏好（对齐 Python get_user_group_settings）：用户从未存过 →
 * 返回干净默认 UserGroupSetting。群不存在 → null。
 */
export function getUserSettings(
  groupId: string,
  userId: string,
): UserGroupSetting | null {
  const row = getGroupRow(groupId);
  if (!row) return null;
  const all = parseJson<Record<string, UserGroupSetting>>(
    row.user_settings,
    {},
  );
  return all[userId] ?? defaultUserSetting();
}

/**
 * 整条替换某用户对某群的偏好（对齐 Python put_user_group_settings：whole-record
 * PUT，其余用户的 setting 不动 = per-user 合并）。bump updated_at。
 * 群不存在 → null。
 */
export function putUserSettings(
  groupId: string,
  userId: string,
  setting: UserGroupSetting,
): UserGroupSetting | null {
  const row = getGroupRow(groupId);
  if (!row) return null;
  const all = parseJson<Record<string, UserGroupSetting>>(
    row.user_settings,
    {},
  );
  all[userId] = setting;
  getDb()
    .prepare(`UPDATE groups SET user_settings = ?, updated_at = ? WHERE group_id = ?`)
    .run(JSON.stringify(all), nowIso(), groupId);
  return setting;
}

// ── NOTE — 与 Python record 的字段差异 ───────────────────────────────────────
//
// 1. updated_at：Python record 建群时**无**该 key（仅 PATCH/PUT/post-message 才
//    写）。本层 createGroup 把 updated_at 存 NULL，record.updated_at = null，
//    与 Python「键缺失」语义等价（1b JSON 化时 null 可保留或由路由剔除）。
// 2. announcement：Python record 建群时无该 key，PATCH 才补。本层存 NULL →
//    record.announcement = null。同上语义等价。
// 3. messages：Python 把消息内联在 group JSON 的 messages 数组里；本层拆成
//    group_messages 表，getGroup/listGroups 时 JOIN 回填成 record.messages（升序）。
//    对外 record 形状一致。
// 4. user_settings：Python record 建群时无该 key（首次 PUT 才 setdefault）。本层
//    存 '{}' → record.user_settings = {}（空对象，而非缺失）。对外读取等价。
// 5. id 形态：group_id/message_id 用 uuid hex（无连字符），对齐 Python uuid4().hex。
