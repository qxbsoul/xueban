"""Tests for Stream C chat→agent dispatch bridge.

Covers:
  - dispatch_agent_reply appends an agent message when BYOK env is set and
    LLM responds
  - No BYOK env → system notice (no LLM call)
  - Empty agent_ids → system notice
  - Missing agent record → system notice
  - LLM error → system notice (does NOT raise)
  - Non-user sender_kind on POST → no dispatch scheduled
  - Roles mapped correctly: agent → assistant, user → user, system skipped
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_agent_record(agent_id: str, name: str = "Test Agent", soul: str = "You are helpful.") -> Path:
    """Write an agent JSON record into the project's .xueban/agents/ dir
    and return the path. Tests are responsible for cleanup (use unique ids)."""
    from xueban.api.agents import _agents_dir

    agents_dir = _agents_dir()
    p = agents_dir / f"{agent_id}.json"
    p.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "name": name,
                "soul": soul,
                "workspace_id": "default",
                "blueprint": {},
                "status": "idle",
                "source": "test",
                "created_at": "2026-05-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return p


def _create_group_with_agent(
    client: TestClient, agent_id: str, name: str = "Bridge Test Group"
) -> str:
    res = client.post(
        "/api/groups",
        json={
            "template_id": "",
            "group_template_id": "",
            "name": name,
            "agent_ids": [agent_id],
            "member_emails": [],
            "policy_matrix": {},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["group_id"]


def _create_group_no_agents(client: TestClient, name: str = "Empty Group") -> str:
    res = client.post(
        "/api/groups",
        json={
            "template_id": "",
            "group_template_id": "",
            "name": name,
            "agent_ids": [],
            "member_emails": [],
            "policy_matrix": {},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["group_id"]


def _get_messages(client: TestClient, group_id: str) -> List[Dict[str, Any]]:
    res = client.get(f"/api/groups/{group_id}/messages?limit=200")
    assert res.status_code == 200, res.text
    return res.json()["messages"]


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeProvider:
    """Stand-in LLM provider that records the messages it was called with."""

    last_messages: List[Dict[str, str]] = []
    last_instance: "_FakeProvider | None" = None

    def __init__(self, reply: str = "Hello from fake LLM"):
        self.reply = reply
        _FakeProvider.last_instance = self

    async def chat(self, messages: List[Dict[str, str]]) -> _FakeResponse:
        _FakeProvider.last_messages = list(messages)
        return _FakeResponse(self.reply)


@pytest.fixture
def fake_byok_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a single BYOK env var so _resolve_byok_env picks it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-sk-fake")


@pytest.fixture
def no_byok_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)


@pytest.fixture
def patched_llm(monkeypatch: pytest.MonkeyPatch):
    """Patch xueban.llm.create_provider so the bridge calls our fake."""
    from xueban import llm as llm_mod

    def fake_create_provider(_ptype, _config):
        return _FakeProvider()

    monkeypatch.setattr(llm_mod, "create_provider", fake_create_provider)
    return _FakeProvider


# ---------------------------------------------------------------------------
# Direct-dispatch tests — invoke dispatch_agent_reply() ourselves so we
# don't depend on FastAPI BackgroundTasks timing semantics in TestClient.
# ---------------------------------------------------------------------------


class TestDispatchHappyPath:
    def test_appends_agent_message_when_byok_set(
        self,
        client: TestClient,
        fake_byok_env,
        patched_llm,
    ):
        agent_id = "test-bridge-happy-agent"
        _write_agent_record(agent_id, name="HappyBot", soul="Be terse.")
        group_id = _create_group_with_agent(client, agent_id, name="Happy Group")

        # Post a user message first (so history exists)
        res = client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "Hi agent", "sender_name": "alice", "sender_kind": "user"},
        )
        assert res.status_code == 201

        # Now run the dispatch directly (BackgroundTasks would have triggered
        # this; calling it ourselves makes the test deterministic).
        from xueban.api._chat_bridge import dispatch_agent_reply

        asyncio.run(dispatch_agent_reply(group_id))

        msgs = _get_messages(client, group_id)
        # Last message must be the agent reply
        assert msgs[-1]["sender_kind"] == "agent"
        assert msgs[-1]["sender_name"] == "HappyBot"
        assert "Hello from fake LLM" in msgs[-1]["content"]

        # Verify soul was used as system prompt
        sent = _FakeProvider.last_messages
        assert sent[0]["role"] == "system"
        assert "Be terse." in sent[0]["content"]
        # User message present
        assert any(m["role"] == "user" and m["content"] == "Hi agent" for m in sent)


class TestDispatchFailureModes:
    def test_no_byok_env_writes_system_notice(
        self,
        client: TestClient,
        no_byok_env,
    ):
        agent_id = "test-bridge-no-byok-agent"
        _write_agent_record(agent_id)
        group_id = _create_group_with_agent(client, agent_id, name="NoBYOK Group")
        client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "hello", "sender_name": "alice", "sender_kind": "user"},
        )

        from xueban.api._chat_bridge import dispatch_agent_reply

        asyncio.run(dispatch_agent_reply(group_id))

        msgs = _get_messages(client, group_id)
        # Last message is a system notice mentioning BYOK
        assert msgs[-1]["sender_kind"] == "system"
        assert "BYOK" in msgs[-1]["content"]

    def test_no_agents_writes_system_notice(
        self,
        client: TestClient,
        fake_byok_env,
    ):
        group_id = _create_group_no_agents(client, name="No-Agents Group")
        client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "hello", "sender_name": "alice", "sender_kind": "user"},
        )

        from xueban.api._chat_bridge import dispatch_agent_reply

        asyncio.run(dispatch_agent_reply(group_id))

        msgs = _get_messages(client, group_id)
        assert msgs[-1]["sender_kind"] == "system"
        assert "No agents" in msgs[-1]["content"]

    def test_missing_agent_record_writes_system_notice(
        self,
        client: TestClient,
        fake_byok_env,
    ):
        # Reference an agent_id that has no file on disk
        group_id = _create_group_with_agent(
            client, "test-bridge-missing-agent-xyz", name="Ghost Group"
        )
        client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "hello", "sender_name": "alice", "sender_kind": "user"},
        )

        from xueban.api._chat_bridge import dispatch_agent_reply

        asyncio.run(dispatch_agent_reply(group_id))

        msgs = _get_messages(client, group_id)
        assert msgs[-1]["sender_kind"] == "system"
        assert "not found" in msgs[-1]["content"].lower()

    def test_llm_error_writes_system_notice_does_not_raise(
        self,
        client: TestClient,
        fake_byok_env,
        monkeypatch: pytest.MonkeyPatch,
    ):
        agent_id = "test-bridge-llm-err-agent"
        _write_agent_record(agent_id)
        group_id = _create_group_with_agent(client, agent_id, name="ErrGroup")
        client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "hello", "sender_name": "alice", "sender_kind": "user"},
        )

        from xueban import llm as llm_mod

        class _BoomProvider:
            async def chat(self, _msgs):
                raise RuntimeError("upstream 500")

        monkeypatch.setattr(llm_mod, "create_provider", lambda _p, _c: _BoomProvider())

        from xueban.api._chat_bridge import dispatch_agent_reply

        # Must NOT raise
        asyncio.run(dispatch_agent_reply(group_id))

        msgs = _get_messages(client, group_id)
        assert msgs[-1]["sender_kind"] == "system"
        assert "LLM error" in msgs[-1]["content"]
        assert "upstream 500" in msgs[-1]["content"]


# ---------------------------------------------------------------------------
# Integration tests — go through the HTTP layer and exercise BackgroundTasks
# ---------------------------------------------------------------------------


class TestHttpIntegration:
    def test_agent_sender_kind_does_not_trigger_dispatch(
        self,
        client: TestClient,
        fake_byok_env,
        patched_llm,
    ):
        """Posting a message with sender_kind='agent' must NOT cause another
        dispatch — otherwise we'd loop forever. We assert by comparing the
        number of messages right after the post: there should be exactly one
        new message (the one we just posted), not two."""
        agent_id = "test-bridge-no-loop-agent"
        _write_agent_record(agent_id)
        group_id = _create_group_with_agent(client, agent_id, name="NoLoop")

        before = _get_messages(client, group_id)
        res = client.post(
            f"/api/groups/{group_id}/messages",
            json={
                "content": "ignore-me",
                "sender_name": "HappyBot",
                "sender_kind": "agent",
            },
        )
        assert res.status_code == 201
        after = _get_messages(client, group_id)
        # TestClient flushes BackgroundTasks before returning, so if dispatch
        # had fired, after would already contain an extra agent reply.
        assert len(after) == len(before) + 1
        assert after[-1]["content"] == "ignore-me"

    def test_user_post_triggers_background_dispatch(
        self,
        client: TestClient,
        fake_byok_env,
        patched_llm,
    ):
        """End-to-end: POST a user message → TestClient runs BackgroundTasks
        → agent reply is persisted before the next GET."""
        agent_id = "test-bridge-http-agent"
        _write_agent_record(agent_id, name="HTTPBot")
        group_id = _create_group_with_agent(client, agent_id, name="HTTPGroup")

        res = client.post(
            f"/api/groups/{group_id}/messages",
            json={"content": "ping", "sender_name": "alice", "sender_kind": "user"},
        )
        assert res.status_code == 201

        msgs = _get_messages(client, group_id)
        # Expect: [user 'ping', agent reply]
        assert len(msgs) >= 2
        assert msgs[0]["sender_kind"] == "user"
        assert msgs[-1]["sender_kind"] == "agent"
        assert msgs[-1]["sender_name"] == "HTTPBot"


# ---------------------------------------------------------------------------
# Unit tests — _build_messages_payload role mapping
# ---------------------------------------------------------------------------


class TestRoleMapping:
    def test_role_mapping_skips_system_and_maps_agent_to_assistant(self):
        from xueban.api._chat_bridge import _build_messages_payload

        history = [
            {"sender_kind": "user", "content": "Q1"},
            {"sender_kind": "agent", "content": "A1"},
            {"sender_kind": "system", "content": "[bridge] notice"},
            {"sender_kind": "user", "content": "Q2"},
        ]
        msgs = _build_messages_payload(history, soul="be terse")

        # First is system soul
        assert msgs[0] == {"role": "system", "content": "be terse"}
        # System notices are skipped
        assert not any(
            m["role"] == "system" and "notice" in m["content"] for m in msgs
        )
        # agent → assistant
        roles = [m["role"] for m in msgs[1:]]
        assert roles == ["user", "assistant", "user"]

    def test_role_mapping_with_no_soul_omits_system_message(self):
        from xueban.api._chat_bridge import _build_messages_payload

        history = [{"sender_kind": "user", "content": "hello"}]
        msgs = _build_messages_payload(history, soul="")
        assert msgs == [{"role": "user", "content": "hello"}]
