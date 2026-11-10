"""Tool credential encryption — Story 8.4b (AC2, AC5).

Symmetric Fernet encryption of MCP Provider env dicts.
Encryption key comes from SF_TOOL_SECRET_KEY env var ONLY — never hardcoded.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Dict


class ToolCredentialError(Exception):
    """Raised when credential encryption/decryption fails."""


def _get_fernet():
    """Return a Fernet instance keyed from SF_TOOL_SECRET_KEY."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ToolCredentialError(
            "cryptography package is required; run: pip install cryptography"
        ) from exc

    raw_key = os.environ.get("SF_TOOL_SECRET_KEY", "")
    if not raw_key:
        raise ToolCredentialError(
            "SF_TOOL_SECRET_KEY environment variable must be set for credential encryption"
        )

    # SHA-256 → 32 bytes → URL-safe base64 → valid Fernet key
    derived = hashlib.sha256(raw_key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


def encrypt_env(env: Dict[str, str]) -> str:
    """Encrypt an env dict to a base64 token string. Returns '' for empty dict."""
    if not env:
        return ""
    f = _get_fernet()
    payload = json.dumps(env, ensure_ascii=False).encode()
    return f.encrypt(payload).decode()


def decrypt_env(encrypted: str) -> Dict[str, str]:
    """Decrypt an encrypted token back to the original env dict."""
    if not encrypted:
        return {}
    try:
        f = _get_fernet()
        payload = f.decrypt(encrypted.encode())
        return json.loads(payload)
    except ToolCredentialError:
        raise
    except Exception as exc:
        raise ToolCredentialError(f"Failed to decrypt credentials: {exc}") from exc


def mask_env(env: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of env with all values replaced by '***'."""
    return {k: "***" for k in env}
