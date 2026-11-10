"""Artifact pin store — 产物 filecard 置顶状态的独立小仓.

artifact 本体随 RunResult.artifacts 持久化（run 域），没有独立存储；pin 是
纯展示态，落 `.xueban/artifacts/artifact_pins.json` —— 与 message
feedback 的 `.xueban/feedback/` 同一先例（xueban/api/groups.py）。

Store shape: ``{artifact_id: pinned_at_iso8601}``。toggle 语义：已置顶再
POST 一次即撤销（整条键删除）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set

_PINS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "artifacts"
_PINS_FILE_NAME = "artifact_pins.json"


def _pins_path() -> Path:
    _PINS_DIR.mkdir(parents=True, exist_ok=True)
    return _PINS_DIR / _PINS_FILE_NAME


def load_pins() -> Dict[str, str]:
    """Load the whole {artifact_id: pinned_at} map ({} on first use)."""
    p = _pins_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_pins(store: Dict[str, str]) -> None:
    """Atomic write — tmp file + os.replace + fsync（与 groups._save_feedback_store 同手法）."""
    target = _pins_path()
    content = json.dumps(store, ensure_ascii=False).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def toggle_pin(artifact_id: str) -> Optional[str]:
    """Toggle one artifact's pin; returns pinned_at when now pinned, else None."""
    store = load_pins()
    if artifact_id in store:
        del store[artifact_id]
        _save_pins(store)
        return None
    pinned_at = datetime.now(timezone.utc).isoformat()
    store[artifact_id] = pinned_at
    _save_pins(store)
    return pinned_at


def pinned_ids() -> Set[str]:
    """All currently pinned artifact ids (projection hydration)."""
    return set(load_pins())
