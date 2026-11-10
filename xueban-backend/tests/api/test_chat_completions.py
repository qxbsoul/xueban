"""POST /api/chat/completions — BYOK endpoint tests.

Covers:
  - 401 when X-LLM-Key header is missing or empty
  - 400 when provider name is unknown
  - path traversal in agent_id is silently ignored (returns 200)
  - invalid message role → 422 Pydantic validation error
  - happy-path 200 with mocked LLM provider (zhipu)
  - response envelope shape is correct
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuthHeader:
    def test_missing_key_returns_401(self, client: TestClient) -> None:
        r = client.post(
            "/api/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"]["code"] == "NO_API_KEY"

    def test_empty_key_string_returns_401(self, client: TestClient) -> None:
        # FastAPI treats an empty string header as falsy in `if not x_llm_key`
        r = client.post(
            "/api/chat/completions",
            headers={"X-LLM-Key": ""},
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------

class TestProviderValidation:
    def test_unknown_provider_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/chat/completions",
            headers={"X-LLM-Key": "sk-test", "X-LLM-Provider": "nonexistent"},
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "UNKNOWN_PROVIDER"


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

class TestMessageValidation:
    def test_invalid_role_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/chat/completions",
            headers={"X-LLM-Key": "sk-test"},
            json={"messages": [{"role": "hacker", "content": "hi"}]},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Path traversal prevention
# ---------------------------------------------------------------------------

class TestAgentIdSecurity:
    @pytest.mark.parametrize("bad_id", [
        "../etc/passwd",
        "../../secrets",
        "foo/bar",
        "foo\\bar",
        ".hidden",
        "foo bar",
    ])
    def test_malicious_agent_id_is_ignored(
        self, client: TestClient, bad_id: str
    ) -> None:
        """Bad agent_id must not cause a file error — endpoint still returns 200."""
        with _mock_provider("zhipu", "safe"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-test", "X-LLM-Provider": "zhipu"},
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "agent_id": bad_id,
                },
            )
        assert r.status_code == 200, f"bad agent_id={bad_id!r} caused {r.status_code}"

    def test_valid_agent_id_accepted(self, client: TestClient) -> None:
        with _mock_provider("zhipu", "ok"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-test", "X-LLM-Provider": "zhipu"},
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "agent_id": "my-agent_01",
                },
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_200_with_content(self, client: TestClient) -> None:
        with _mock_provider("zhipu", "你好！"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-zhipu", "X-LLM-Provider": "zhipu"},
                json={"messages": [{"role": "user", "content": "你好"}]},
            )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["content"] == "你好！"
        assert data["provider"] == "zhipu"

    def test_envelope_shape(self, client: TestClient) -> None:
        with _mock_provider("zhipu", "world"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-test"},
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
        body = r.json()
        assert set(body.keys()) >= {"data", "meta"}
        assert set(body["data"].keys()) >= {"content", "model", "provider", "tokens_used"}

    def test_system_message_accepted(self, client: TestClient) -> None:
        with _mock_provider("zhipu", "sure"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-test"},
                json={
                    "messages": [
                        {"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "hi"},
                    ]
                },
            )
        assert r.status_code == 200

    def test_assistant_role_accepted(self, client: TestClient) -> None:
        with _mock_provider("openai", "ok"):
            r = client.post(
                "/api/chat/completions",
                headers={"X-LLM-Key": "sk-test", "X-LLM-Provider": "openai"},
                json={
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi there"},
                        {"role": "user", "content": "bye"},
                    ]
                },
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _mock_provider(provider_name: str, response_content: str):
    """Patch xueban.llm so create_provider returns a mock with a fast chat()."""
    mock_resp = MagicMock()
    mock_resp.content = response_content
    mock_resp.model = "mock-model"
    mock_resp.tokens_used = 0

    mock_prov = MagicMock()
    mock_prov.chat = AsyncMock(return_value=mock_resp)

    # The chat endpoint does a lazy `from xueban.llm import ...` inside the
    # function body.  Patching at the module level of xueban.llm is the
    # most reliable intercept point.
    with patch("xueban.llm.create_provider", return_value=mock_prov), \
         patch("xueban.llm.LLMConfig"), \
         patch("xueban.llm.ProviderType"):
        yield mock_prov
