"""Story 2.1 — AgentExecutor ABC 契约测试。"""

from __future__ import annotations

import pytest
from abc import ABC

from xueban.runtime.executors import AgentExecutor
from xueban.runtime.contracts import AgentCapabilities, AgentEvent, AgentHandle, AgentTask


# ---------------------------------------------------------------------------
# Minimal concrete implementation
# ---------------------------------------------------------------------------

class _ConcreteExecutor(AgentExecutor):
    kind = "api"
    provider = "test-provider"

    async def dispatch(self, task):
        return AgentHandle(run_id=task.run_id, node_id=task.node_id, agent_id=task.agent_id, status="done")

    async def stream_events(self, handle):
        yield AgentEvent(run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id, type="agent.completed")

    def capabilities(self):
        return AgentCapabilities(streaming=True, tool_calls=True)


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------

class TestAgentExecutorABC:
    def test_cannot_instantiate_abstract_directly(self):
        with pytest.raises(TypeError):
            AgentExecutor()  # type: ignore[abstract]

    def test_missing_dispatch_raises_typeerror(self):
        class _Missing(AgentExecutor):
            kind = "api"
            provider = "x"

            async def stream_events(self, handle):
                yield

            def capabilities(self):
                return AgentCapabilities()

        with pytest.raises(TypeError):
            _Missing()

    def test_missing_stream_events_raises_typeerror(self):
        class _Missing(AgentExecutor):
            kind = "cli"
            provider = "x"

            async def dispatch(self, task):
                pass

            def capabilities(self):
                return AgentCapabilities()

        with pytest.raises(TypeError):
            _Missing()

    def test_missing_capabilities_raises_typeerror(self):
        class _Missing(AgentExecutor):
            kind = "mcp"
            provider = "x"

            async def dispatch(self, task):
                pass

            async def stream_events(self, handle):
                yield

        with pytest.raises(TypeError):
            _Missing()

    def test_concrete_implementation_can_be_instantiated(self):
        executor = _ConcreteExecutor()
        assert executor.kind == "api"
        assert executor.provider == "test-provider"


# ---------------------------------------------------------------------------
# AgentCapabilities
# ---------------------------------------------------------------------------

class TestAgentCapabilities:
    def test_default_all_false(self):
        caps = AgentCapabilities()
        assert caps.streaming is False
        assert caps.approval_required is False
        assert caps.session_resume is False
        assert caps.tool_calls is False

    def test_can_set_fields(self):
        caps = AgentCapabilities(streaming=True, tool_calls=True)
        assert caps.streaming is True
        assert caps.tool_calls is True


# ---------------------------------------------------------------------------
# AgentTask / AgentHandle / AgentEvent
# ---------------------------------------------------------------------------

class TestAgentDataStructures:
    def test_agent_task_auto_id(self):
        task = AgentTask(run_id="r1", node_id="n1", agent_id="a1")
        assert task.task_id.startswith("atask-")

    def test_agent_handle_auto_id(self):
        handle = AgentHandle(run_id="r1", node_id="n1", agent_id="a1")
        assert handle.handle_id.startswith("ahandle-")
        assert handle.status == "pending"

    def test_agent_event_has_ts(self):
        event = AgentEvent(run_id="r1", node_id="n1", agent_id="a1", type="agent.dispatched")
        assert event.ts is not None

    @pytest.mark.asyncio
    async def test_dispatch_returns_handle(self):
        executor = _ConcreteExecutor()
        task = AgentTask(run_id="r1", node_id="n1", agent_id="a1", payload={"x": 1})
        handle = await executor.dispatch(task)
        assert handle.run_id == "r1"
        assert handle.status == "done"

    @pytest.mark.asyncio
    async def test_stream_events_yields_event(self):
        executor = _ConcreteExecutor()
        handle = AgentHandle(run_id="r1", node_id="n1", agent_id="a1")
        events = [e async for e in executor.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.completed"
