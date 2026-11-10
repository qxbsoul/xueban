/**
 * groups-backend.ts — 后端合一·阶段1b（2026-06-12）：群读写后端的薄抽象。
 *
 * 背景
 * ----
 * groups-chat.ts 的回复网关需要读/写群消息做持久化。1b 之前它直连 Python
 * （pyGet/pyPostMessage → PYTHON_BACKEND_URL）。现在引入一层「群后端」抽象，
 * 让网关只依赖三个方法（getGroup / appendMessage / listMessages），由开关
 * `SF_GROUPS_BACKEND` 决定走 Python(HTTP) 还是 Node(直调 groups-store)。
 *
 *   - 'python'（默认）：保持现状——HTTP 打 Python:8000。SF_GROUPS_BACKEND 不设时
 *     行为零变化，proxyFallback 仍把 /api/groups/* 转 Python。
 *   - 'node'：直调 storage/groups-store.ts（Node SQLite）。配合 index.ts 把
 *     groupsNodeRouter 挂在 proxyFallback 之前，群数据完全留在 Node。
 *
 * 网关只用到的最小接口（不是 groups-store 全集）：
 *   - getGroup(id)        → { agent_ids, messages, workspace_dir, team_id, kind } | null
 *   - appendMessage(id,b) → 追加一条消息（noDispatch 仅 Python 语义；Node 无回复桥，忽略）
 *   - listMessages(id)    → 该群消息（升序）
 *
 * 形状对齐：两种实现都返回蛇形 key 的群/消息形状，与 Python record JSON 一致，
 * 所以 groups-chat.ts 的 GroupRecord/ChatMessageItem 解构无需区分后端。
 */

import { PYTHON_BACKEND_URL } from '../python-backend';
import * as store from './groups-store';

/** 网关消费的最小群形状（蛇形，与 Python record JSON 对齐）。 */
export interface BackendGroup {
  group_id?: string;
  agent_ids?: string[];
  messages?: Array<{
    message_id?: string;
    sender_name?: string;
    sender_kind?: string;
    content?: string;
    timestamp?: string;
    reply_to?: string | null;
    /** U0 绑角色（2026-06-23）— agent 消息发送者 agent_id 真源；user/system 为 null。 */
    agent_id?: string | null;
  }>;
  kind?: string;
  workspace_dir?: string | null;
  team_id?: string | null;
  [k: string]: unknown;
}

export interface AppendMessageBody {
  content: string;
  sender_name: string;
  sender_kind: string;
  /** U0 绑角色 — agent 消息发送者 agent_id（Python 持久化；Node store 暂未落该列，见 nodeBackend）。 */
  agent_id?: string;
}

export interface AppendMessageOpts {
  /** Python 语义：抑制其自带回复桥（X-SF-No-Dispatch）。Node 无桥 → 忽略。 */
  noDispatch?: boolean;
}

/** 网关只依赖这三个方法。两种后端各自实现。 */
export interface GroupsBackend {
  getGroup(groupId: string): Promise<BackendGroup | null>;
  appendMessage(
    groupId: string,
    body: AppendMessageBody,
    opts?: AppendMessageOpts,
  ): Promise<unknown>;
  listMessages(groupId: string): Promise<BackendGroup['messages']>;
}

// ── Python 实现（HTTP；搬自 groups-chat.ts 原 pyGet/pyPostMessage） ───────────

const pythonBackend: GroupsBackend = {
  async getGroup(groupId) {
    try {
      const res = await fetch(
        `${PYTHON_BACKEND_URL}/api/groups/${encodeURIComponent(groupId)}`,
      );
      if (!res.ok) return null;
      const json = (await res.json()) as { data?: BackendGroup } | BackendGroup;
      return ((json as { data?: BackendGroup }).data ?? json) as BackendGroup;
    } catch {
      return null;
    }
  },

  async appendMessage(groupId, body, opts = {}) {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (opts.noDispatch) headers['X-SF-No-Dispatch'] = '1';
    const res = await fetch(
      `${PYTHON_BACKEND_URL}/api/groups/${encodeURIComponent(groupId)}/messages`,
      { method: 'POST', headers, body: JSON.stringify(body) },
    );
    if (!res.ok) {
      const txt = await res.text().catch(() => '');
      throw new Error(`python persist failed ${res.status}: ${txt}`);
    }
    return res.json().catch(() => null);
  },

  async listMessages(groupId) {
    const g = await this.getGroup(groupId);
    return g?.messages ?? [];
  },
};

// ── Node 实现（直调 groups-store SQLite） ─────────────────────────────────────

const nodeBackend: GroupsBackend = {
  async getGroup(groupId) {
    return store.getGroup(groupId) as BackendGroup | null;
  },

  async appendMessage(groupId, body) {
    // Node 无 Python 回复桥，noDispatch 无意义（网关本就是唯一回复来源）。
    // 群不存在时 groups-store.appendGroupMessage 抛错，与 Python 502 语义一致
    // （网关 catch → PERSIST_FAILED）。
    // U0 绑角色 TODO：Node SQLite store 暂未落 agent_id 列（默认后端是 Python，
    // 'node'/'dual' 模式才走这里）；待加 agent_id 列后透传 body.agent_id。
    return store.appendGroupMessage(groupId, {
      content: body.content,
      sender_name: body.sender_name,
      sender_kind: body.sender_kind as store.SenderKind,
    });
  },

  async listMessages(groupId) {
    return store.listGroupMessages(groupId) as BackendGroup['messages'];
  },
};

// ── Dual 实现（阶段2 双写校验：写两边、读 Node 缺则回落 Python、后台 diff 报警） ──

/** 群一致性的廉价指纹：消息数 + 末条 sender|content 头 60 字。 */
function fingerprint(g: BackendGroup | null): string {
  if (!g) return 'null';
  const msgs = g.messages ?? [];
  const last = msgs[msgs.length - 1];
  const tail = last ? `${last.sender_kind ?? ''}:${(last.content ?? '').slice(0, 60)}` : '∅';
  return `n=${msgs.length}|${tail}`;
}

function diffAlarm(where: string, groupId: string, py: BackendGroup | null, nd: BackendGroup | null): void {
  const a = fingerprint(py);
  const b = fingerprint(nd);
  if (a !== b) {
    console.warn(`[groups-dual] ⚠ DIFF ${where} group=${groupId.slice(0, 8)} python(${a}) ≠ node(${b})`);
  }
}

const dualBackend: GroupsBackend = {
  async getGroup(groupId) {
    const nd = await nodeBackend.getGroup(groupId);
    // 后台比对 Python（不阻塞热路径）。
    void pythonBackend
      .getGroup(groupId)
      .then((py) => diffAlarm('getGroup', groupId, py, nd))
      .catch(() => { /* python 不可达不报 diff */ });
    if (nd) return nd;
    // Node 没有（未迁移/新群）→ 回落 Python，提示补迁移。
    const py = await pythonBackend.getGroup(groupId);
    if (py) console.warn(`[groups-dual] node-miss getGroup group=${groupId.slice(0, 8)} → 回落 Python（建议跑迁移脚本）`);
    return py;
  },

  async appendMessage(groupId, body, opts = {}) {
    // 写 Python（权威 + 保留回复桥语义）。失败即抛（与现状一致）。
    const pyResult = await pythonBackend.appendMessage(groupId, body, opts);
    // 写 Node（best-effort：群未迁移则 store 抛错，仅告警不阻塞聊天）。
    try {
      await nodeBackend.appendMessage(groupId, body);
    } catch (e) {
      console.warn(`[groups-dual] node appendMessage 失败 group=${groupId.slice(0, 8)}: ${e instanceof Error ? e.message : e}（建议跑迁移脚本）`);
    }
    return pyResult;
  },

  async listMessages(groupId) {
    const g = await this.getGroup(groupId);
    return g?.messages ?? [];
  },
};

/**
 * 按 `SF_GROUPS_BACKEND` 选后端：
 *   'node' → 直调 store（群完全留 Node）。
 *   'dual' → 阶段2 双写校验：写两边、读 Node 缺则回落 Python、后台 diff 报警。
 *   其余（含未设）→ Python HTTP（默认，零变化）。
 * 每次调用读 env（不缓存）——测试可在用例间切换，运行期 env 固定时开销可忽略。
 */
export function getGroupsBackend(): GroupsBackend {
  const mode = process.env.SF_GROUPS_BACKEND;
  if (mode === 'node') return nodeBackend;
  if (mode === 'dual') return dualBackend;
  return pythonBackend;
}
