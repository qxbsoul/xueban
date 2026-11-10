"""Story 2.3 — ACP 故障处理测试（subprocess crash → agent.failed + fallback）。"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xueban.runtime.acp.transport import AcpSessionTerminated
from xueban.runtime.contracts import AgentCapabilities, AgentEvent, AgentHandle, AgentTask
from xueban.runtime.executors import AcpAgentExecutor


# ---------------------------------------------------------------------------
# AcpAgentExecutor unit
# ---------------------------------------------------------------------------

class TestAcpAgentExecutorCapabilities:
    def test_capabilities_streaming_true(self):
        exc = AcpAgentExecutor("hermes", ["hermes", "acp"])
        caps = exc.capabilities()
        assert caps.streaming is True
        assert caps.approval_required is True
        # session_resume changed to False (Chunk B review 2026-04-22): stream_events.finally
        # unconditionally stops the transport, so resume isn't actually supported yet.
        assert caps.session_resume is False
        assert caps.tool_calls is True

    def test_kind_is_acp(self):
        exc = AcpAgentExecutor("hermes", ["hermes", "acp"])
        assert exc.kind == "acp"

    def test_provider_stored(self):
        exc = AcpAgentExecutor("shadowsoul", ["shadow", "acp", "serve"])
        assert exc.provider == "shadowsoul"


# ---------------------------------------------------------------------------
# stream_events with no ACP client in handle
# ---------------------------------------------------------------------------

class TestAcpAgentExecutorStreamEvents:
    @pytest.mark.asyncio
    async def test_stream_events_no_session_in_registry_emits_failed(self):
        """Handle without a matching session in executor._sessions → agent.failed.

        (Chunk B review 2026-04-22: live transport/client live in an executor-local
        registry keyed by handle_id, not in AgentHandle.metadata.)
        """
        exc = AcpAgentExecutor("hermes", ["hermes", "acp"])
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"provider": "hermes"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.failed"
        assert "no ACP session" in events[0].payload["error"]

    @pytest.mark.asyncio
    async def test_stream_events_client_crash_emits_failed(self):
        class _CrashClient:
            async def stream_events(self, handle) -> AsyncIterator[AgentEvent]:
                raise AcpSessionTerminated(exit_code=137, stderr_tail="killed by OOM")
                if False:
                    yield

        class _StubTransport:
            async def stop(self):
                pass

        exc = AcpAgentExecutor("hermes", ["hermes", "acp"])
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"provider": "hermes"},
        )
        # Inject (transport, client) into the session registry directly for the test.
        exc._sessions[handle.handle_id] = (_StubTransport(), _CrashClient())
        events = [e async for e in exc.stream_events(handle)]
        # AcpSessionTerminated raised from client is caught in AcpAgentExecutor.stream_events
        assert len(events) == 1
        assert events[0].type == "agent.failed"
        assert events[0].payload["exit_code"] == 137
        # After stream_events finally, the registry entry is cleaned up
        assert handle.handle_id not in exc._sessions

    @pytest.mark.asyncio
    async def test_stream_events_clean_completes(self):
        class _OkClient:
            async def stream_events(self, handle) -> AsyncIterator[AgentEvent]:
                yield AgentEvent(
                    run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                    type="agent.completed", payload={"result": "done"},
                )

        class _StubTransport:
            async def stop(self):
                pass

        exc = AcpAgentExecutor("hermes", ["hermes", "acp"])
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"provider": "hermes"},
        )
        exc._sessions[handle.handle_id] = (_StubTransport(), _OkClient())
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.completed"
        assert handle.handle_id not in exc._sessions


# ---------------------------------------------------------------------------
# AcpSessionTerminated details
# ---------------------------------------------------------------------------

class TestAcpSessionTerminatedDetails:
    def test_none_exit_code(self):
        exc = AcpSessionTerminated(exit_code=None)
        assert exc.exit_code is None
        assert "None" in str(exc)

    def test_nonzero_exit_code(self):
        exc = AcpSessionTerminated(exit_code=1)
        assert exc.exit_code == 1

    def test_stderr_tail_stored(self):
        exc = AcpSessionTerminated(exit_code=2, stderr_tail="error log\ntrace")
        assert "error log" in exc.stderr_tail


# ---------------------------------------------------------------------------
# ExecutorRegistry has ACP executors registered
# ---------------------------------------------------------------------------

class TestAcpExecutorRegistration:
    def test_hermes_acp_auto_registered(self):
        from xueban.runtime.executors import ExecutorRegistry
        reg = ExecutorRegistry()
        pairs = reg.list_agent_executors()
        assert ("acp", "hermes") in pairs

    def test_shadowsoul_acp_auto_registered(self):
        from xueban.runtime.executors import ExecutorRegistry
        reg = ExecutorRegistry()
        assert ("acp", "shadowsoul") in reg.list_agent_executors()
