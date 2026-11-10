/**
 * groups-node.ts — 后端合一·阶段1b（2026-06-12）：Node 自服务 /api/groups。
 *
 * 把 1a 的 storage/groups-store.ts（Node SQLite，蛇形 key 与 Python record 等价）
 * 接进请求路径，实现 xueban/api/groups.py 的核心端点——**响应信封/字段/状态码
 * 逐字对齐 Python**，使 SF_GROUPS_BACKEND==='node' 时前端无需任何改动即可换源。
 *
 * 挂载（index.ts）：仅当 SF_GROUPS_BACKEND==='node' 时把本 router 挂在 proxyFallback
 * **之前**。默认（python / 未设）不挂 → /api/groups/* 仍由 proxyFallback 转 Python，
 * 现状零变化。
 *
 * 与 groups-chat.ts 的拦截顺序：index.ts 先挂 groupsChatRouter（拦 POST
 * /:id/messages + GET /:id/timeline），再挂 groupsNodeRouter。Express 按注册序匹配，
 * 所以 POST /api/groups/:id/messages 仍先进 groups-chat 网关跑回复；网关内部
 * 经 getGroupsBackend() 直调 store 持久化。本 router 的 POST /:id/messages 只在
 * 网关 next() 透传时兜底（正常不会到达）——保留以保证「不挂网关也能纯持久化」。
 *
 * 信封对齐速查（对照 groups.py）：
 *   - create   → 201 GroupResponse（裸对象，无 envelope）：group_id/name/template_id/
 *                created_at/agents/workspace_id/team_id/conversation_id
 *   - list     → { data, groups, meta:{count} }（双形态：data 与 groups 同值）
 *   - get      → { data: record, meta: {} }
 *   - patch    → { data: record, meta: {} }
 *   - messages GET → { group_id, messages:[MessageItem] }（裸，无 envelope）
 *   - messages POST → 201 MessageItem（裸）
 *   - reactions → _envelope({ message_id, reactions })
 *   - pin       → _envelope({ message_id, pinned })
 *   - user-settings GET/PUT → _envelope(setting)
 *   - dm resolve → { group_id, agent_id, created }（裸）
 *   - 404 群    → { detail: { error: { code:'GROUP_NOT_FOUND', message } } }
 *   - 404 消息  → { detail: { error: { code:'MESSAGE_NOT_FOUND', message } } }
 *
 * Python 用 FastAPI，其 HTTPException(detail=...) 序列化为 `{ "detail": ... }`；
 * pydantic 校验失败为 422 `{ "detail": [...] }`。本 router 复刻：显式 400（空名）、
 * 422（name>40 / policy_matrix>200，对齐 Python pydantic Field/validator），群/消息
 * 404 用 detail.error 信封。
 */

import { Router, type Request, type Response } from 'express';
import * as store from '../storage/groups-store';

const router = Router();

/**
 * 群成员上限，对齐 groups-chat.ts 的 fanout 上限
 * （`_MAX_FANOUT_AGENTS`，Python `_chat_bridge` 同源）。加人超限时直接拒绝，
 * 不静默截断——否则前端以为加进去了，派发时却被 slice 掉。
 */
const MAX_FANOUT_AGENTS = 5;

// ── helpers ──────────────────────────────────────────────────────────────────

/** 标准信封（对齐 Python _envelope：{ data, meta }）。 */
function envelope(data: unknown, meta: Record<string, unknown> = {}) {
  return { data, meta };
}

/** 404 群（对齐 Python _group_not_found 的 detail.error 信封）。 */
function groupNotFound(res: Response, groupId: string): void {
  res.status(404).json({
    detail: {
      error: { code: 'GROUP_NOT_FOUND', message: `Group '${groupId}' not found` },
    },
  });
}

/** 404 消息（对齐 Python _message_not_found）。 */
function messageNotFound(res: Response, messageId: string): void {
  res.status(404).json({
    detail: {
      error: { code: 'MESSAGE_NOT_FOUND', message: `Message '${messageId}' not found` },
    },
  });
}

/** 422 校验失败（粗略对齐 FastAPI pydantic：{ detail:[{ loc, msg, type }] }）。 */
function validationError(res: Response, loc: string[], msg: string): void {
  res.status(422).json({ detail: [{ loc, msg, type: 'value_error' }] });
}

// ── POST /api/groups — 建群 ───────────────────────────────────────────────────
// 对齐 create_group：name trim 后非空（空→400 "name is required"）；name ≤40、
// policy_matrix ≤200 键由 pydantic 担保（这里复刻为 422）。201 + GroupResponse 裸对象。

router.post('/', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as {
    template_id?: string;
    group_template_id?: string;
    name?: unknown;
    agent_ids?: string[];
    member_emails?: string[];
    policy_matrix?: Record<string, unknown>;
    workspace_id?: string | null;
    team_id?: string | null;
    workspace_dir?: string | null;
    kind?: string;
    conversation_id?: string | null;
  };

  const rawName = typeof body.name === 'string' ? body.name : '';
  // pydantic Field(max_length=40) 作用于原始字符串（trim 前）→ 422。
  if (rawName.length > 40) {
    validationError(res, ['body', 'name'], 'ensure this value has at most 40 characters');
    return;
  }
  // field_validator policy_matrix ≤200 键 → 422。
  const policy = body.policy_matrix ?? {};
  if (Object.keys(policy).length > 200) {
    validationError(res, ['body', 'policy_matrix'], 'policy_matrix exceeds 200 key limit');
    return;
  }
  // create_group: name.strip() 后非空，否则 HTTPException(400, "name is required")。
  const name = rawName.trim();
  if (!name) {
    res.status(400).json({ detail: 'name is required' });
    return;
  }

  const rec = store.createGroup({
    name,
    template_id: body.template_id ?? '',
    group_template_id: body.group_template_id ?? '',
    agent_ids: body.agent_ids ?? [],
    member_emails: body.member_emails ?? [],
    policy_matrix: policy,
    workspace_id: body.workspace_id ?? null,
    workspace_dir: body.workspace_dir ?? null,
    team_id: body.team_id ?? null,
    kind: (body.kind as store.GroupKind) || 'group',
    conversation_id: body.conversation_id ?? null,
  });

  // GroupResponse 裸对象（agents = agent_ids）。
  res.status(201).json({
    group_id: rec.group_id,
    name: rec.name,
    template_id: rec.template_id,
    created_at: rec.created_at,
    agents: rec.agent_ids,
    workspace_id: rec.workspace_id,
    team_id: rec.team_id,
    conversation_id: rec.conversation_id,
  });
});

// ── POST /api/chat/dm/:agent_id/resolve — DM find-or-create ───────────────────
// 注意路径在 /api/chat/* 下，不在 /api/groups。index.ts 用第二个 app.use 挂载
// （见文件末尾 dmResolveRouter）。这里先定义 handler，导出供挂载。
export function handleDmResolve(req: Request, res: Response): void {
  const agentId = req.params.agent_id;
  const workspaceId =
    typeof req.query.workspace_id === 'string' ? req.query.workspace_id : null;
  const before = store.listGroups({
    workspace_id: workspaceId,
    kinds: ['dm'],
    include_archived: true,
  });
  const existing = before.find(
    (r) => r.agent_ids.length === 1 && r.agent_ids[0] === agentId,
  );
  if (existing) {
    res.json({ group_id: existing.group_id, agent_id: agentId, created: false });
    return;
  }
  const rec = store.resolveDmGroup(agentId, workspaceId);
  res.json({ group_id: rec.group_id, agent_id: agentId, created: true });
}

// ── GET /api/groups — list（双形态信封 + 三过滤） ──────────────────────────────

router.get('/', (req: Request, res: Response) => {
  const workspaceId =
    typeof req.query.workspace_id === 'string' ? req.query.workspace_id : undefined;
  const includeArchived =
    req.query.include_archived === 'true' || req.query.include_archived === '1';
  const kindParam = typeof req.query.kind === 'string' ? req.query.kind : undefined;

  let kinds: store.GroupKind[];
  if (kindParam === 'dm') kinds = ['dm'];
  else if (kindParam === 'all') kinds = ['group', 'dm'];
  else kinds = ['group'];

  const records = store.listGroups({
    workspace_id: workspaceId,
    kinds,
    include_archived: includeArchived,
  });

  res.json({
    data: records,
    groups: records, // legacy stub-compatible field（对齐 Python）
    meta: { count: records.length },
  });
});

// ── GET /api/groups/:id ───────────────────────────────────────────────────────

router.get('/:id', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  res.json(envelope(rec));
});

// ── PATCH /api/groups/:id ─────────────────────────────────────────────────────
// 对齐 patch_group：只更新提供的字段；workspace_dir/conversation_id 「空串=清除」
// 归一在路由层做（store 直存 null=清除）。总是 bump updated_at（store 负责）。

router.patch('/:id', (req: Request, res: Response) => {
  const body = (req.body ?? {}) as {
    name?: string;
    announcement?: string;
    archived?: boolean;
    workspace_dir?: string;
    conversation_id?: string;
    /** Grok 式「添加成员」：全量成员列表（前端读 group.agent_ids 后追加再回传）。 */
    agent_ids?: unknown;
    /** 单聊加人 → 'group'（加人即成群，无独立的「新建群聊」入口）。 */
    kind?: unknown;
  };

  // pydantic：name min_length=1 max_length=120。
  if (body.name !== undefined) {
    if (typeof body.name !== 'string' || body.name.length < 1 || body.name.length > 120) {
      validationError(res, ['body', 'name'], 'name must be 1..120 chars');
      return;
    }
  }

  const patch: store.PatchGroupInput = {};
  if (body.name !== undefined) patch.name = body.name;
  if (body.announcement !== undefined) patch.announcement = body.announcement;
  if (body.archived !== undefined) patch.archived = body.archived;
  if (body.workspace_dir !== undefined) {
    // 空串 = 清除（回退默认）；非空原样存。对齐 Python `.strip() or None`。
    patch.workspace_dir = body.workspace_dir.trim() || null;
  }
  if (body.conversation_id !== undefined) {
    patch.conversation_id = body.conversation_id.trim() || null;
  }

  // Grok 式成员编辑（2026-11-03）：agent_ids 全量替换。去重保序，空串剔除，
  // 上限对齐 fanout MAX_FANOUT_AGENTS（超了后端跑不动，直接拒绝而不是静默截断）。
  if (body.agent_ids !== undefined) {
    if (!Array.isArray(body.agent_ids)) {
      validationError(res, ['body', 'agent_ids'], 'agent_ids must be an array of strings');
      return;
    }
    const cleaned: string[] = [];
    for (const raw of body.agent_ids) {
      if (typeof raw !== 'string') continue;
      const id = raw.trim();
      if (id && !cleaned.includes(id)) cleaned.push(id);
    }
    if (cleaned.length > MAX_FANOUT_AGENTS) {
      validationError(
        res,
        ['body', 'agent_ids'],
        `agent_ids must be at most ${MAX_FANOUT_AGENTS} entries`,
      );
      return;
    }
    patch.agent_ids = cleaned;
  }

  // 形态切换：只允许 dm / group 两值（单聊加人 → group）。
  if (body.kind !== undefined) {
    if (body.kind !== 'dm' && body.kind !== 'group') {
      validationError(res, ['body', 'kind'], "kind must be 'dm' or 'group'");
      return;
    }
    patch.kind = body.kind;
  }

  const rec = store.patchGroup(req.params.id, patch);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  res.json(envelope(rec));
});

// ── GET /api/groups/:id/messages ──────────────────────────────────────────────
// 对齐 get_group_messages：{ group_id, messages }；limit 取末 N 条（默认 20，1..200）。

router.get('/:id/messages', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    // Python get_group_messages 用裸 detail（非 error 信封）：404 {"detail": "Group not found: <id>"}
    res.status(404).json({ detail: `Group not found: ${req.params.id}` });
    return;
  }
  let limit = 20;
  const raw = req.query.limit;
  if (typeof raw === 'string' && raw.trim()) {
    const n = Number(raw);
    if (Number.isFinite(n)) limit = Math.max(1, Math.min(200, Math.trunc(n)));
  }
  const all = store.listGroupMessages(req.params.id);
  const sliced = all.slice(-limit);
  res.json({ group_id: req.params.id, messages: sliced });
});

// ── POST /api/groups/:id/messages ─────────────────────────────────────────────
// 兜底纯持久化（正常被 groups-chat 网关先拦截）。对齐 post_group_message 的持久化部分：
// 追加 user 消息，201 + MessageItem 裸对象。**回复链路不在本端点**（网关负责）。
// content min_length=1 max_length=8000 → 422。

router.post('/:id/messages', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    res.status(404).json({ detail: `Group not found: ${req.params.id}` });
    return;
  }
  const body = (req.body ?? {}) as {
    content?: unknown;
    sender_name?: string;
    sender_kind?: string;
    reply_to?: string | null;
  };
  const content = typeof body.content === 'string' ? body.content : '';
  if (content.length < 1 || content.length > 8000) {
    validationError(res, ['body', 'content'], 'content must be 1..8000 chars');
    return;
  }
  const msg = store.appendGroupMessage(req.params.id, {
    content,
    sender_name: body.sender_name ?? 'user',
    sender_kind: (body.sender_kind as store.SenderKind) ?? 'user',
    reply_to: body.reply_to ?? null,
  });
  res.status(201).json(msg);
});

// ── POST /api/groups/:id/messages/:mid/reactions ──────────────────────────────
// 对齐 toggle_reaction：幂等切换；返回 _envelope({ message_id, reactions })。

router.post('/:id/messages/:mid/reactions', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  const body = (req.body ?? {}) as { emoji?: string; user_id?: string };
  const emoji = typeof body.emoji === 'string' ? body.emoji : '';
  if (emoji.length < 1 || emoji.length > 16) {
    validationError(res, ['body', 'emoji'], 'emoji must be 1..16 chars');
    return;
  }
  const userId = typeof body.user_id === 'string' ? body.user_id : 'anonymous';

  // 切换语义：若该用户已用此 emoji 反应过 → 移除（on=false），否则添加（on=true）。
  // store.setReaction 的 on 参数需路由先判定当前态（对齐 Python toggle）。
  const messages = store.listGroupMessages(req.params.id);
  const target = messages.find((m) => m.message_id === req.params.mid);
  if (!target) {
    messageNotFound(res, req.params.mid);
    return;
  }
  const already = (target.reactions[emoji] ?? []).includes(userId);
  const reactions = store.setReaction(
    req.params.id,
    req.params.mid,
    emoji,
    userId,
    !already,
  );
  if (reactions === null) {
    messageNotFound(res, req.params.mid);
    return;
  }
  res.json(envelope({ message_id: req.params.mid, reactions }));
});

// ── POST /api/groups/:id/messages/:mid/pin ────────────────────────────────────
// 对齐 pin_message：pinned 省略=翻转；返回 _envelope({ message_id, pinned })。

router.post('/:id/messages/:mid/pin', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  const body = (req.body ?? {}) as { pinned?: boolean };
  const pinned = store.setPinned(
    req.params.id,
    req.params.mid,
    body.pinned === undefined ? undefined : !!body.pinned,
  );
  if (pinned === null) {
    messageNotFound(res, req.params.mid);
    return;
  }
  res.json(envelope({ message_id: req.params.mid, pinned }));
});

// ── GET/PUT /api/groups/:id/user-settings ─────────────────────────────────────
// 对齐 get/put_user_group_settings：_envelope(setting)；user_id 走 query，默认 anonymous。

router.get('/:id/user-settings', (req: Request, res: Response) => {
  const userId =
    typeof req.query.user_id === 'string' ? req.query.user_id : 'anonymous';
  const setting = store.getUserSettings(req.params.id, userId);
  if (setting === null) {
    groupNotFound(res, req.params.id);
    return;
  }
  res.json(envelope(setting));
});

router.put('/:id/user-settings', (req: Request, res: Response) => {
  const userId =
    typeof req.query.user_id === 'string' ? req.query.user_id : 'anonymous';
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  const body = (req.body ?? {}) as Partial<store.UserGroupSetting>;
  // whole-record PUT：未传字段用默认（对齐 Python UserGroupSetting 默认值）。
  const setting: store.UserGroupSetting = {
    muted: !!body.muted,
    pinned: !!body.pinned,
    folded: !!body.folded,
    show_nickname: body.show_nickname === undefined ? true : !!body.show_nickname,
    my_nickname: body.my_nickname ?? null,
  };
  const saved = store.putUserSettings(req.params.id, userId, setting);
  if (saved === null) {
    groupNotFound(res, req.params.id);
    return;
  }
  res.json(envelope(saved));
});

// ── GET /api/groups/:id/events — SSE ──────────────────────────────────────────
// 对齐 stream_group_events：event agent.message / system.notice / agent.typing。
// Node 侧用本模块内存事件总线（见 groups-event-bus.ts）。群不存在 → 404。
// 帧格式：`id: <seq>\nevent: <type>\ndata: <json>\n\n`；15s keepalive 注释帧。
//
// 2026-11-07 — 断线补帧兜底：帧带组内单调 id；连接时按游标补发缓冲中缺失帧，
// 浏览器 EventSource 自动重连会带 Last-Event-ID 头（sseClient 也支持
// ?last_event_id= / ?after= 查询参数），断线窗口的帧不再永久丢失。
// 补帧顺序：先订阅再补发，live 帧 id ≤ 已补发水位则跳过（去重）。

import {
  subscribe,
  unsubscribe,
  getGroupEventsAfter,
  type GroupEvent,
} from './groups-event-bus';

/** 游标解析：Last-Event-ID 头优先，其次 ?last_event_id= / ?after=。无效 → null（全量补发）。 */
function parseReplayCursor(req: Request): number | null {
  const raw =
    (typeof req.headers['last-event-id'] === 'string' ? req.headers['last-event-id'] : undefined) ??
    (typeof req.query.last_event_id === 'string' ? req.query.last_event_id : undefined) ??
    (typeof req.query.after === 'string' ? req.query.after : undefined);
  if (raw === undefined) return null;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

router.get('/:id/events', (req: Request, res: Response) => {
  const rec = store.getGroup(req.params.id);
  if (!rec) {
    groupNotFound(res, req.params.id);
    return;
  }
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(': connected\n\n');
  // 断开的客户端继续 write 只该安静失败（bus 已吞 listener 抛错，这里兜住流错误）。
  res.on('error', () => {});
  const write = (s: string) => {
    try {
      res.write(s);
    } catch {
      /* 客户端已断开 */
    }
  };

  const groupId = req.params.id;
  const cursor = parseReplayCursor(req);
  // 水位基线从「实际发出」起算而非游标：进程重启后 seq 归零，旧游标可能远大于
  // 新 id（42 vs 6），以游标为基线会把重启后的 live 帧全部误杀。
  let lastSentId = -1;
  const onEvent = (event: GroupEvent) => {
    if (event.id <= lastSentId) return; // 补帧与 live 重叠去重
    lastSentId = event.id;
    write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event.data)}\n\n`);
  };
  // 先订阅后补帧：订阅与补发之间 publish 的帧走 live 回调，由 lastSentId 去重，
  // 不丢不重（Node 单线程下实际不会交错，去重是保险丝）。
  subscribe(groupId, onEvent);
  for (const ev of getGroupEventsAfter(groupId, cursor)) {
    onEvent(ev);
  }

  const keepalive = setInterval(() => {
    write(': keepalive\n\n');
  }, 15000);

  req.on('close', () => {
    clearInterval(keepalive);
    unsubscribe(groupId, onEvent);
  });
});

export { router as groupsNodeRouter };
export default router;
