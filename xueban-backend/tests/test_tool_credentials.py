"""tests/test_tool_credentials.py — AC5, AC7: 凭证加解密 + 掩码测试"""
from __future__ import annotations

import os
import pytest
from xueban.runtime.tool_credentials import (
    ToolCredentialError,
    decrypt_env,
    encrypt_env,
    mask_env,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_secret_key(monkeypatch):
    """Every test gets a known key so encryption is deterministic."""
    monkeypatch.setenv("SF_TOOL_SECRET_KEY", "test-key-for-unit-tests-2026")
    yield


# ---------------------------------------------------------------------------
# encrypt / decrypt roundtrip
# ---------------------------------------------------------------------------


def test_roundtrip_empty_dict():
    assert encrypt_env({}) == ""
    assert decrypt_env("") == {}


def test_roundtrip_single_pair():
    env = {"XHS_API_KEY": "secret-value-123"}
    token = encrypt_env(env)
    assert token != ""
    assert "secret-value-123" not in token  # not plaintext
    assert decrypt_env(token) == env


def test_roundtrip_multiple_pairs():
    env = {"KEY_A": "val_a", "KEY_B": "val_b", "KEY_C": "val_c"}
    assert decrypt_env(encrypt_env(env)) == env


def test_different_envs_produce_different_tokens():
    t1 = encrypt_env({"K": "v1"})
    t2 = encrypt_env({"K": "v2"})
    assert t1 != t2


# ---------------------------------------------------------------------------
# mask_env
# ---------------------------------------------------------------------------


def test_mask_env_replaces_all_values():
    env = {"KEY_A": "real-secret", "KEY_B": "another-secret"}
    masked = mask_env(env)
    assert masked == {"KEY_A": "***", "KEY_B": "***"}


def test_mask_env_empty():
    assert mask_env({}) == {}


def test_mask_env_does_not_mutate_input():
    env = {"K": "v"}
    mask_env(env)
    assert env["K"] == "v"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_decrypt_invalid_token_raises():
    with pytest.raises(ToolCredentialError):
        decrypt_env("not-a-valid-fernet-token")


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("SF_TOOL_SECRET_KEY", raising=False)
    with pytest.raises(ToolCredentialError, match="SF_TOOL_SECRET_KEY"):
        encrypt_env({"K": "v"})


def test_missing_key_raises_on_decrypt(monkeypatch):
    # Encrypt with key present, then remove key before decrypting
    token = encrypt_env({"K": "v"})
    monkeypatch.delenv("SF_TOOL_SECRET_KEY", raising=False)
    with pytest.raises(ToolCredentialError):
        decrypt_env(token)


def test_wrong_key_raises(monkeypatch):
    token = encrypt_env({"K": "v"})
    monkeypatch.setenv("SF_TOOL_SECRET_KEY", "completely-different-key")
    with pytest.raises(ToolCredentialError):
        decrypt_env(token)
