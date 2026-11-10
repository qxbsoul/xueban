"""Story 2.1 — ExecutorRegistry (kind, provider) 注册 + 查找测试。"""

from __future__ import annotations

import pytest

from xueban.runtime.contracts import AgentCapabilities, AgentEvent, AgentHandle, AgentTask
from xueban.runtime.executors import AgentExecutor, ExecutorRegistry, UnknownExecutorError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_executor(kind: str, provider: str) -> AgentExecutor:
    class _E(AgentExecutor):
        async def dispatch(self, task):
            return AgentHandle(run_id=task.run_id, node_id=task.node_id, agent_id=task.agent_id)

        async def stream_events(self, handle):
            yield AgentEvent(run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id, type="ok")

        def capabilities(self):
            return AgentCapabilities()

    e = _E.__new__(_E)
    e.kind = kind
    e.provider = provider
    return e


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestExecutorRegistryAgentRegistration:
    def test_register_and_resolve(self):
        reg = ExecutorRegistry()
        exc = _make_executor("api", "openai")
        reg.register_agent(exc)
        found = reg.resolve("api", "openai")
        assert found is exc

    def test_resolve_unknown_kind_raises(self):
        reg = ExecutorRegistry()
        # "api/totally-unknown" is never auto-registered by ExecutorRegistry
        with pytest.raises(UnknownExecutorError) as exc_info:
            reg.resolve("api", "totally-unknown")
        err = exc_info.value
        assert err.kind == "api"
        assert err.provider == "totally-unknown"

    def test_resolve_unknown_provider_raises(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        with pytest.raises(UnknownExecutorError) as exc_info:
            reg.resolve("api", "anthropic")
        err = exc_info.value
        assert err.kind == "api"
        assert err.provider == "anthropic"

    def test_overwrite_registration(self):
        reg = ExecutorRegistry()
        e1 = _make_executor("cli", "claude")
        e2 = _make_executor("cli", "claude")
        reg.register_agent(e1)
        reg.register_agent(e2)
        found = reg.resolve("cli", "claude")
        assert found is e2

    def test_different_kinds_same_provider(self):
        reg = ExecutorRegistry()
        e_api = _make_executor("api", "openai")
        e_cli = _make_executor("cli", "openai")
        reg.register_agent(e_api)
        reg.register_agent(e_cli)
        assert reg.resolve("api", "openai") is e_api
        assert reg.resolve("cli", "openai") is e_cli

    def test_list_agent_executors(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        reg.register_agent(_make_executor("mcp", "hermes"))
        pairs = reg.list_agent_executors()
        assert ("api", "openai") in pairs
        assert ("mcp", "hermes") in pairs

    def test_error_message_lists_available_executors(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        with pytest.raises(UnknownExecutorError) as exc_info:
            reg.resolve("mcp", "unregistered-xyz")
        assert "openai" in str(exc_info.value)

    def test_error_message_for_unknown_acp_provider_mentions_available(self):
        reg = ExecutorRegistry()
        # Registry has preset cli executors auto-registered but no "acp" kind
        with pytest.raises(UnknownExecutorError) as exc_info:
            reg.resolve("acp", "unknown-provider")
        # Error message should mention kind and provider
        msg = str(exc_info.value)
        assert "acp" in msg
        assert "unknown-provider" in msg


# ---------------------------------------------------------------------------
# Legacy execute() still works
# ---------------------------------------------------------------------------

class TestLegacyExecuteUnchanged:
    @pytest.mark.asyncio
    async def test_cli_executor_still_reachable_by_kind(self):
        reg = ExecutorRegistry()
        # Just verify no AttributeError — execution will fail without real CLI binary
        assert reg._executors.get("cli") is not None

    @pytest.mark.asyncio
    async def test_execute_unknown_kind_raises_value_error(self):
        reg = ExecutorRegistry()
        with pytest.raises(ValueError, match="unsupported executor kind"):
            await reg.execute({"kind": "no-such-kind"}, {})
