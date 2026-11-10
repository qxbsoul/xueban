"""AgentTeams real-backend bridge tests (design §6).

Verifies the deterministic parts (Team CR rendering, Matrix client-server v3 request
construction) without a cluster or homeserver. Live CR apply + room dispatch remain
env-gated (MATRIX_HOMESERVER) and are exercised end-to-end only with real backends.
"""

from __future__ import annotations

import asyncio

from xueban.runtime.agentteams_bridge import MatrixClient, _FakeAsyncHttp, render_team_cr


# ---------- Team CR rendering ----------


def test_render_team_cr_is_valid_yaml_with_team_kind():
    cr = render_team_cr({
        "provider": "hermes",
        "runtime": "hermes",
        "workers": [{"id": "bob", "runtime": "hermes"}, {"id": "alice", "skills": ["code"]}],
        "skills": ["code", "review"],
    })
    assert "apiVersion: agentteams.alibaba.com/v1" in cr
    assert "kind: Team" in cr
    assert "name: xueban-hermes" in cr
    assert "id: bob" in cr and "id: alice" in cr
    assert "skills: [code, review]" in cr


def test_render_team_cr_handles_empty_workers():
    cr = render_team_cr({"provider": "p", "runtime": "openclaw"})
    assert "workers:\n  []" in cr


# ---------- Matrix client request construction (no network) ----------


def test_matrix_login_create_room_send_message_build_correct_requests():
    fake = _FakeAsyncHttp()
    fake.set_response("POST", "/_matrix/client/v3/login", {"access_token": "tok-123"})
    fake.set_response("POST", "/_matrix/client/v3/createRoom", {"room_id": "!room:hs"})
    fake.set_response("PUT", "m.room.message/1", {"event_id": "$evt:hs"})

    client = MatrixClient("https://hs.local", "xueban", password="pw", http_client=fake)

    token = asyncio.run(client.login())
    assert token == "tok-123"
    assert client.token == "tok-123"

    room_id = asyncio.run(client.create_room(name="xueban-hermes", topic="do the thing"))
    assert room_id == "!room:hs"

    event_id = asyncio.run(client.send_message(room_id, "do the thing"))
    assert event_id == "$evt:hs"

    # Verify the requests hit the right Matrix v3 endpoints with auth + payloads.
    login_call = fake.calls[0]
    assert login_call["url"].endswith("/_matrix/client/v3/login")
    assert login_call["json"]["type"] == "m.login.password"
    assert login_call["json"]["identifier"]["user"] == "xueban"

    room_call = fake.calls[1]
    assert room_call["url"].endswith("/_matrix/client/v3/createRoom")
    assert room_call["headers"]["Authorization"] == "Bearer tok-123"
    assert room_call["json"]["name"] == "xueban-hermes"

    msg_call = fake.calls[2]
    assert "/_matrix/client/v3/rooms/!room:hs/send/m.room.message/1" in msg_call["url"]
    assert msg_call["json"] == {"msgtype": "m.text", "body": "do the thing"}
    assert msg_call["headers"]["Authorization"] == "Bearer tok-123"


def test_matrix_room_events_reads_sync_timeline():
    fake = _FakeAsyncHttp()
    fake.set_response(
        "GET", "/_matrix/client/v3/sync",
        {"rooms": {"join": {"!room:hs": {"timeline": {"events": [{"type": "m.room.message", "content": {"body": "hi"}}]}}}}},
    )
    client = MatrixClient("https://hs.local", "u", token="tok", http_client=fake)
    events = asyncio.run(client.room_events("!room:hs"))
    assert events == [{"type": "m.room.message", "content": {"body": "hi"}}]
