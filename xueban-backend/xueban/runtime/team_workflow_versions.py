"""Team workflow 版本仓 — DAG 编辑器「版本管理」的独立小仓.

每次 PUT /api/teams/{id}/workflow 成功保存时自动追加一份快照；restore 把
指定版本写回 team 记录（并再记一个新版本，语义同「恢复到 vN」而不是抹掉
历史）；branch 在 teams.py 里复用本仓给新变体记下初始版本。

存储先例同 artifact_pins（xueban/runtime/artifact_pins.py）：与 team
本体 `.xueban/teams/{team_id}.json` 分离，落
`.xueban/team_workflow_versions/{team_id}.json`。

Store shape::

    {
      "team_id": "team-xxx",
      "versions": [
        {"version": 1, "saved_at": iso8601, "summary": str | None,
         "workflow": {"nodes": [...], "edges": [...]}},
        ...
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "team_workflow_versions"
_TEAM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _versions_path(team_id: str) -> Path:
    if not _TEAM_ID_RE.match(team_id):
        raise ValueError(f"Invalid team_id: {team_id!r}")
    _VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _VERSIONS_DIR / f"{team_id}.json"


def _load_store(team_id: str) -> Dict[str, Any]:
    p = _versions_path(team_id)
    if not p.exists():
        return {"team_id": team_id, "versions": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"team_id": team_id, "versions": []}
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        return {"team_id": team_id, "versions": []}
    return data


def _save_store(store: Dict[str, Any]) -> None:
    """Atomic write — tmp file + os.replace + fsync（与 artifact_pins 同手法）."""
    target = _versions_path(store["team_id"])
    content = json.dumps(store, ensure_ascii=False, default=str).encode("utf-8")
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


def record_version(
    team_id: str,
    workflow: Dict[str, Any],
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a snapshot of ``workflow``; returns the new version entry."""
    store = _load_store(team_id)
    entry = {
        "version": len(store["versions"]) + 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "workflow": workflow,
    }
    store["versions"].append(entry)
    _save_store(store)
    return entry


def list_versions(team_id: str) -> List[Dict[str, Any]]:
    """All version entries, oldest first ({} store → [])."""
    return list(_load_store(team_id)["versions"])


def get_version(team_id: str, version: int) -> Optional[Dict[str, Any]]:
    """Single version entry by 1-based version number, None when absent."""
    for entry in _load_store(team_id)["versions"]:
        if entry.get("version") == version:
            return entry
    return None


def latest_version(team_id: str) -> Optional[Dict[str, Any]]:
    """Most recent version entry, None when the store is empty."""
    versions = _load_store(team_id)["versions"]
    return versions[-1] if versions else None
