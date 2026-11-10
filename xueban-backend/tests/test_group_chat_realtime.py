"""Tests for the real-time group chat path (2026-05-29):

  - BYOK override forwarded from frontend headers wins over env keys
  - multi-agent fan-out: every member replies in order, later agents see
    earlier replies, a dangling agent is skipped without aborting the round
  - _append_message_to_group publishes onto the per-group SSE bus
  - the _group_events pub/sub primitive (subscribe / publish / unsubscribe)

Data isolation: _GROUPS_DIR (groups) and _AGENTS_DIR (agents) are monkeypatched
to tmp_path so these never touch the real .xueban dir (see
memory/feedback_tests_isolate_data_dir.md).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from xueban.api import _chat_bridge, _group_events
from xueban.api import agents as agents_mod
from xueban.api import groups as groups_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    """Point both JSON stores at tmp_path and reset the SSE bus."""
    g = tmp_path / "groups"
    a = tmp_path / "agents"
    g.mkdir()
    a.mkdir()
    monkeypatch.setattr(groups_mod, "_GROUPS_DIR", g)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", a)
    _group_events.reset_for_tests()
    yield
    _group_events.reset_for_tests()


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    """Strip BYOK env keys so tests control the credential path explicitly."""
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY"):
        monkeypatch.delenv(env, raising=False)


def _make_agent(agent_id: str, name: str, soul: str = "") -> None:
    agents_mod._save_agent(
        {
            "agent_id": agent_id,
            "name": name,
            "soul": soul,
            "status": "idle",
            "source": "quick_hire",
        }
    )


def _make_group(group_id: str, agent_ids: List[str], user_text: str = "hi team") -> None:
    groups_mod._save_group(
        {
            "group_id": group_id,
            "name": "Test Group",
            "agent_ids": agent_ids,
            "messages": [
                {
                    "sender_name": "user",
                    "sender_kind": "user",
                    "content": user_text,
                    "timestamp": "2026-05-29T00:00:00+00:00",
                    "message_id": "u1",
                }
            ],
        }
    )


class _Capture:
    def __init__(self) -> None:
        self.ptypes: List[Any] = []
        self.configs: List[Any] = []
        self.messages: List[List[Dict[str, str]]] = []


@pytest.fixture
def fake_llm(monkeypatch) -> _Capture:
    """Patch xueban.llm.create_provider with a fake that records calls and
    returns a deterministic per-call reply ('reply-0', 'reply-1', ...)."""
    cap = _Capture()
    import xueban.llm as llm_mod

    class _FakeProvider:
        def __init__(self, reply: str) -> None:
            self._reply = reply

        async def chat(self, messages: List[Dict[str, str]]):
            cap.messages.append(list(messages))
            return SimpleNamespace(content=self._reply)

    def fake_create_provider(ptype, config):
        cap.ptypes.append(ptype)
        cap.configs.append(config)
        return _FakeProvider(f"reply-{len(cap.ptypes) - 1}")

    monkeypatch.setattr(llm_mod, "create_provider", fake_create_provider)
    return cap


def _agent_messages(group_id: str) -> List[Dict[str, Any]]:
    rec = groups_mod._load_group(group_id)
    assert rec is not None
    return [m for m in rec["messages"] if m.get("sender_kind") == "agent"]


def _system_messages(group_id: str) -> List[Dict[str, Any]]:
    rec = groups_mod._load_group(group_id)
    assert rec is not None
    return [m for m in rec["messages"] if m.get("sender_kind") == "system"]


# ---------------------------------------------------------------------------
# build_byok_override (unit)
# ---------------------------------------------------------------------------


def test_build_byok_override_none_without_key():
    assert _chat_bridge.build_byok_override("openai", None, None) is None
    assert _chat_bridge.build_byok_override(None, "", None) is None


def test_build_byok_override_defaults_model_by_provider():
    assert _chat_bridge.build_byok_override("claude", "k", None) == (
        "k",
        "claude",
        "claude-3-5-haiku-20241022",
    )
    # explicit model wins; provider defaults to zhipu
    assert _chat_bridge.build_byok_override(None, "k", "glm-4") == ("k", "zhipu", "glm-4")


# ---------------------------------------------------------------------------
# dispatch_agent_reply — BYOK precedence
# ---------------------------------------------------------------------------


def test_byok_override_wins_over_env(monkeypatch, fake_llm):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-claude-key")
    _make_agent("a1", "Alice", soul="be helpful")
    _make_group("g1", ["a1"])

    asyncio.run(
        _chat_bridge.dispatch_agent_reply(
            "g1", byok_override=("override-key", "openai", "gpt-4o-mini")
        )
    )

    assert len(fake_llm.configs) == 1
    assert fake_llm.configs[0].api_key == "override-key"
    replies = _agent_messages("g1")
    assert len(replies) == 1
    assert replies[0]["content"] == "reply-0"
    assert replies[0]["sender_name"] == "Alice"


def test_no_byok_anywhere_writes_system_notice(fake_llm):
    _make_agent("a1", "Alice")
    _make_group("g1", ["a1"])

    asyncio.run(_chat_bridge.dispatch_agent_reply("g1", byok_override=None))

    assert _agent_messages("g1") == []
    notices = _system_messages("g1")
    assert any("BYOK" in n["content"] for n in notices)


# ---------------------------------------------------------------------------
# Multi-agent fan-out
# ---------------------------------------------------------------------------


def test_fanout_all_agents_reply_in_order_seeing_prior(fake_llm):
    _make_agent("a1", "Alice", soul="alice-soul")
    _make_agent("a2", "Bob", soul="bob-soul")
    _make_group("g1", ["a1", "a2"])

    asyncio.run(
        _chat_bridge.dispatch_agent_reply("g1", byok_override=("k", "zhipu", "glm-4-flash"))
    )

    replies = _agent_messages("g1")
    assert [r["sender_name"] for r in replies] == ["Alice", "Bob"]
    assert [r["content"] for r in replies] == ["reply-0", "reply-1"]

    # Bob's payload (2nd LLM call) must include Alice's reply as an assistant turn.
    assert len(fake_llm.messages) == 2
    bob_payload = fake_llm.messages[1]
    assert any(m["role"] == "assistant" and m["content"] == "reply-0" for m in bob_payload)
    # And Bob's own soul, not Alice's, is the system prompt.
    assert bob_payload[0] == {"role": "system", "content": "bob-soul"}


def test_fanout_skips_dangling_agent_but_continues(fake_llm):
    _make_agent("real", "RealOne", soul="s")
    _make_group("g1", ["ghost", "real"])  # ghost has no record

    asyncio.run(
        _chat_bridge.dispatch_agent_reply("g1", byok_override=("k", "zhipu", "glm-4-flash"))
    )

    replies = _agent_messages("g1")
    assert [r["sender_name"] for r in replies] == ["RealOne"]
    notices = _system_messages("g1")
    assert any("ghost" in n["content"] and "not found" in n["content"] for n in notices)


def test_fanout_capped_at_max(monkeypatch, fake_llm):
    monkeypatch.setattr(_chat_bridge, "_MAX_FANOUT_AGENTS", 2)
    for i in range(4):
        _make_agent(f"a{i}", f"Agent{i}", soul="s")
    _make_group("g1", ["a0", "a1", "a2", "a3"])

    asyncio.run(
        _chat_bridge.dispatch_agent_reply("g1", byok_override=("k", "zhipu", "glm-4-flash"))
    )

    assert len(_agent_messages("g1")) == 2  # capped


# ---------------------------------------------------------------------------
# SSE publish on append
# ---------------------------------------------------------------------------


def test_append_publishes_agent_message_event(fake_llm):
    _make_agent("a1", "Alice", soul="s")
    _make_group("g1", ["a1"])

    q = _group_events.subscribe("g1")
    asyncio.run(
        _chat_bridge.dispatch_agent_reply("g1", byok_override=("k", "zhipu", "glm-4-flash"))
    )

    events: List[Dict[str, Any]] = []
    while not q.empty():
        events.append(q.get_nowait())

    agent_events = [e for e in events if e["type"] == "agent.message"]
    assert len(agent_events) == 1
    assert agent_events[0]["data"]["content"] == "reply-0"
    assert agent_events[0]["data"]["sender_kind"] == "agent"


# ---------------------------------------------------------------------------
# _group_events pub/sub primitive
# ---------------------------------------------------------------------------


def test_group_events_subscribe_publish_unsubscribe():
    assert _group_events.subscriber_count("gx") == 0
    q = _group_events.subscribe("gx")
    assert _group_events.subscriber_count("gx") == 1

    _group_events.publish_group_event("gx", {"type": "system.notice", "data": {"content": "hi"}})
    assert q.get_nowait()["data"]["content"] == "hi"

    # other groups don't receive it
    other = _group_events.subscribe("gy")
    _group_events.publish_group_event("gx", {"type": "agent.message", "data": {}})
    assert other.empty()

    _group_events.unsubscribe("gx", q)
    assert _group_events.subscriber_count("gx") == 0
    # publishing to a group with no subscribers is a no-op (no raise)
    _group_events.publish_group_event("gx", {"type": "agent.message", "data": {}})


# ---------------------------------------------------------------------------
# Node-gateway support (2026-05-29): X-SF-No-Dispatch suppress header + SSE
# publish on agent/system writes via the HTTP endpoint.
# ---------------------------------------------------------------------------


def _post_message(group_id: str, **body):
    """POST /api/groups/{id}/messages via TestClient; returns (resp, captured_events)."""
    from fastapi.testclient import TestClient
    from xueban.server import app

    headers = body.pop("_headers", {})
    q = _group_events.subscribe(group_id)
    client = TestClient(app)
    resp = client.post(f"/api/groups/{group_id}/messages", json=body, headers=headers)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    _group_events.unsubscribe(group_id, q)
    return resp, events


def test_user_message_no_dispatch_header_suppresses_python_bridge(monkeypatch, fake_llm):
    """X-SF-No-Dispatch on a user message → Python persists but does NOT auto-reply
    (the Node gateway owns the reply). Without the header the bridge fires."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # bridge would have a key
    _make_agent("a1", "Alice", soul="s")
    _make_group("g1", ["a1"], user_text="seed")

    before = len(groups_mod._load_group("g1")["messages"])
    resp, _ = _post_message(
        "g1", content="hi", sender_name="user", sender_kind="user",
        _headers={"X-SF-No-Dispatch": "1"},
    )
    assert resp.status_code == 201
    # Only the user message was appended; no agent reply (dispatch suppressed).
    msgs = groups_mod._load_group("g1")["messages"]
    assert len(msgs) == before + 1
    assert all(m.get("sender_kind") != "agent" for m in msgs[before:])


def test_agent_message_publishes_sse_via_http(fake_llm):
    """POSTing a sender_kind='agent' message publishes agent.message on the bus
    (this is how the Node gateway's reply reaches the browser)."""
    _make_agent("a1", "Alice", soul="s")
    _make_group("g1", ["a1"])
    resp, events = _post_message(
        "g1", content="reply from node", sender_name="Alice", sender_kind="agent",
    )
    assert resp.status_code == 201
    agent_events = [e for e in events if e["type"] == "agent.message"]
    assert len(agent_events) == 1
    assert agent_events[0]["data"]["content"] == "reply from node"


def test_user_message_does_not_publish_sse(fake_llm):
    """User messages are NOT published (sender shows them optimistically)."""
    _make_agent("a1", "Alice", soul="s")
    _make_group("g1", ["a1"])
    _resp, events = _post_message(
        "g1", content="hi", sender_name="user", sender_kind="user",
        _headers={"X-SF-No-Dispatch": "1"},
    )
    assert [e for e in events if e["type"] == "agent.message"] == []
