"""Chat-to-agent dispatch bridge — Stream C.

Wires user-posted group messages to an LLM-backed agent reply that gets
appended back to the same group's message log. This closes the loop that
used to silently 404 on a non-existent `/api/chat/sessions/{id}/messages`
legacy endpoint.

Design notes
------------
- **Server-side BYOK fallback**: the bridge reads ANTHROPIC_API_KEY /
  OPENAI_API_KEY / DEEPSEEK_API_KEY / ZHIPUAI_API_KEY from the process env
  (Docker/CLI flows). If none are set, the bridge writes a `sender_kind=
  'system'` notice into the group — no silent black-hole.
- **Async fire-and-forget**: dispatch is invoked via FastAPI BackgroundTasks
  so the POST /messages response returns immediately. Any failure is caught
  and surfaced as a `sender_kind='system'` message — the main user-message
  persistence path is never blocked.
- **Single-agent assumption**: multi-agent groups currently only invoke the
  first agent in `group.agent_ids`. Policy-Matrix-driven routing for multi-
  agent fan-out is tracked as a TODO below.
- **Soul-prompt injection**: if the resolved agent has a `soul` field, it is
  prepended as a system message — mirrors the chat-completions endpoint.

History
-------
2026-05-28 — Created to fix the chat-no-reply bug documented in
`memory/bug_quick_hire_dangling_agent.md`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# Cap how many agents reply to a single user message. Multi-agent fan-out runs
# serially (each agent sees prior replies), so a large group would be slow and
# token-expensive without a guard. Excess agents are skipped with a log line.
_MAX_FANOUT_AGENTS = 5

# provider name -> default model, kept in sync with _BYOK_ENV_PREFERENCE and the
# frontend (src/api/chat.ts). Used when the frontend forwards a key but no model.
_PROVIDER_DEFAULT_MODEL: Dict[str, str] = {
    "claude": "claude-3-5-haiku-20241022",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "zhipu": "glm-4-flash",
}

# Ordered preference: try Claude first (best multi-turn), then OpenAI,
# DeepSeek, Zhipu. Each entry maps env var → (ProviderType, default model).
_BYOK_ENV_PREFERENCE: List[Tuple[str, str, str]] = [
    # (env_var_name, provider_name, default_model)
    ("ANTHROPIC_API_KEY", "claude", "claude-3-5-haiku-20241022"),
    ("OPENAI_API_KEY", "openai", "gpt-4o-mini"),
    ("DEEPSEEK_API_KEY", "deepseek", "deepseek-chat"),
    ("ZHIPUAI_API_KEY", "zhipu", "glm-4-flash"),
]

_BYOK_MISSING_NOTICE = (
    "[chat-bridge] No BYOK API key configured on the server. "
    "Set one of: ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / "
    "ZHIPUAI_API_KEY so the agent can reply. "
    "(Frontend BYOK headers are not forwarded to async dispatch yet.)"
)


def _resolve_byok_env() -> Optional[Tuple[str, str, str]]:
    """Return (api_key, provider_name, default_model) for the first env var
    that is populated, or None if no BYOK key is available."""
    for env_name, provider_name, default_model in _BYOK_ENV_PREFERENCE:
        api_key = os.environ.get(env_name)
        if api_key:
            return api_key, provider_name, default_model
    return None


def build_byok_override(
    provider: Optional[str],
    key: Optional[str],
    model: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    """Build a (api_key, provider_name, model) tuple from frontend X-LLM-*
    headers, or None when no key was forwarded.

    Mirrors the BYOK convention used by /api/chat/completions: provider
    defaults to 'zhipu', model falls back to the provider's default.
    """
    if not key:
        return None
    provider_name = (provider or "zhipu").lower()
    resolved_model = model or _PROVIDER_DEFAULT_MODEL.get(provider_name, "glm-4-flash")
    return key, provider_name, resolved_model


def _load_agent_record(agent_id: str) -> Optional[Dict[str, Any]]:
    """Load an agent JSON record by id without going through the registry
    (the registry is for ACP-handshaked agents; quick-hire agents are
    file-only). Returns None if missing or invalid."""
    # Lazy import to avoid circulars
    from xueban.api.agents import _agent_path, _validate_agent_id  # noqa: PLC0415

    try:
        _validate_agent_id(agent_id)
        path = _agent_path(agent_id)
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _build_messages_payload(
    history: List[Dict[str, Any]], soul: str
) -> List[Dict[str, str]]:
    """Convert persisted group messages to an LLM-friendly message list.

    Maps sender_kind → role:
      - 'user'   → 'user'
      - 'agent'  → 'assistant'
      - 'system' → 'system' (skipped — these are bridge notices, not
                   model-relevant context)
    """
    msgs: List[Dict[str, str]] = []
    if soul:
        msgs.append({"role": "system", "content": soul})
    for m in history:
        kind = m.get("sender_kind", "user")
        if kind == "system":
            continue
        role = "assistant" if kind == "agent" else "user"
        content = m.get("content", "")
        if isinstance(content, str) and content:
            msgs.append({"role": role, "content": content})
    return msgs


def _append_message_to_group(group_id: str, msg: Dict[str, Any]) -> bool:
    """Append a single message to a group, atomically, then publish it on the
    group's SSE bus so subscribed browsers see it in real time.

    Reloads the group fresh to avoid clobbering concurrent writes that landed
    since dispatch started. Returns True on success. SSE publish is best-effort
    and never fails the append.
    """
    # Lazy import to avoid circulars (groups imports this module)
    from xueban.api.groups import _load_group, _save_group  # noqa: PLC0415

    rec = _load_group(group_id)
    if rec is None:
        logger.warning("chat-bridge: group %s disappeared mid-dispatch", group_id)
        return False
    messages = rec.get("messages", [])
    messages.append(msg)
    rec["messages"] = messages
    try:
        _save_group(rec)
    except Exception as exc:
        logger.exception("chat-bridge: failed to append message to %s: %s", group_id, exc)
        return False

    # Real-time push (best-effort). Event type from sender_kind so the frontend
    # can route agent bubbles vs. system rows.
    try:
        from xueban.api._group_events import publish_group_event  # noqa: PLC0415

        kind = msg.get("sender_kind", "agent")
        event_type = "system.notice" if kind == "system" else "agent.message"
        publish_group_event(group_id, {"type": event_type, "data": msg})
    except Exception:  # pragma: no cover — SSE push must never break persistence
        logger.debug("chat-bridge: SSE publish failed for %s", group_id, exc_info=True)
    return True


def _make_system_notice(content: str) -> Dict[str, Any]:
    return {
        "sender_name": "system",
        "sender_kind": "system",
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message_id": uuid4().hex,
    }


def _make_agent_message(agent_name: str, content: str) -> Dict[str, Any]:
    return {
        "sender_name": agent_name,
        "sender_kind": "agent",
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message_id": uuid4().hex,
    }


async def _reply_for_one_agent(
    group_id: str,
    agent_id: str,
    api_key: str,
    provider_name: str,
    default_model: str,
) -> None:
    """Produce one agent's reply and append it to the group.

    Reloads the group fresh so that in a fan-out the LLM payload includes
    replies from earlier agents in this same round. Failures are turned into
    `sender_kind='system'` notices and never raised — so one bad agent doesn't
    abort the rest of the fan-out.
    """
    from xueban.api.groups import _load_group  # noqa: PLC0415

    agent_rec = _load_agent_record(agent_id)
    if agent_rec is None:
        _append_message_to_group(
            group_id,
            _make_system_notice(f"[chat-bridge] Agent '{agent_id}' not found in registry."),
        )
        return

    agent_name: str = agent_rec.get("name") or agent_id
    soul: str = agent_rec.get("soul", "") or ""

    # Fresh history so fan-out agents see prior replies from this round.
    rec = _load_group(group_id)
    history: List[Dict[str, Any]] = (rec or {}).get("messages", []) or []
    msgs = _build_messages_payload(history, soul)
    if not msgs or all(m["role"] == "system" for m in msgs):
        _append_message_to_group(
            group_id,
            _make_system_notice("[chat-bridge] No user message to reply to."),
        )
        return

    try:
        from xueban.llm import (  # noqa: PLC0415
            LLMConfig,
            ProviderType,
            create_provider,
        )
    except ImportError as exc:
        _append_message_to_group(
            group_id,
            _make_system_notice(f"[chat-bridge] LLM module unavailable: {exc}"),
        )
        return

    provider_map = {
        "claude": ProviderType.CLAUDE,
        "openai": ProviderType.OPENAI,
        "deepseek": ProviderType.DEEPSEEK,
        "zhipu": ProviderType.ZHIPU,
    }
    ptype = provider_map.get(provider_name)
    if ptype is None:
        _append_message_to_group(
            group_id,
            _make_system_notice(f"[chat-bridge] Unknown provider mapping: {provider_name}"),
        )
        return

    config = LLMConfig(model=default_model, api_key=api_key)
    try:
        provider = create_provider(ptype, config)
        response = await provider.chat(msgs)
    except Exception as exc:
        logger.exception("chat-bridge: LLM call failed for group %s agent %s", group_id, agent_id)
        _append_message_to_group(
            group_id,
            _make_system_notice(f"[chat-bridge] LLM error ({agent_name}): {exc!s}"),
        )
        return

    reply_content = (response.content or "").strip() if response else ""
    if not reply_content:
        _append_message_to_group(
            group_id,
            _make_system_notice(f"[chat-bridge] {agent_name} returned an empty response."),
        )
        return

    _append_message_to_group(group_id, _make_agent_message(agent_name, reply_content))


async def dispatch_agent_reply(
    group_id: str,
    byok_override: Optional[Tuple[str, str, str]] = None,
) -> None:
    """Fire-and-forget background task: every agent in the group replies (in
    order, serially) to the latest user message, each reply appended as a
    `sender_kind='agent'` message and pushed on the group SSE bus.

    BYOK resolution: ``byok_override`` (forwarded from the frontend X-LLM-*
    headers) wins; otherwise fall back to server env keys via
    ``_resolve_byok_env``. When neither is available a system notice is written.

    Fan-out is serial and capped at ``_MAX_FANOUT_AGENTS`` so later agents see
    earlier replies and the round stays bounded. Policy-Matrix-driven routing
    (who replies / in what order) is a future enhancement — TODO below.

    This function MUST NOT raise — any failure is caught and turned into a
    `sender_kind='system'` notice so the user always gets visible feedback.
    """
    # ----- Lazy imports (avoid circulars / heavy import at module load) ----
    try:
        from xueban.api.groups import _load_group  # noqa: PLC0415
    except Exception as exc:
        logger.exception("chat-bridge: import failure: %s", exc)
        return

    try:
        rec = _load_group(group_id)
        if rec is None:
            logger.warning("chat-bridge: group %s not found at dispatch", group_id)
            return

        agent_ids: List[str] = rec.get("agent_ids", []) or []
        if not agent_ids:
            _append_message_to_group(
                group_id,
                _make_system_notice("[chat-bridge] No agents are members of this group."),
            )
            return

        # ----- BYOK lookup (override wins, env fallback) ------------------
        byok = byok_override or _resolve_byok_env()
        if byok is None:
            _append_message_to_group(group_id, _make_system_notice(_BYOK_MISSING_NOTICE))
            return
        api_key, provider_name, default_model = byok

        # TODO(policy-matrix): use rec["policy_matrix"] to decide which agents
        # reply and in what order. MVP: every member replies once, in order.
        roster = agent_ids[:_MAX_FANOUT_AGENTS]
        if len(agent_ids) > _MAX_FANOUT_AGENTS:
            logger.info(
                "chat-bridge: group %s has %d agents; capping fan-out to %d",
                group_id,
                len(agent_ids),
                _MAX_FANOUT_AGENTS,
            )

        for agent_id in roster:
            await _reply_for_one_agent(
                group_id, agent_id, api_key, provider_name, default_model
            )
    except Exception as exc:  # pragma: no cover — defensive last-resort
        logger.exception("chat-bridge: unexpected failure: %s", exc)
        try:
            _append_message_to_group(
                group_id,
                _make_system_notice(f"[chat-bridge] Unexpected failure: {exc!s}"),
            )
        except Exception:
            pass
