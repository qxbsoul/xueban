"""定时任务本地执行凭据加密；明文只存在于当前请求与执行调用内。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

_KEY_PATH = Path(__file__).resolve().parents[2] / ".xueban" / ".schedule-secret-key"


def _fernet() -> Fernet:
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _KEY_PATH.exists():
        _KEY_PATH.write_bytes(Fernet.generate_key())
        try:
            os.chmod(_KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(_KEY_PATH.read_bytes().strip())


def encrypt_schedule_secrets(value: Dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decrypt_schedule_secrets(value: str) -> Dict[str, Any]:
    try:
        result = json.loads(_fernet().decrypt(value.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("定时任务执行凭据无法解密，请编辑并重新保存任务") from exc
    return result if isinstance(result, dict) else {}
