"""Story 2.5 — Agent binary health check 测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xueban.runtime.health import (
    HealthResult,
    check_binary,
    check_shadowsoul_binary,
    check_hermes_binary,
    check_openclaw_binary,
    check_all_agents,
    health_results_to_dict,
)


# ---------------------------------------------------------------------------
# check_binary
# ---------------------------------------------------------------------------

class TestCheckBinary:
    def test_binary_not_in_path_returns_not_ok(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None):
            result = check_binary("shadow")
        assert result.ok is False
        assert result.error is not None
        assert "not found" in result.error

    def test_binary_found_calls_version(self):
        with patch("xueban.runtime.health.shutil.which", return_value="/usr/bin/shadow"), \
             patch("xueban.runtime.health.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="shadow 0.1.0\n", stderr="", returncode=0
            )
            result = check_binary("shadow")
        assert result.ok is True
        assert result.path == "/usr/bin/shadow"
        assert result.version == "shadow 0.1.0"

    def test_version_timeout_returns_not_ok(self):
        import subprocess
        with patch("xueban.runtime.health.shutil.which", return_value="/usr/bin/shadow"), \
             patch("xueban.runtime.health.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("shadow", 3)):
            result = check_binary("shadow")
        assert result.ok is False
        assert "timed out" in result.error

    def test_unexpected_exception_returns_not_ok(self):
        with patch("xueban.runtime.health.shutil.which", return_value="/usr/bin/shadow"), \
             patch("xueban.runtime.health.subprocess.run",
                   side_effect=OSError("permission denied")):
            result = check_binary("shadow")
        assert result.ok is False

    def test_binary_name_stored(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None):
            result = check_binary("shadow")
        assert result.binary == "shadow"


# ---------------------------------------------------------------------------
# Named checkers
# ---------------------------------------------------------------------------

class TestNamedCheckers:
    def test_check_shadowsoul_checks_shadow_binary(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None) as mock_which:
            result = check_shadowsoul_binary()
            mock_which.assert_called_with("shadow")
        assert result.ok is False

    def test_check_hermes_checks_hermes_binary(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None) as mock_which:
            result = check_hermes_binary()
            mock_which.assert_called_with("hermes")
        assert result.ok is False

    def test_check_openclaw_checks_openclaw_binary(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None) as mock_which:
            result = check_openclaw_binary()
            mock_which.assert_called_with("openclaw")
        assert result.ok is False


# ---------------------------------------------------------------------------
# check_all_agents
# ---------------------------------------------------------------------------

class TestCheckAllAgents:
    def test_returns_dict_with_all_providers(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None):
            results = check_all_agents()
        assert "shadowsoul" in results
        assert "hermes" in results
        assert "openclaw" in results

    def test_all_unavailable_when_no_binaries_in_path(self):
        with patch("xueban.runtime.health.shutil.which", return_value=None):
            results = check_all_agents()
        assert all(not r.ok for r in results.values())


# ---------------------------------------------------------------------------
# health_results_to_dict
# ---------------------------------------------------------------------------

class TestHealthResultsToDict:
    def test_serializes_ok_false(self):
        results = {"shadowsoul": HealthResult(ok=False, binary="shadow", error="not found")}
        d = health_results_to_dict(results)
        assert d["shadowsoul"]["ok"] is False
        assert d["shadowsoul"]["error"] == "not found"

    def test_serializes_ok_true(self):
        results = {
            "hermes": HealthResult(ok=True, binary="hermes", path="/usr/bin/hermes", version="1.0")
        }
        d = health_results_to_dict(results)
        assert d["hermes"]["ok"] is True
        assert d["hermes"]["path"] == "/usr/bin/hermes"
