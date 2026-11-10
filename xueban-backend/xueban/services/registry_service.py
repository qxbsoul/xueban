"""Agent Pack Registry Service — Story 12.5 AC2-AC6.

RegistryService loads pack manifests from `templates/agent-packs/` at startup
and holds them in memory (no per-request disk reads).

Installed-pack records are stored as JSON files per workspace under
`templates/agent-packs/installed/{workspace_id}.json`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

import yaml
from packaging.version import Version, InvalidVersion

from xueban.contracts.agent_manifest import AgentPackManifest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "templates" / "agent-packs"
_INSTALLED_DIR = _PACKS_ROOT / "installed"
_INDEX_FILE = _PACKS_ROOT / "registry-index.yaml"
_WORKSPACE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_workspace_path(workspace_id: str) -> Path:
    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(f"Invalid workspace_id: {workspace_id!r}")
    _INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    resolved = (_INSTALLED_DIR / f"{workspace_id}.json").resolve()
    if _INSTALLED_DIR.resolve() not in resolved.parents:
        raise ValueError(f"Invalid workspace_id: {workspace_id!r}")
    return resolved


def _atomic_write_json(path: Path, data: Any) -> None:
    content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp).unlink()
        except OSError:
            pass
        raise


def _parse_version(v: str) -> Version:
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0.0.0")


# ---------------------------------------------------------------------------
# Index entry
# ---------------------------------------------------------------------------


class PackIndexEntry:
    __slots__ = ("id", "path", "tags")

    def __init__(self, id: str, path: str, tags: List[str]) -> None:
        self.id = id
        self.path = path
        self.tags = tags


# ---------------------------------------------------------------------------
# RegistryService
# ---------------------------------------------------------------------------


class RegistryService:
    """Singleton that caches pack manifests loaded from disk at startup."""

    def __init__(self) -> None:
        self._index: List[PackIndexEntry] = []
        self._manifests: Dict[str, AgentPackManifest] = {}
        self._tags: Dict[str, List[str]] = {}
        self._loaded = False
        # Per-workspace write locks prevent concurrent install() calls from
        # racing on the same installed-packs JSON file (read-modify-write).
        self._ws_locks: Dict[str, threading.Lock] = {}
        self._ws_locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, packs_root: Optional[Path] = None) -> None:
        root = packs_root or _PACKS_ROOT
        index_file = root / "registry-index.yaml"
        if not index_file.exists():
            self._loaded = True
            return

        raw = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
        for entry_raw in raw.get("packs", []):
            pack_id = entry_raw.get("id", "")
            rel_path = entry_raw.get("path", "")
            tags = entry_raw.get("tags", [])
            manifest_file = root / rel_path
            if not manifest_file.exists():
                continue
            try:
                manifest_data = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
                manifest = AgentPackManifest.model_validate(manifest_data)
                self._manifests[pack_id] = manifest
                self._tags[pack_id] = tags
                self._index.append(PackIndexEntry(pack_id, rel_path, tags))
            except Exception as exc:
                _logger.warning(
                    "Skipping pack %r — failed to load/validate manifest at %s: %s",
                    pack_id,
                    manifest_file,
                    exc,
                )
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Pack queries
    # ------------------------------------------------------------------

    def get_manifest(self, pack_id: str) -> Optional[AgentPackManifest]:
        self._ensure_loaded()
        return self._manifests.get(pack_id)

    def list_manifests(
        self,
        tags: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[PackIndexEntry]:
        self._ensure_loaded()
        result = list(self._index)
        if tags:
            wanted = {t.strip() for t in tags.split(",")}
            result = [e for e in result if wanted & set(e.tags)]
        if q:
            q_lower = q.lower()
            result = [
                e for e in result
                if e.id in self._manifests and (
                    q_lower in self._manifests[e.id].name.lower()
                    or q_lower in self._manifests[e.id].description.lower()
                )
            ]
        return result

    # ------------------------------------------------------------------
    # Installed-pack persistence
    # ------------------------------------------------------------------

    def _load_installed(self, workspace_id: str) -> Dict[str, Any]:
        try:
            p = _safe_workspace_path(workspace_id)
        except ValueError:
            return {"workspace_id": workspace_id, "installed": []}
        if not p.exists():
            return {"workspace_id": workspace_id, "installed": []}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"workspace_id": workspace_id, "installed": []}

    def _save_installed(self, workspace_id: str, data: Dict[str, Any]) -> None:
        p = _safe_workspace_path(workspace_id)
        _atomic_write_json(p, data)

    def get_installed_version(self, pack_id: str, workspace_id: str) -> Optional[str]:
        db = self._load_installed(workspace_id)
        for rec in db.get("installed", []):
            if rec.get("pack_id") == pack_id:
                return rec.get("pack_version")
        return None

    def get_installed_record(self, pack_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        db = self._load_installed(workspace_id)
        for rec in db.get("installed", []):
            if rec.get("pack_id") == pack_id:
                return rec
        return None

    def list_installed(self, workspace_id: str) -> List[Dict[str, Any]]:
        db = self._load_installed(workspace_id)
        result = []
        for rec in db.get("installed", []):
            pack_id = rec.get("pack_id", "")
            manifest = self._manifests.get(pack_id)
            registry_version = manifest.version if manifest else rec.get("pack_version", "0.0.0")
            installed_version = rec.get("pack_version", "0.0.0")
            update_available = _parse_version(registry_version) > _parse_version(installed_version)
            result.append({
                **rec,
                "name": manifest.name if manifest else pack_id,
                "update_available": update_available,
                "verified": rec.get("verified", False),
            })
        return result

    def _ws_lock(self, workspace_id: str) -> threading.Lock:
        """Return (creating if needed) the per-workspace write lock."""
        with self._ws_locks_guard:
            if workspace_id not in self._ws_locks:
                self._ws_locks[workspace_id] = threading.Lock()
            return self._ws_locks[workspace_id]

    def upsert_installed(self, workspace_id: str, install_rec: Dict[str, Any]) -> None:
        # Acquire per-workspace lock to serialise concurrent install() calls
        # and prevent the read-modify-write from losing concurrent updates.
        with self._ws_lock(workspace_id):
            db = self._load_installed(workspace_id)
            pack_id = install_rec["pack_id"]
            existing = [r for r in db.get("installed", []) if r.get("pack_id") != pack_id]
            existing.append(install_rec)
            db["installed"] = existing
            self._save_installed(workspace_id, db)

    # ------------------------------------------------------------------
    # Version status
    # ------------------------------------------------------------------

    def install_status(self, pack_id: str, workspace_id: str) -> str:
        manifest = self._manifests.get(pack_id)
        if manifest is None:
            return "not_installed"
        installed_ver = self.get_installed_version(pack_id, workspace_id)
        if installed_ver is None:
            return "not_installed"
        if _parse_version(manifest.version) > _parse_version(installed_ver):
            return "has_update"
        return "installed"

    # ------------------------------------------------------------------
    # Signature verification (AC5)
    # ------------------------------------------------------------------

    def verify_signature(self, manifest: AgentPackManifest) -> bool:
        if manifest.signature is None:
            return True  # unverified pack — allowed but flagged as unverified
        secret_str = os.environ.get("XUEBAN_PACK_SECRET", "")
        if not secret_str:
            # No secret configured: a signed pack CANNOT be verified — treat as
            # invalid rather than silently bypassing (prevents spoofing in envs
            # where the env-var was accidentally omitted).
            return False
        secret = secret_str.encode()
        body = manifest.model_dump(exclude={"signature"})
        canonical = yaml.dump(body, sort_keys=True, allow_unicode=True).encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, manifest.signature.value)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: Optional[RegistryService] = None


def get_registry_service(packs_root: Optional[Path] = None) -> RegistryService:
    global _service
    if _service is None:
        _service = RegistryService()
        _service.load(packs_root)
    return _service


def reset_registry_service() -> None:
    """For tests only — resets the singleton."""
    global _service
    _service = None
