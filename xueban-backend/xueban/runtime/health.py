"""Agent binary health checks for Xueban (Story 2.5).

Each external agent (ShadowSoul, Hermes, OpenClaw) has a named binary.
If the binary is absent from PATH, we degrade gracefully rather than hard-crashing.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("xueban.health")

# Map from logical agent provider name → binary name in PATH
_AGENT_BINARIES: Dict[str, str] = {
    "shadowsoul": "shadow",
    "hermes": "hermes",
    "openclaw": "openclaw",
}


@dataclass
class HealthResult:
    """Result of a single agent binary health check."""

    ok: bool
    binary: str
    path: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None


def check_binary(binary: str, version_args: list[str] | None = None) -> HealthResult:
    """Check whether *binary* is in PATH and responds to a version query."""
    resolved = shutil.which(binary)
    if resolved is None:
        return HealthResult(ok=False, binary=binary, error=f"{binary!r} not found in PATH")

    args = version_args if version_args is not None else ["--version"]
    try:
        result = subprocess.run(
            [resolved, *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines = (result.stdout or result.stderr or "").strip().splitlines()
        version = lines[0] if lines else None
        return HealthResult(ok=True, binary=binary, path=resolved, version=version)
    except subprocess.TimeoutExpired:
        return HealthResult(ok=False, binary=binary, path=resolved, error="version check timed out")
    except Exception as exc:
        return HealthResult(ok=False, binary=binary, path=resolved, error=str(exc))


def check_shadowsoul_binary() -> HealthResult:
    return check_binary("shadow")


def check_hermes_binary() -> HealthResult:
    return check_binary("hermes")


def check_openclaw_binary() -> HealthResult:
    return check_binary("openclaw")


def check_all_agents() -> Dict[str, HealthResult]:
    """Return health status for all known external agent binaries."""
    return {
        provider: check_binary(binary)
        for provider, binary in _AGENT_BINARIES.items()
    }


def log_agent_health_warnings(results: Dict[str, HealthResult]) -> None:
    """Emit logger.warning for every unavailable agent binary."""
    for provider, result in results.items():
        if not result.ok:
            logger.warning(
                "Xueban: agent %r unavailable (%s). "
                "Templates using this provider will degrade to fallback executor.",
                provider,
                result.error or "not found",
            )
        else:
            logger.info("Xueban: agent %r OK (path=%s, version=%s)", provider, result.path, result.version)


def health_results_to_dict(results: Dict[str, HealthResult]) -> Dict[str, Any]:
    """Serialize health results to a JSON-safe dict for the /health endpoint."""
    return {
        provider: {
            "ok": r.ok,
            "binary": r.binary,
            "path": r.path,
            "version": r.version,
            "error": r.error,
        }
        for provider, r in results.items()
    }
