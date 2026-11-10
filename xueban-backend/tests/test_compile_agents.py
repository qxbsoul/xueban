"""Story 2.1 — compile_agents() 编译校验测试。"""

from __future__ import annotations

import pytest

from xueban.assembly.compile import CompilationError, compile_agents, parse_agent_specs
from xueban.runtime.contracts import AgentCapabilities, AgentEvent, AgentHandle, AgentTask, AgentSpec
from xueban.runtime.executors import AgentExecutor, ExecutorRegistry


# ---------------------------------------------------------------------------
# Helpers
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


def _raw_template(agents) -> dict:
    return {
        "workflow_id": "test-wf",
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# parse_agent_specs
# ---------------------------------------------------------------------------

class TestParseAgentSpecs:
    def test_empty_agents_list(self):
        specs = parse_agent_specs(_raw_template([]))
        assert specs == []

    def test_single_agent_parsed(self):
        raw = _raw_template([
            {"id": "writer", "executor": {"kind": "api", "provider": "openai"}}
        ])
        specs = parse_agent_specs(raw)
        assert len(specs) == 1
        assert specs[0].id == "writer"
        assert specs[0].kind == "api"
        assert specs[0].provider == "openai"

    def test_no_agents_key_returns_empty(self):
        specs = parse_agent_specs({"workflow_id": "x"})
        assert specs == []

    def test_agents_not_list_raises_compilation_error(self):
        with pytest.raises(CompilationError):
            parse_agent_specs({"agents": "should-be-a-list"})

    def test_non_dict_agent_entry_raises(self):
        with pytest.raises(CompilationError):
            parse_agent_specs({"agents": ["not-a-dict"]})

    def test_soul_and_tools_parsed(self):
        raw = _raw_template([
            {"id": "analyst", "soul": "senior-analyst", "tools": ["search", "calc"],
             "executor": {"kind": "cli", "provider": "claude"}}
        ])
        specs = parse_agent_specs(raw)
        assert specs[0].soul == "senior-analyst"
        assert specs[0].tools == ["search", "calc"]


# ---------------------------------------------------------------------------
# compile_agents
# ---------------------------------------------------------------------------

class TestCompileAgents:
    def test_registered_executor_passes(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        raw = _raw_template([{"id": "writer", "executor": {"kind": "api", "provider": "openai"}}])
        specs = compile_agents(raw, reg)
        assert len(specs) == 1

    def test_unregistered_provider_raises_compilation_error(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        raw = _raw_template([{"id": "writer", "executor": {"kind": "api", "provider": "anthropic"}}])
        with pytest.raises(CompilationError) as exc_info:
            compile_agents(raw, reg)
        assert "anthropic" in str(exc_info.value)

    def test_error_message_shows_available_executors(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        # "api/totally-unknown" is never auto-registered
        raw = _raw_template([{"id": "x", "executor": {"kind": "api", "provider": "totally-unknown"}}])
        with pytest.raises(CompilationError) as exc_info:
            compile_agents(raw, reg)
        # Should mention the available (api, openai) combo
        assert "openai" in str(exc_info.value)

    def test_multiple_agents_all_registered(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        reg.register_agent(_make_executor("cli", "claude"))
        raw = _raw_template([
            {"id": "a1", "executor": {"kind": "api", "provider": "openai"}},
            {"id": "a2", "executor": {"kind": "cli", "provider": "claude"}},
        ])
        specs = compile_agents(raw, reg)
        assert len(specs) == 2

    def test_first_failing_agent_stops_compilation(self):
        reg = ExecutorRegistry()
        reg.register_agent(_make_executor("api", "openai"))
        raw = _raw_template([
            {"id": "bad", "executor": {"kind": "mcp", "provider": "unknown"}},
            {"id": "good", "executor": {"kind": "api", "provider": "openai"}},
        ])
        with pytest.raises(CompilationError) as exc_info:
            compile_agents(raw, reg)
        assert "bad" in str(exc_info.value)

    def test_empty_registry_any_agent_fails(self):
        reg = ExecutorRegistry()
        # "mcp/unregistered-xyz" is never auto-registered by ExecutorRegistry
        raw = _raw_template([{"id": "a", "executor": {"kind": "mcp", "provider": "unregistered-xyz"}}])
        with pytest.raises(CompilationError):
            compile_agents(raw, reg)
