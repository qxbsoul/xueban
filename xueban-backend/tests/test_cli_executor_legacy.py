"""Story 2.2 — 旧 CliExecutor.execute() 回归测试（claude / codex 路径不变）。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from xueban.runtime.executors import CliExecutor, ExecutorRegistry


# ---------------------------------------------------------------------------
# CliExecutor still works as before
# ---------------------------------------------------------------------------

class TestCliExecutorLegacy:
    def test_cli_executor_still_registered_in_registry(self):
        reg = ExecutorRegistry()
        assert "cli" in reg._executors
        assert isinstance(reg._executors["cli"], CliExecutor)

    @pytest.mark.asyncio
    async def test_legacy_execute_via_registry_calls_cli_executor(self):
        reg = ExecutorRegistry()
        completed = MagicMock()
        completed.stdout = json.dumps({"message": "hello"})
        completed.stderr = ""
        completed.returncode = 0
        with patch("asyncio.to_thread", return_value=completed):
            result = await reg.execute(
                config={"kind": "cli", "command": "echo", "parse": "json"},
                payload={"prompt": "test"},
            )
        assert "message" in result

    @pytest.mark.asyncio
    async def test_execute_unknown_kind_still_raises(self):
        reg = ExecutorRegistry()
        with pytest.raises(ValueError, match="unsupported executor kind"):
            await reg.execute({"kind": "unknown"}, {})

    @pytest.mark.asyncio
    async def test_generic_cli_executor_json_parse(self):
        cli = CliExecutor()
        completed = MagicMock()
        completed.stdout = '{"answer": 42}'
        completed.stderr = ""
        completed.returncode = 0
        with patch("asyncio.to_thread", return_value=completed):
            result = await cli.execute(
                config={"kind": "cli", "command": "my-cmd", "parse": "json"},
                payload={"data": "input"},
            )
        assert result.get("answer") == 42

    @pytest.mark.asyncio
    async def test_cli_executor_text_parse(self):
        cli = CliExecutor()
        completed = MagicMock()
        completed.stdout = "plain text output"
        completed.stderr = ""
        completed.returncode = 0
        with patch("asyncio.to_thread", return_value=completed):
            result = await cli.execute(
                config={"kind": "cli", "command": "echo", "parse": "text"},
                payload={},
            )
        assert result["response_text"] == "plain text output"

    @pytest.mark.asyncio
    async def test_cli_executor_nonzero_exit_raises(self):
        cli = CliExecutor()
        completed = MagicMock()
        completed.stdout = ""
        completed.stderr = "fatal error"
        completed.returncode = 2
        with patch("asyncio.to_thread", return_value=completed):
            with pytest.raises(ValueError, match="exit code"):
                await cli.execute(
                    config={"kind": "cli", "command": "fail-cmd"},
                    payload={},
                )


# ---------------------------------------------------------------------------
# ExecutorRegistry auto-registers preset CliAgentExecutors
# ---------------------------------------------------------------------------

class TestAutoRegisteredPresets:
    def test_openclaw_auto_registered(self):
        reg = ExecutorRegistry()
        pairs = reg.list_agent_executors()
        assert ("cli", "openclaw") in pairs

    def test_hermes_auto_registered(self):
        reg = ExecutorRegistry()
        assert ("cli", "hermes") in reg.list_agent_executors()

    def test_shadowsoul_auto_registered(self):
        reg = ExecutorRegistry()
        assert ("cli", "shadowsoul") in reg.list_agent_executors()

    def test_claude_preset_auto_registered(self):
        reg = ExecutorRegistry()
        assert ("cli", "claude") in reg.list_agent_executors()

    def test_codex_preset_auto_registered(self):
        reg = ExecutorRegistry()
        assert ("cli", "codex") in reg.list_agent_executors()
