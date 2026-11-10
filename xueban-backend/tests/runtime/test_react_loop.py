"""Integration tests for Story 11.4 — ReAct loop (AC4 / AC5).

Uses fully mocked LLM + mocked MCP client — no real API keys or MCP server needed.
"""
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from xueban.llm.base import LLMResponse, ProviderType, ToolCall
from xueban.runtime.service import RuntimeService


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockBlueprint:
    """Minimal agent blueprint with a soul."""
    def __init__(self, soul: str = "You are a helpful assistant."):
        self.soul = soul


class MockMCPClient:
    """Mock MCP client returning full tool definitions."""

    def __init__(self, tools: List[Dict[str, Any]], call_results: Dict[str, Any]):
        self._tools = tools  # List[{name, description, inputSchema}]
        self._call_results = call_results  # {tool_name: result}
        self.call_tool_calls: List[Dict[str, Any]] = []

    async def list_tools(self) -> List[Dict[str, Any]]:
        return self._tools

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        self.call_tool_calls.append({"name": name, "args": args})
        return self._call_results.get(name, {})


class MockLLMProvider:
    """Mock LLM that returns predetermined responses per call sequence."""

    def __init__(self, responses: List[LLMResponse]):
        self._responses = list(responses)
        self._call_count = 0
        self.chat_calls: List[Dict[str, Any]] = []

    async def chat(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.chat_calls.append({"messages": list(messages), "tools": tools})
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp


# ---------------------------------------------------------------------------
# AC5 — 2-round ReAct iteration
# ---------------------------------------------------------------------------

class TestReActLoop:

    def _make_service(self) -> RuntimeService:
        return RuntimeService()

    @pytest.mark.asyncio
    async def test_two_round_react_loop(self):
        """AC5: Round 1 → tool call; Round 2 → text result; loop terminates."""
        service = self._make_service()

        # Round 1: LLM requests tool call
        round1_tc = ToolCall(id="tc_001", name="run", args={"command": "pwd"})
        round1_response = LLMResponse(
            content="",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[round1_tc],
        )

        # Round 2: LLM returns final text
        round2_response = LLMResponse(
            content="Your current directory is /home/user",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[],
        )

        mock_llm = MockLLMProvider(responses=[round1_response, round2_response])

        shell_tool_def = {
            "name": "run",
            "description": "Run a shell command",
            "inputSchema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        }
        mock_mcp = MockMCPClient(
            tools=[shell_tool_def],
            call_results={"run": {"stdout": "/home/user", "exit_code": 0}},
        )

        blueprint = MockBlueprint(soul="You are a shell assistant.")
        result = await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="tell me the current directory",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
        )

        # Assertions per AC5
        assert "home/user" in result or "directory" in result.lower()
        assert mock_llm._call_count == 2, "Should make exactly 2 LLM calls"
        assert len(mock_mcp.call_tool_calls) == 1, "Should call MCP tool once"
        assert mock_mcp.call_tool_calls[0]["name"] == "run"

    @pytest.mark.asyncio
    async def test_no_tools_returns_immediately(self):
        """If LLM returns no tool_calls on first call, loop terminates after 1 iteration."""
        service = self._make_service()

        response = LLMResponse(
            content="The answer is 42.",
            model="test-model",
            provider=ProviderType.OPENAI,
            tool_calls=[],
        )
        mock_llm = MockLLMProvider(responses=[response])
        mock_mcp = MockMCPClient(tools=[], call_results={})
        blueprint = MockBlueprint()

        result = await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="what is the answer?",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
        )

        assert result == "The answer is 42."
        assert mock_llm._call_count == 1

    @pytest.mark.asyncio
    async def test_max_iterations_terminates_loop(self):
        """Loop terminates after max_iterations even if LLM keeps requesting tools."""
        service = self._make_service()

        # LLM always returns tool call — should terminate at max_iterations
        infinite_tool_call = ToolCall(id="tc_inf", name="run", args={"command": "ls"})
        infinite_responses = [
            LLMResponse(
                content="",
                model="test-model",
                provider=ProviderType.CLAUDE,
                tool_calls=[infinite_tool_call],
            )
            for _ in range(15)  # more than max_iterations
        ]
        mock_llm = MockLLMProvider(responses=infinite_responses)
        mock_mcp = MockMCPClient(
            tools=[{"name": "run", "description": "run", "inputSchema": {}}],
            call_results={"run": {"stdout": "ok"}},
        )
        blueprint = MockBlueprint()

        result = await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="run ls forever",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
            max_iterations=3,
        )

        # Should stop after 3 iterations
        assert mock_llm._call_count == 3
        # Returns last message content (tool result, not None crash)
        assert result is not None

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_result(self):
        """If LLM calls a tool that no MCP client has, error is injected into messages."""
        service = self._make_service()

        round1 = LLMResponse(
            content="",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[ToolCall(id="tc_ghost", name="ghost_tool", args={})],
        )
        round2 = LLMResponse(
            content="I could not use the tool.",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[],
        )
        mock_llm = MockLLMProvider(responses=[round1, round2])
        mock_mcp = MockMCPClient(tools=[], call_results={})  # empty tool list
        blueprint = MockBlueprint()

        result = await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="use ghost_tool",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
        )

        assert result == "I could not use the tool."
        # Error was injected into message history (Round 2 LLM saw it)
        round2_messages = mock_llm.chat_calls[1]["messages"]
        tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "not found" in tool_msgs[0]["content"].lower()

    @pytest.mark.asyncio
    async def test_sse_events_emitted_on_tool_call(self):
        """SSE bus receives agent.tool_called and agent.tool_result events."""
        service = self._make_service()

        # Wire up mock event bus
        mock_bus = MagicMock()
        mock_bus.publish = MagicMock()
        service._event_bus = mock_bus

        round1 = LLMResponse(
            content="",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[ToolCall(id="tc_sse", name="run", args={"command": "echo hi"})],
        )
        round2 = LLMResponse(
            content="Done.",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[],
        )
        mock_llm = MockLLMProvider(responses=[round1, round2])
        mock_mcp = MockMCPClient(
            tools=[{"name": "run", "description": "run", "inputSchema": {}}],
            call_results={"run": {"stdout": "hi"}},
        )
        blueprint = MockBlueprint()

        await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="echo hi",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
            run_id="run-test-001",
        )

        published_events = [call.args[1] for call in mock_bus.publish.call_args_list]
        event_types = [e["type"] for e in published_events]

        assert "agent.tool_called" in event_types
        assert "agent.tool_result" in event_types
        assert "agent.completed" in event_types

    @pytest.mark.asyncio
    async def test_sse_max_iterations_event(self):
        """SSE bus receives agent.max_iterations_reached when loop is exhausted."""
        service = self._make_service()

        mock_bus = MagicMock()
        mock_bus.publish = MagicMock()
        service._event_bus = mock_bus

        tool_responses = [
            LLMResponse(
                content="",
                model="test-model",
                provider=ProviderType.CLAUDE,
                tool_calls=[ToolCall(id=f"tc_{i}", name="run", args={})],
            )
            for i in range(5)
        ]
        mock_llm = MockLLMProvider(responses=tool_responses)
        mock_mcp = MockMCPClient(
            tools=[{"name": "run", "description": "run", "inputSchema": {}}],
            call_results={"run": {}},
        )
        blueprint = MockBlueprint()

        await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="loop",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
            run_id="run-loop-001",
            max_iterations=2,
        )

        published_types = [c.args[1]["type"] for c in mock_bus.publish.call_args_list]
        assert "agent.max_iterations_reached" in published_types

    @pytest.mark.asyncio
    async def test_messages_include_system_prompt(self):
        """System prompt from agent_blueprint.soul is inserted as first message."""
        service = self._make_service()

        response = LLMResponse(
            content="ok",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[],
        )
        mock_llm = MockLLMProvider(responses=[response])
        mock_mcp = MockMCPClient(tools=[], call_results={})
        blueprint = MockBlueprint(soul="You are a specialist in Python.")

        await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="help me",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
        )

        first_call_messages = mock_llm.chat_calls[0]["messages"]
        system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "You are a specialist in Python."

    @pytest.mark.asyncio
    async def test_tools_passed_to_llm_chat(self):
        """Tool definitions from MCP clients are forwarded to llm.chat(tools=...)."""
        service = self._make_service()

        response = LLMResponse(
            content="done",
            model="test-model",
            provider=ProviderType.CLAUDE,
            tool_calls=[],
        )
        mock_llm = MockLLMProvider(responses=[response])
        tool_def = {"name": "search", "description": "web search", "inputSchema": {}}
        mock_mcp = MockMCPClient(tools=[tool_def], call_results={})
        blueprint = MockBlueprint()

        await service.run_agent_with_tools(
            agent_blueprint=blueprint,
            task="search something",
            mcp_clients=[mock_mcp],
            llm_provider=mock_llm,
        )

        tools_passed = mock_llm.chat_calls[0]["tools"]
        assert tools_passed is not None
        assert any(t["name"] == "search" for t in tools_passed)
