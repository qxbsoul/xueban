"""Groups API — chat group persistence.

POST   /api/groups                                       — create a new group chat
GET    /api/groups                                       — list groups (filter by workspace_id)
GET    /api/groups/{id}                                  — get single group
GET    /api/groups/{id}/messages?limit=N                 — list recent messages
POST   /api/groups/{id}/messages                         — append a message (optional reply_to)
GET    /api/groups/{id}/briefboard?date=                 — per-day agent output feed
GET    /api/groups/{id}/messages/{msg_id}/thread         — thread view: source + replies

Storage: JSON files under .xueban/groups/{group_id}.json (mirrors
xueban/api/teams.py for consistency). Each group record holds its
own messages array — keeps a single-file invariant per group.

History
-------
2026-05-19 — Migrated from in-memory `_group_store` dict (Story 7.3 MVP) to
JSON-file persistence. The dict lost everything on uvicorn restart and the
inbox aggregator couldn't see groups created in run-session auto-save.
This rewrite also drops the template_id requirement so groups created
ad-hoc from a run-session blueprint (no template) persist correctly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from xueban.api._limiter import limiter
from xueban.services.secretary_seed import (
    SECRETARY_AGENT_ID,
    SECRETARY_GREETING,
    SECRETARY_NAME,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Storage layer — mirrors teams.py for consistency
# ---------------------------------------------------------------------------

_GROUPS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "groups"
_GROUP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_group_id(group_id: str) -> None:
    if not _GROUP_ID_RE.match(group_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_GROUP_ID", "message": "Invalid group_id format"}},
        )


def _groups_dir() -> Path:
    _GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    return _GROUPS_DIR


def _group_path(group_id: str) -> Path:
    _validate_group_id(group_id)
    groups_root = _groups_dir().resolve()
    resolved = (groups_root / f"{group_id}.json").resolve()
    if groups_root not in resolved.parents:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_GROUP_ID", "message": "Invalid group_id"}},
        )
    return resolved


def _save_group(record: Dict[str, Any]) -> None:
    target = _group_path(record["group_id"])
    content = json.dumps(record, default=str).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _load_group(group_id: str) -> Optional[Dict[str, Any]]:
    p = _group_path(group_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _list_groups_records(
    workspace_id: Optional[str] = None,
    kinds: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List group/conversation records.

    2026-05-29 — conversation 模型：每条记录有 kind（'group' | 'dm'）。默认只返回
    kind='group'（DM 是单聊，不该出现在群列表）。``kinds`` 显式传入时按它过滤
    （DM resolve 端点传 ['dm']）。老记录无 kind 字段 → 视为 'group'（向后兼容）。
    """
    allow = set(kinds) if kinds is not None else {"group"}
    d = _groups_dir()
    records: List[Dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if (rec.get("kind") or "group") not in allow:
                continue
            if workspace_id is None:
                records.append(rec)
            else:
                rec_ws = rec.get("workspace_id")
                # 2026-05-28 — strict scoping. Previously `not rec_ws` was
                # treated as a wildcard so legacy / fixture records with
                # workspace_id=null bled into every workspace's chat list.
                # Now an explicit workspace_id filter only matches records
                # whose workspace_id equals it; null / missing records are
                # excluded. To see those run an unscoped list (no query).
                if rec_ws == workspace_id:
                    records.append(rec)
        except (json.JSONDecodeError, OSError):
            pass
    return records


def list_groups(
    workspace_id: Optional[str] = None,
    kinds: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Public helper used by xueban.api.inbox to aggregate groups into
    the chat inbox view. Returns raw records — callers can reshape.
    Default excludes DM conversations (kind='dm')."""
    return _list_groups_records(workspace_id, kinds)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateGroupRequest(BaseModel):
    # template_id / group_template_id are kept optional. Run-session
    # auto-save (Step 2 of the data-vertical plan) sends empty strings.
    template_id: str = ""
    group_template_id: str = ""
    name: str = Field(..., max_length=40)
    agent_ids: List[str] = Field(default_factory=list, max_length=50)
    member_emails: List[str] = Field(default_factory=list, max_length=50)
    policy_matrix: Dict[str, Any] = Field(default_factory=dict)
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None
    # 2026-05-30 — CLI 工作目录(绝对路径)。群里选 CLI executor 回话时,Node 网关
    # (groups-chat.ts)用它当 spawn cwd。空 = 回退到仓库外默认 scratch 目录。
    # 与 workspace_id(Xueban 工作区概念)无关。
    workspace_dir: Optional[str] = None
    # 2026-05-29 — conversation kind：'group'（群聊）| 'dm'（单聊）。DM 不显示在
    # 群列表，但底层复用同一存储 + 回复桥（dispatch_agent_reply 对 N=1 即单聊）。
    kind: str = "group"
    # 2026-06-11 B1（统一会话内核）— 与 Node SQLite conversation 的显式互链。
    # run-sessions 体系的 conversation_id；有值即「该群的 run 都挂在这条
    # conversation 下」，前端可由 group 一步查到关联 run 列表。
    conversation_id: Optional[str] = None

    @field_validator("policy_matrix")
    @classmethod
    def _validate_policy_matrix_size(cls, v: dict) -> dict:
        if len(v) > 200:
            raise ValueError("policy_matrix exceeds 200 key limit")
        return v


class GroupResponse(BaseModel):
    group_id: str
    name: str
    template_id: str
    created_at: str
    agents: List[str]
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None
    # B1 统一会话内核 — 互链的 Node conversation（可选，详见 CreateGroupRequest）
    conversation_id: Optional[str] = None


class MessageItem(BaseModel):
    sender_name: str
    sender_kind: str = "user"
    content: str
    timestamp: str
    # 2026-06-23 · 消息引擎归一 U0（绑角色）— agent 消息的发送者 agent_id 真源。
    # sender_name 仅作展示（可重名）；agent_id 用于 color/avatar/权限的确定性归属。
    # user/system 消息无 agent_id（None）。老记录无该字段时 Pydantic 用默认值。
    agent_id: Optional[str] = None
    # Stream G — thread reply support. Stored on each message; nullable when the
    # message is a top-level post (not a reply). Carrying the source message_id
    # lets us query a thread via /api/groups/{gid}/messages/{mid}/thread.
    message_id: Optional[str] = None
    reply_to: Optional[str] = None
    # Stream M 2026-05-29 — message-level actions (消息悬浮工具栏接后端).
    #   reactions: emoji → [user_id]（去重；空 emoji 项会被删除）
    #   pinned:    是否被置顶
    # 老消息没有这两个字段时 Pydantic 用默认值，向后兼容。
    reactions: Optional[Dict[str, List[str]]] = None
    pinned: bool = False


class PostMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    sender_name: str = "user"
    sender_kind: str = "user"
    # Stream G — optional thread anchor. When provided, this message is a reply
    # to the message whose `message_id` equals `reply_to`.
    reply_to: Optional[str] = None
    # 2026-06-23 · U0 绑角色 — agent 回话时 Node 网关透传发送者 agent_id（见 MessageItem）。
    agent_id: Optional[str] = None


class GroupMessagesResponse(BaseModel):
    group_id: str
    messages: List[MessageItem]


class BriefBoardEntry(BaseModel):
    agent_name: str
    agent_kind: str
    summary: str
    timestamp: str


class BriefBoardData(BaseModel):
    date: str
    entries: List[BriefBoardEntry]


class BriefBoardResponse(BaseModel):
    data: BriefBoardData


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/groups", response_model=GroupResponse, status_code=201)
@limiter.limit("20/minute")
async def create_group(request: Request, body: CreateGroupRequest) -> GroupResponse:
    """Create a new group chat. Persists to .xueban/groups/{group_id}.json."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    group_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()

    record: Dict[str, Any] = {
        "group_id": group_id,
        "name": name,
        "template_id": body.template_id,
        "group_template_id": body.group_template_id,
        "agent_ids": body.agent_ids,
        "member_emails": body.member_emails,
        "policy_matrix": body.policy_matrix,
        "workspace_id": body.workspace_id,
        "workspace_dir": body.workspace_dir,
        "team_id": body.team_id,
        "kind": body.kind or "group",
        "conversation_id": body.conversation_id,
        "created_at": created_at,
        "messages": [],
    }
    _save_group(record)

    return GroupResponse(
        group_id=group_id,
        name=name,
        template_id=body.template_id,
        created_at=created_at,
        agents=body.agent_ids,
        workspace_id=body.workspace_id,
        team_id=body.team_id,
        conversation_id=body.conversation_id,
    )


# ---------------------------------------------------------------------------
# DM conversation — find-or-create（2026-05-29）
# 单聊 = kind='dm' 且 agent_ids 恰 1 个的 conversation。前端点 agent 时先 resolve
# 拿到 conversation id，再用 group 模式（同一持久化 + dispatch_agent_reply 回复桥
# + SSE）发消息。DM 不出现在群列表（_list_groups_records 默认排除 kind='dm'）。
# ---------------------------------------------------------------------------


class DmResolveResponse(BaseModel):
    group_id: str
    agent_id: str
    created: bool


@router.post("/api/chat/dm/{agent_id}/resolve", response_model=DmResolveResponse)
@limiter.limit("60/minute")
async def resolve_dm_conversation(
    request: Request,
    agent_id: str,
    workspace_id: Optional[str] = Query(default=None),
) -> DmResolveResponse:
    """Find-or-create the DM conversation for (workspace_id, agent_id).

    Idempotent: returns the existing kind='dm' conversation whose single member
    is ``agent_id`` in this workspace, or creates one. The returned ``group_id``
    is then used by the frontend exactly like a group conversation.
    """
    # 找已有 DM conversation（同 workspace + 单成员是该 agent）。
    for rec in _list_groups_records(workspace_id, kinds=["dm"]):
        ids = rec.get("agent_ids") or []
        if len(ids) == 1 and ids[0] == agent_id:
            # 秘书问候幂等补种（2026-11-06；同日「Hello World!」改常规欢迎语）：
            # 存量老会话建于播种功能上线之前，命中时零消息 → 补一条欢迎语
            # （与新建播种同款），保证「点进来就有主控消息」对老会话同样成立；
            # 已有消息绝不覆盖。
            if agent_id == SECRETARY_AGENT_ID and not (rec.get("messages") or []):
                rec["messages"] = [
                    {
                        "sender_name": SECRETARY_NAME,
                        "sender_kind": "agent",
                        "agent_id": agent_id,
                        "content": SECRETARY_GREETING,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ]
                _save_group(rec)
            return DmResolveResponse(
                group_id=rec.get("group_id", ""), agent_id=agent_id, created=False
            )

    # 没有就建一条。name 取 agent 名（拿不到就用 id），方便 header 显示。
    agent_name = agent_id
    try:
        from xueban.api._chat_bridge import _load_agent_record  # noqa: PLC0415

        arec = _load_agent_record(agent_id)
        if arec and arec.get("name"):
            agent_name = arec["name"]
    except Exception:
        pass

    group_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    # 内置秘书首聊预置问候（2026-11-06；同日「Hello World!」改常规欢迎语）：
    # 用户第一次点开秘书的会话不是空白流，而是一条欢迎语。仅 kind='dm' 且
    # 新建时写，老会话/普通 agent 零变化。sender 名跟随 agent 当前名（拿不到
    # 记录才回落 SECRETARY_NAME）。
    messages: List[Dict[str, Any]] = []
    if agent_id == SECRETARY_AGENT_ID:
        messages.append(
            {
                "sender_name": agent_name if agent_name != agent_id else SECRETARY_NAME,
                "sender_kind": "agent",
                "agent_id": agent_id,
                "content": SECRETARY_GREETING,
                "timestamp": created_at,
            }
        )
    record: Dict[str, Any] = {
        "group_id": group_id,
        "name": agent_name,
        "template_id": "",
        "group_template_id": "",
        "agent_ids": [agent_id],
        "member_emails": [],
        "policy_matrix": {},
        "workspace_id": workspace_id,
        "team_id": None,
        "kind": "dm",
        "created_at": created_at,
        "messages": messages,
    }
    _save_group(record)
    return DmResolveResponse(group_id=group_id, agent_id=agent_id, created=True)


@router.get("/api/groups")
async def list_groups_endpoint(
    workspace_id: Optional[str] = Query(default=None),
    include_archived: bool = Query(default=False),
    kind: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """List groups, optionally filtered by workspace_id.

    Wraps records in {data, meta} envelope to match other endpoints — but
    also exposes a top-level `groups` array for the front-end's existing
    stub-compatible callers. Archived groups (group-level ``archived: true``)
    are hidden by default; pass ``include_archived=true`` to include them.

    2026-06-08 — ``kind`` filters by conversation kind so the workbench 单聊
    侧栏能列出 DM 历史:None/'group'(默认,排除 DM,保持既有行为)、'dm'(只 DM)、
    'all'(群+DM)。"""
    if kind == "dm":
        kinds = ["dm"]
    elif kind == "all":
        kinds = ["group", "dm"]
    else:
        kinds = ["group"]
    records = _list_groups_records(workspace_id, kinds=kinds)
    if not include_archived:
        records = [r for r in records if not r.get("archived")]
    return {
        "data": records,
        "groups": records,  # legacy stub-compatible field
        "meta": {"count": len(records)},
    }


@router.get("/api/groups/{group_id}")
async def get_group(group_id: str) -> Dict[str, Any]:
    rec = _load_group(group_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "GROUP_NOT_FOUND", "message": f"Group {group_id!r} not found"}},
        )
    return {"data": rec, "meta": {}}


@router.get("/api/groups/{group_id}/briefboard", response_model=BriefBoardResponse)
async def get_group_briefboard(
    group_id: str,
    date: Optional[str] = Query(default=None),
) -> BriefBoardResponse:
    """Return per-agent output feed for a group on a given date.

    Aggregates run step outputs from the in-memory run store when available.
    Returns empty entries when no runs are associated with the group.
    """
    rec = _load_group(group_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")

    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Lazy import to avoid circular dependency at module level
    try:
        from xueban.runtime.service import _run_store  # noqa: PLC0415

        entries: List[BriefBoardEntry] = []
        for run_id, run_data in _run_store.items():
            if run_data.get("group_id") != group_id:
                continue
            for step in run_data.get("steps", []):
                ts: str = step.get("timestamp", "")
                if ts.startswith(target_date):
                    entries.append(
                        BriefBoardEntry(
                            agent_name=step.get("agent_name", "Agent"),
                            agent_kind=step.get("agent_kind", "acp"),
                            summary=step.get("output", ""),
                            timestamp=ts,
                        )
                    )
    except (ImportError, AttributeError):
        entries = []

    return BriefBoardResponse(data=BriefBoardData(date=target_date, entries=entries))


@router.get("/api/groups/{group_id}/messages", response_model=GroupMessagesResponse)
async def get_group_messages(
    group_id: str,
    limit: int = Query(default=20, ge=1, le=200),
) -> GroupMessagesResponse:
    """Return recent messages for a group, newest last (insertion order)."""
    rec = _load_group(group_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")

    messages: List[Dict[str, Any]] = rec.get("messages", [])
    # Return the last `limit` items in chronological order.
    sliced = messages[-limit:]
    return GroupMessagesResponse(
        group_id=group_id,
        messages=[MessageItem(**m) for m in sliced],
    )


@router.post(
    "/api/groups/{group_id}/messages",
    response_model=MessageItem,
    status_code=201,
)
@limiter.limit("60/minute")
async def post_group_message(
    request: Request,
    group_id: str,
    body: PostMessageRequest,
    background_tasks: BackgroundTasks,
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_model: Optional[str] = Header(None, alias="X-LLM-Model"),
    x_sf_no_dispatch: Optional[str] = Header(None, alias="X-SF-No-Dispatch"),
) -> MessageItem:
    """Append a message to a group's persisted message log.

    Atomic w.r.t. file IO via _save_group's tempfile-replace pattern. Not
    safe under concurrent writes to the SAME group from multiple processes;
    that's acceptable for the current single-uvicorn-process deployment.

    Side effect — Stream C chat→agent bridge: when the incoming message is
    from a user (``sender_kind == 'user'``) and the group has agent members,
    we schedule an async dispatch via ``BackgroundTasks`` that calls the
    configured BYOK LLM and appends agent replies back into this group's
    message log. The dispatch failures are isolated — they cannot affect
    the persistence success of this endpoint.

    2026-05-29 — Forward the frontend BYOK credentials (``X-LLM-*`` headers,
    same as ``/api/chat/completions``) into the background dispatch so the
    group agent can reply using the key configured in the browser, not only a
    server-side env var. ``dispatch_agent_reply`` falls back to env keys when
    these are absent.

    2026-05-29 (Node 接管) — ``X-SF-No-Dispatch`` header suppresses the Python
    reply bridge. The Node gateway (``server/src/routes/groups-chat.ts``) now
    intercepts this endpoint, persists the user message here with this header
    set (so Python does NOT also reply — would double up), then generates the
    agent reply via its own dispatcher (CLI/API) and POSTs it back here as a
    ``sender_kind='agent'`` message. Direct (non-Node) callers omit the header
    and keep the legacy Python bridge as a fallback.

    SSE publish — agent/system messages are pushed onto the group's event bus
    so the browser sees them in real time regardless of which process wrote
    them (Python bridge OR Node gateway). User messages are NOT published (the
    sender already shows them optimistically; publishing would echo-duplicate).
    """
    rec = _load_group(group_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")

    msg = {
        "sender_name": body.sender_name,
        "sender_kind": body.sender_kind,
        "content": body.content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Stream G — give every message a stable id so it can be referenced by
        # future replies. Existing records without `message_id` are still served
        # back via get_group_messages (Pydantic treats the field as Optional).
        "message_id": uuid4().hex,
        "reply_to": body.reply_to,
        # U0 绑角色 — 持久化发送者 agent_id；随 msg 一并经 SSE publish 推给前端。
        "agent_id": body.agent_id,
    }
    messages = rec.get("messages", [])
    messages.append(msg)
    rec["messages"] = messages
    _save_group(rec)

    # Push agent/system messages onto the SSE bus so subscribed browsers see
    # them live. Covers the Node-gateway path (Node writes agent replies here)
    # AND any direct agent/system writer. User messages skip publish (the
    # sender renders them optimistically; publishing would duplicate).
    if body.sender_kind in ("agent", "system"):
        try:
            from xueban.api._group_events import publish_group_event  # noqa: PLC0415

            event_type = "system.notice" if body.sender_kind == "system" else "agent.message"
            publish_group_event(group_id, {"type": event_type, "data": msg})
        except Exception:  # pragma: no cover — SSE push must never fail the write
            pass

    # Stream C bridge: only dispatch on user-originated messages. Agent /
    # system messages should never trigger another agent reply (would loop).
    # Skipped when X-SF-No-Dispatch is set — the Node gateway owns the reply.
    if body.sender_kind == "user" and rec.get("agent_ids") and not x_sf_no_dispatch:
        from xueban.api._chat_bridge import (  # noqa: PLC0415
            build_byok_override,
            dispatch_agent_reply,
        )

        byok_override = build_byok_override(x_llm_provider, x_llm_key, x_llm_model)
        background_tasks.add_task(dispatch_agent_reply, group_id, byok_override)

    return MessageItem(**msg)


# ---------------------------------------------------------------------------
# 2026-05-29 — Group chat SSE: real-time push of async agent replies.
# Mirrors stream_approval_events / stream_run_events. Lets the browser see the
# _chat_bridge fan-out replies the moment they land, instead of polling.
# ---------------------------------------------------------------------------


@router.get("/api/groups/{group_id}/events")
async def stream_group_events(group_id: str, request: Request) -> StreamingResponse:
    """SSE stream of a group's live events.

    Events emitted (``id:`` seq → ``event:`` line → ``data:`` JSON):
      agent.message  — an agent reply was appended to the group
      system.notice  — a bridge/system notice was appended
      agent.typing   — an agent is composing a reply (optional UX hint)

    Keepalive comments every 15s keep the connection open through proxies.

    2026-11-07 — Disconnect replay: frames carry a per-group monotonic ``id``.
    On connect the stream first replays buffered events missing since the
    client's cursor (``Last-Event-ID`` header — sent automatically by the
    browser's EventSource auto-reconnect — or ``?last_event_id=`` / ``?after=``),
    then continues live. Without a cursor the whole buffer is replayed; the
    client dedupes by message_id so duplicates are harmless.
    """
    if _load_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")

    from xueban.api._group_events import (  # noqa: PLC0415
        get_group_events_after,
        subscribe,
        unsubscribe,
    )

    cursor = _parse_replay_cursor(request)

    async def _generate():
        q = subscribe(group_id)
        try:
            yield ": connected\n\n"
            # 补帧：先订阅再补发 —— 订阅与快照之间 publish 的帧两路都会到，
            # 靠 last_sent 水位去重。水位从「实际发出」起算，不从游标起算
            # （进程重启后 seq 归零，旧游标可能远大于新 id，以游标为基线会
            # 把重启后的 live 帧全部误杀）。
            last_sent = -1
            for event in get_group_events_after(group_id, cursor):
                last_sent = max(last_sent, event.get("id", -1))
                yield _format_group_event(event)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    ev_id = event.get("id")
                    if ev_id is not None and ev_id <= last_sent:
                        continue  # already replayed (or older) — dedupe
                    if ev_id is not None:
                        last_sent = ev_id
                    yield _format_group_event(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe(group_id, q)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _format_group_event(event: Dict[str, Any]) -> str:
    """One SSE frame: ``id:`` (when present) + ``event:`` + ``data:``."""
    event_type = event.get("type", "message")
    data = json.dumps(event.get("data", {}), default=str)
    ev_id = event.get("id")
    id_line = f"id: {ev_id}\n" if ev_id is not None else ""
    return f"{id_line}event: {event_type}\ndata: {data}\n\n"


def _parse_replay_cursor(request: Request) -> Optional[int]:
    """Reconnect cursor: ``Last-Event-ID`` header first, then ?last_event_id= / ?after=.

    Missing/invalid → None (replay the whole buffer; see get_group_events_after).
    """
    raw = request.headers.get("last-event-id")
    if raw is None:
        raw = request.query_params.get("last_event_id")
    if raw is None:
        raw = request.query_params.get("after")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


# ---------------------------------------------------------------------------
# Stream M 2026-05-29 — message-level actions: reactions + pin
# （消息悬浮工具栏「反应」「Pin」接后端；翻译/AI改写/转发见批 2）
# ---------------------------------------------------------------------------


class ReactionRequest(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=16)
    # auth 未落地前 user_id 走前端传入，默认 anonymous（与 user-settings 一致）
    user_id: str = "anonymous"


class PinRequest(BaseModel):
    # None = toggle（不传就翻转）；显式 true/false = 直接设定
    pinned: Optional[bool] = None


def _message_not_found(message_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "MESSAGE_NOT_FOUND", "message": f"Message {message_id!r} not found"}},
    )


def _find_message(rec: Dict[str, Any], message_id: str) -> Optional[Dict[str, Any]]:
    for m in rec.get("messages", []):
        if m.get("message_id") == message_id:
            return m
    return None


@router.post("/api/groups/{group_id}/messages/{message_id}/reactions")
@limiter.limit("120/minute")
async def toggle_reaction(
    request: Request, group_id: str, message_id: str, body: ReactionRequest
) -> Dict[str, Any]:
    """Toggle one emoji reaction by one user on a message.

    Idempotent toggle: if the user already reacted with this emoji, the
    reaction is removed; otherwise it is added. Empty emoji buckets are
    pruned so the stored map stays clean.
    """
    rec = _load_group(group_id)
    if rec is None:
        raise _group_not_found(group_id)
    target = _find_message(rec, message_id)
    if target is None:
        raise _message_not_found(message_id)

    reactions: Dict[str, List[str]] = dict(target.get("reactions") or {})
    users = list(reactions.get(body.emoji, []))
    if body.user_id in users:
        users.remove(body.user_id)
    else:
        users.append(body.user_id)
    if users:
        reactions[body.emoji] = users
    else:
        reactions.pop(body.emoji, None)
    target["reactions"] = reactions
    _save_group(rec)
    return _envelope({"message_id": message_id, "reactions": reactions})


@router.post("/api/groups/{group_id}/messages/{message_id}/pin")
@limiter.limit("120/minute")
async def pin_message(
    request: Request, group_id: str, message_id: str, body: PinRequest
) -> Dict[str, Any]:
    """Pin or unpin a message. Body `pinned` omitted → toggle current state."""
    rec = _load_group(group_id)
    if rec is None:
        raise _group_not_found(group_id)
    target = _find_message(rec, message_id)
    if target is None:
        raise _message_not_found(message_id)

    new_val = (not bool(target.get("pinned", False))) if body.pinned is None else body.pinned
    target["pinned"] = new_val
    _save_group(rec)
    return _envelope({"message_id": message_id, "pinned": new_val})


# ---------------------------------------------------------------------------
# L1 缺口补齐（2026-08-22）— message 👍/👎 feedback
#
# POST /api/messages/{message_id}/feedback { rating: 1 | -1, user_id? }
# GET  /api/messages/{message_id}/feedback?user_id=
#
# 为什么不在群记录里就地存：单聊（run-session）消息的 id 由 Node 网关管线
# 生成，不在任何群 JSON 里；群消息 message_id 又是全局唯一（uuid4 hex）。
# 所以反馈落一个按 message_id 索引的独立 JSON 仓，两种消息统一可用。
# ---------------------------------------------------------------------------

_FEEDBACK_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "feedback"
_FEEDBACK_FILE_NAME = "message_feedback.json"
_MESSAGE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _feedback_path() -> Path:
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return _FEEDBACK_DIR / _FEEDBACK_FILE_NAME


def _load_feedback_store() -> Dict[str, Dict[str, int]]:
    """Load the whole {message_id: {user_id: rating}} map ({} on first use)."""
    p = _feedback_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_feedback_store(store: Dict[str, Dict[str, int]]) -> None:
    """Atomic write — mirrors _save_group (tmp file + os.replace + fsync)."""
    target = _feedback_path()
    content = json.dumps(store, ensure_ascii=False).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _validate_message_id(message_id: str) -> None:
    if not _MESSAGE_ID_RE.match(message_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_MESSAGE_ID", "message": "Invalid message_id format"}},
        )


class FeedbackRequest(BaseModel):
    # 1 = 👍 / -1 = 👎；同一 user 重复同一 rating = toggle 撤销
    rating: Literal[1, -1]
    # auth 未落地前 user_id 走前端传入，默认 anonymous（与 reactions 一致）
    user_id: str = "anonymous"


def _feedback_view(
    store: Dict[str, Dict[str, int]], message_id: str, user_id: str
) -> Dict[str, Any]:
    """Project one message's ratings into the API shape (counts + my_rating)."""
    ratings = store.get(message_id) or {}
    up = sum(1 for r in ratings.values() if r == 1)
    down = sum(1 for r in ratings.values() if r == -1)
    my = ratings.get(user_id)
    return {
        "message_id": message_id,
        "up": up,
        "down": down,
        "my_rating": my if my in (1, -1) else None,
    }


@router.post("/api/messages/{message_id}/feedback")
@limiter.limit("120/minute")
async def post_message_feedback(
    request: Request, message_id: str, body: FeedbackRequest
) -> Dict[str, Any]:
    """Record 👍/👎 feedback on any message (group chat or run-session).

    Toggle semantics: submitting the same rating again withdraws it;
    submitting the opposite rating switches it. One rating per user per
    message.
    """
    _validate_message_id(message_id)
    store = _load_feedback_store()
    ratings: Dict[str, int] = dict(store.get(message_id) or {})
    if ratings.get(body.user_id) == body.rating:
        ratings.pop(body.user_id, None)  # toggle off
    else:
        ratings[body.user_id] = body.rating
    if ratings:
        store[message_id] = ratings
    else:
        store.pop(message_id, None)  # prune empty bucket
    _save_feedback_store(store)
    return _envelope(_feedback_view(store, message_id, body.user_id))


@router.get("/api/messages/{message_id}/feedback")
async def get_message_feedback(
    message_id: str, user_id: str = Query(default="anonymous")
) -> Dict[str, Any]:
    """Read back counts + the caller's own rating (widget hydration)."""
    _validate_message_id(message_id)
    store = _load_feedback_store()
    return _envelope(_feedback_view(store, message_id, user_id))


# ---------------------------------------------------------------------------
# Stream G — thread view endpoint
# ---------------------------------------------------------------------------


class ThreadViewData(BaseModel):
    source_message: MessageItem
    replies: List[MessageItem]


class ThreadViewResponse(BaseModel):
    data: ThreadViewData
    meta: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stream K — group settings (group metadata + per-user preferences)
# ---------------------------------------------------------------------------


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    """Standard response envelope (mirrors teams.py / workspaces.py)."""
    return {"data": data, "meta": meta}


def _group_not_found(group_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "GROUP_NOT_FOUND", "message": f"Group {group_id!r} not found"}},
    )


class PatchGroupRequest(BaseModel):
    """Group-level metadata patch payload.

    All fields are optional — only the provided fields are updated. Empty
    JSON body is a no-op (still bumps `updated_at`). Future expansion can
    add avatar, member_emails, etc.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    announcement: Optional[str] = Field(None, max_length=2000)
    # 归档群聊（群级，非每用户）。True → 从默认群列表隐藏；列表端点用
    # include_archived=true 仍可拉到。
    archived: Optional[bool] = None
    # 2026-06-11 B1（统一会话内核）— 事后补链：把已有群挂到一条 Node
    # conversation 上（如 run 完成后回填）。空串 = 清除链接。
    conversation_id: Optional[str] = None
    # 2026-05-30 — CLI 工作目录(绝对路径)。空字符串 = 清除(回退默认)。
    workspace_dir: Optional[str] = Field(None, max_length=4096)
    # 2026-11-04 — Grok 式「添加成员」：全量成员 id 列表（不增量）。前端读
    # group.agent_ids 后合并/移除再整体回写，避免多端并发丢更新。
    agent_ids: Optional[List[str]] = None
    # 2026-11-04 — 会话形态：'dm' 单聊 | 'group' 群聊。单聊加第二个人后前端
    # 显式下发 'group'（加人即成群，产品上没有独立的「新建群聊」入口）。
    kind: Optional[Literal["dm", "group"]] = None
    # 2026-11-06 — 编队补链（建群即建编队）：自由群打开时前端 find-or-create
    # 同名团队后回填挂链。空串 = 清除链接；非空 = 挂到该团队（与
    # conversation_id 同语义）。编队 tab 的 TeamPanel 依赖 group.team_id。
    team_id: Optional[str] = None


class UserGroupSetting(BaseModel):
    """Per-user-per-group preferences.

    Stored under ``record["user_settings"][user_id]`` in the group's JSON file.
    Until auth lands, ``user_id`` comes from a query parameter and defaults to
    ``"anonymous"`` — see TODO at the bottom of this module.
    """

    muted: bool = False
    pinned: bool = False
    folded: bool = False
    show_nickname: bool = True
    my_nickname: Optional[str] = Field(None, max_length=80)


@router.delete("/api/groups/{group_id}", status_code=204)
async def delete_group(group_id: str) -> None:
    """Delete a group / DM conversation record and its persisted JSON file.

    Returns 204 No Content on success. Returns 404 when the group does not
    exist. This endpoint covers both kind='group' and kind='dm' records so
    the frontend's 'delete DM conversation' feature works correctly.
    """
    p = _group_path(group_id)  # validates group_id format and path traversal
    if not p.exists():
        raise _group_not_found(group_id)
    try:
        p.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "DELETE_FAILED", "message": str(exc)}},
        ) from exc


@router.patch("/api/groups/{group_id}")
async def patch_group(group_id: str, body: PatchGroupRequest) -> Dict[str, Any]:
    """Update group-level metadata (name / announcement).

    Tolerant of legacy records that lack the ``announcement`` field — they
    pick it up on the first PATCH that touches that field.
    """
    record = _load_group(group_id)
    if record is None:
        raise _group_not_found(group_id)

    if body.name is not None:
        record["name"] = body.name
    if body.announcement is not None:
        record["announcement"] = body.announcement
    if body.archived is not None:
        record["archived"] = body.archived
    if body.workspace_dir is not None:
        # 空串 = 清除(回退到 Node 默认 scratch 目录);非空原样存(Node 端再校验)。
        record["workspace_dir"] = body.workspace_dir.strip() or None
    if body.conversation_id is not None:
        # B1 互链：空串 = 清除；非空 = 挂到该 Node conversation（Node 端校验存在性）。
        record["conversation_id"] = body.conversation_id.strip() or None
    if body.agent_ids is not None:
        # Grok 式成员编辑（2026-11-04）：去重保序、剔空串；上限对齐 Node
        # fanout 的 5 人，超了直接 422 而不是静默截断（否则前端以为加进去了，
        # 派发时却被 slice 掉）。
        cleaned: List[str] = []
        for raw in body.agent_ids:
            aid = (raw or "").strip()
            if aid and aid not in cleaned:
                cleaned.append(aid)
        if len(cleaned) > 5:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail="agent_ids 最多 5 个成员")
        record["agent_ids"] = cleaned
    if body.kind is not None:
        record["kind"] = body.kind
    if body.team_id is not None:
        # 2026-11-06 编队补链：空串 = 清除；非空 = 挂到该团队（Node 端团队
        # DAG 调度与前端编队 tab 都按 group.team_id 找编队）。
        record["team_id"] = body.team_id.strip() or None
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_group(record)
    return _envelope(record)


@router.get("/api/groups/{group_id}/user-settings")
async def get_user_group_settings(
    group_id: str,
    user_id: str = Query(default="anonymous", max_length=120),
) -> Dict[str, Any]:
    """Return the requesting user's preferences for this group.

    Defaults to a clean ``UserGroupSetting()`` when the user has never saved
    settings for this group — so the FE always gets a complete shape.
    """
    record = _load_group(group_id)
    if record is None:
        raise _group_not_found(group_id)

    all_settings: Dict[str, Any] = record.get("user_settings", {})
    setting = all_settings.get(user_id, UserGroupSetting().model_dump())
    return _envelope(setting)


@router.put("/api/groups/{group_id}/user-settings")
async def put_user_group_settings(
    group_id: str,
    body: UserGroupSetting,
    user_id: str = Query(default="anonymous", max_length=120),
) -> Dict[str, Any]:
    """Whole-record replace of the requesting user's preferences for this group.

    Matches the simple PUT semantics used by PolicyMatrix / workflow endpoints
    — the FE sends the full shape every time.
    """
    record = _load_group(group_id)
    if record is None:
        raise _group_not_found(group_id)

    all_settings: Dict[str, Any] = record.setdefault("user_settings", {})
    all_settings[user_id] = body.model_dump()
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_group(record)
    return _envelope(body.model_dump())


# TODO(stream-k): swap query-param `user_id` for an authenticated principal
# once auth lands. The persisted user_settings dict is forward-compatible —
# real user_ids just become the new keys; "anonymous" entries from this MVP
# phase can be migrated or dropped during the auth cut-over.


@router.get(
    "/api/groups/{group_id}/messages/{message_id}/thread",
    response_model=ThreadViewResponse,
)
async def get_message_thread(group_id: str, message_id: str) -> ThreadViewResponse:
    """Return the thread view for a message.

    Response shape: ``{ data: { source_message, replies: [...] }, meta: {} }``
    where ``replies`` is every message whose ``reply_to`` equals ``message_id``,
    in chronological order. 404 when no message with that id exists in the
    group's persisted message log.
    """
    rec = _load_group(group_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "GROUP_NOT_FOUND", "message": f"Group {group_id!r} not found"}},
        )

    messages: List[Dict[str, Any]] = rec.get("messages", [])
    source: Optional[Dict[str, Any]] = next(
        (m for m in messages if m.get("message_id") == message_id), None
    )
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "MESSAGE_NOT_FOUND",
                    "message": f"Message {message_id!r} not found in group {group_id!r}",
                }
            },
        )

    replies = [m for m in messages if m.get("reply_to") == message_id]
    # `messages` is already insertion-ordered (chronological); preserve that.

    return ThreadViewResponse(
        data=ThreadViewData(
            source_message=MessageItem(**source),
            replies=[MessageItem(**r) for r in replies],
        ),
        meta={"count": len(replies)},
    )
