"""Tests for Stream G — thread reply API on group chats.

Covers:
  - POST /api/groups/{id}/messages with reply_to persists the field
  - GET /api/groups/{id}/messages/{msg_id}/thread returns source + replies
  - thread view 404 when source message_id does not exist
  - thread view 404 when group does not exist
  - replies are returned in chronological order
  - back-compat: messages without reply_to still serialize cleanly
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def no_byok_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the chat-bridge from auto-posting agent replies during these tests
    — they only care about message persistence + thread queries, not LLM
    dispatch. With no BYOK env the bridge writes a system notice; we tolerate
    that by always querying message lists explicitly.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)


def _create_group(client: TestClient, name: str = "Thread Test Group") -> str:
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


def _post_message(
    client: TestClient,
    group_id: str,
    content: str,
    sender_name: str = "alice",
    sender_kind: str = "user",
    reply_to: str | None = None,
) -> dict:
    body = {
        "content": content,
        "sender_name": sender_name,
        "sender_kind": sender_kind,
    }
    if reply_to is not None:
        body["reply_to"] = reply_to
    res = client.post(f"/api/groups/{group_id}/messages", json=body)
    assert res.status_code == 201, res.text
    return res.json()


class TestReplyToPersistence:
    def test_post_message_returns_message_id_and_null_reply_to(
        self, client: TestClient, no_byok_env
    ):
        gid = _create_group(client, "RT-Persist-1")
        msg = _post_message(client, gid, "hello world")
        # message_id should be present + non-empty
        assert msg.get("message_id"), "message_id should be returned"
        assert len(msg["message_id"]) >= 8
        # No reply_to when omitted
        assert msg.get("reply_to") is None

    def test_post_message_with_reply_to_persists(
        self, client: TestClient, no_byok_env
    ):
        gid = _create_group(client, "RT-Persist-2")
        parent = _post_message(client, gid, "the question")
        parent_id = parent["message_id"]

        child = _post_message(
            client, gid, "the answer", sender_name="bob", reply_to=parent_id
        )
        assert child["reply_to"] == parent_id

        # And it survives a re-GET
        res = client.get(f"/api/groups/{gid}/messages?limit=10")
        assert res.status_code == 200
        msgs = res.json()["messages"]
        child_persisted = next(m for m in msgs if m["content"] == "the answer")
        assert child_persisted["reply_to"] == parent_id
        assert child_persisted["message_id"]  # also persisted


class TestThreadEndpoint:
    def test_thread_returns_source_and_replies_in_order(
        self, client: TestClient, no_byok_env
    ):
        gid = _create_group(client, "Thread-View-1")
        parent = _post_message(client, gid, "source msg")
        pid = parent["message_id"]

        r1 = _post_message(
            client, gid, "reply #1", sender_name="bob", reply_to=pid
        )
        # Post an unrelated message in between — it must NOT appear in the
        # thread view (no reply_to or different reply_to).
        _post_message(client, gid, "unrelated chatter", sender_name="carol")
        r2 = _post_message(
            client, gid, "reply #2", sender_name="dave", reply_to=pid
        )

        res = client.get(f"/api/groups/{gid}/messages/{pid}/thread")
        assert res.status_code == 200, res.text
        payload = res.json()
        assert "data" in payload
        assert "meta" in payload

        data = payload["data"]
        assert data["source_message"]["message_id"] == pid
        assert data["source_message"]["content"] == "source msg"

        replies = data["replies"]
        assert len(replies) == 2
        # Chronological order preserved
        assert replies[0]["message_id"] == r1["message_id"]
        assert replies[1]["message_id"] == r2["message_id"]
        assert payload["meta"]["count"] == 2

    def test_thread_404_when_message_id_missing(
        self, client: TestClient, no_byok_env
    ):
        gid = _create_group(client, "Thread-404-msg")
        _post_message(client, gid, "anchor")
        res = client.get(
            f"/api/groups/{gid}/messages/non-existent-message-id/thread"
        )
        assert res.status_code == 404
        body = res.json()
        # FastAPI's HTTPException wraps detail under `detail`
        assert body["detail"]["error"]["code"] == "MESSAGE_NOT_FOUND"

    def test_thread_404_when_group_missing(
        self, client: TestClient, no_byok_env
    ):
        res = client.get(
            "/api/groups/no-such-group-xyz/messages/whatever/thread"
        )
        assert res.status_code == 404
        body = res.json()
        assert body["detail"]["error"]["code"] == "GROUP_NOT_FOUND"

    def test_thread_with_no_replies_returns_empty_list(
        self, client: TestClient, no_byok_env
    ):
        gid = _create_group(client, "Thread-NoReplies")
        parent = _post_message(client, gid, "alone")
        pid = parent["message_id"]

        res = client.get(f"/api/groups/{gid}/messages/{pid}/thread")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["source_message"]["message_id"] == pid
        assert data["replies"] == []
