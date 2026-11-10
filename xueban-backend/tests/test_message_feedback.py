"""Tests for L1 gap — message 👍/👎 feedback endpoints (2026-08-22).

Covers POST /api/messages/{mid}/feedback (toggle per user) and
GET  /api/messages/{mid}/feedback (counts + my_rating hydration).

Storage isolation: monkeypatch ``groups._FEEDBACK_DIR`` to tmp_path so tests
never touch the real ``.xueban/feedback/`` directory (same discipline as
test_groups_reactions.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.api import groups


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(groups, "_FEEDBACK_DIR", tmp_path / "feedback")
    return TestClient(app)


class TestPostFeedback:
    def test_set_up_rating(self, client: TestClient):
        r = client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "u1"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data == {"message_id": "m1", "up": 1, "down": 0, "my_rating": 1}

    def test_same_rating_toggles_off(self, client: TestClient):
        client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "u1"})
        r = client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "u1"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["up"] == 0 and data["my_rating"] is None

    def test_opposite_rating_switches(self, client: TestClient):
        client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "u1"})
        r = client.post("/api/messages/m1/feedback", json={"rating": -1, "user_id": "u1"})
        data = r.json()["data"]
        assert data["up"] == 0 and data["down"] == 1 and data["my_rating"] == -1

    def test_distinct_users_accumulate(self, client: TestClient):
        client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "a"})
        r = client.post("/api/messages/m1/feedback", json={"rating": 1, "user_id": "b"})
        assert r.json()["data"]["up"] == 2

    def test_default_user_is_anonymous(self, client: TestClient):
        r = client.post("/api/messages/m1/feedback", json={"rating": 1})
        assert r.json()["data"]["my_rating"] == 1
        g = client.get("/api/messages/m1/feedback")
        assert g.json()["data"]["my_rating"] == 1  # 默认 anonymous 读到同一票

    def test_invalid_rating_rejected(self, client: TestClient):
        r = client.post("/api/messages/m1/feedback", json={"rating": 0, "user_id": "u1"})
        assert r.status_code == 422

    def test_invalid_message_id_rejected(self, client: TestClient):
        r = client.post("/api/messages/bad%20id/feedback", json={"rating": 1})
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "INVALID_MESSAGE_ID"


class TestGetFeedback:
    def test_unknown_message_is_zeroed(self, client: TestClient):
        r = client.get("/api/messages/nope/feedback")
        assert r.status_code == 200
        assert r.json()["data"] == {"message_id": "nope", "up": 0, "down": 0, "my_rating": None}

    def test_counts_and_my_rating_per_user(self, client: TestClient):
        client.post("/api/messages/m2/feedback", json={"rating": 1, "user_id": "a"})
        client.post("/api/messages/m2/feedback", json={"rating": -1, "user_id": "b"})
        r = client.get("/api/messages/m2/feedback", params={"user_id": "b"})
        data = r.json()["data"]
        assert data["up"] == 1 and data["down"] == 1 and data["my_rating"] == -1
        # 旁观者视角：计数相同，my_rating 为空
        r2 = client.get("/api/messages/m2/feedback", params={"user_id": "c"})
        assert r2.json()["data"]["my_rating"] is None

    def test_persists_across_reads(self, client: TestClient, tmp_path):
        client.post("/api/messages/m3/feedback", json={"rating": 1, "user_id": "a"})
        # 直接读落盘文件确认持久化真实发生
        import json as _json

        stored = _json.loads((tmp_path / "feedback" / "message_feedback.json").read_text("utf-8"))
        assert stored == {"m3": {"a": 1}}
