"""Unit tests for Story 11.4 — LLM tool_use wiring (AC1 / AC2 / AC3).

All tests use fully mocked LLM backends — no real API keys required.
"""
import pytest
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List, Optional

from xueban.llm.base import LLMConfig, LLMResponse, LLMProvider, ProviderType, ToolCall


# ---------------------------------------------------------------------------
# AC1 — ToolCall dataclass + LLMResponse.tool_calls backward compat
# ---------------------------------------------------------------------------

class TestToolCallDataclass:
    def test_tool_call_fields(self):
        tc = ToolCall(id="tc1", name="run", args={"command": "pwd"})
        assert tc.id == "tc1"
        assert tc.name == "run"
        assert tc.args == {"command": "pwd"}

    def test_tool_call_to_dict(self):
        tc = ToolCall(id="tc1", name="run", args={"command": "pwd"})
        d = tc.to_dict()
        assert d == {"id": "tc1", "name": "run", "args": {"command": "pwd"}}


class TestLLMResponseBackwardCompat:
    def test_default_tool_calls_empty_list(self):
        resp = LLMResponse(content="hello", model="m", provider=ProviderType.CLAUDE)
        assert resp.tool_calls == []

    def test_to_dict_includes_tool_calls(self):
        tc = ToolCall(id="x", name="search", args={"q": "test"})
        resp = LLMResponse(
            content="ok",
            model="claude-sonnet-4-6",
            provider=ProviderType.CLAUDE,
            tool_calls=[tc],
        )
        d = resp.to_dict()
        assert "tool_calls" in d
        assert d["tool_calls"] == [{"id": "x", "name": "search", "args": {"q": "test"}}]

    def test_to_dict_no_tools_empty_list(self):
        resp = LLMResponse(content="hi", model="m", provider=ProviderType.OPENAI)
        assert resp.to_dict()["tool_calls"] == []

    def test_existing_fields_unchanged(self):
        resp = LLMResponse(
            content="hello",
            model="gpt-4o",
            provider=ProviderType.OPENAI,
            tokens_used=42,
            finish_reason="stop",
            metadata={"id": "abc"},
        )
        d = resp.to_dict()
        assert d["content"] == "hello"
        assert d["model"] == "gpt-4o"
        assert d["tokens_used"] == 42
        assert d["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# AC2 — ClaudeProvider.chat() with tools
# ---------------------------------------------------------------------------

class TestClaudeProviderToolUse:
    """Tests use mocked anthropic.AsyncAnthropic — no API key needed."""

    def _make_provider(self):
        from xueban.llm.claude import ClaudeProvider
        config = LLMConfig(model="claude-sonnet-4-6", api_key="test-key")
        with patch("xueban.llm.claude.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = MagicMock()
            mock_anthropic.APIError = Exception
            provider = ClaudeProvider.__new__(ClaudeProvider)
            provider.config = config
            provider._provider_type = ProviderType.CLAUDE
            provider.client = MagicMock()
        return provider

    @pytest.mark.asyncio
    async def test_chat_without_tools_returns_text_only(self):
        from xueban.llm.claude import ClaudeProvider
        config = LLMConfig(model="claude-sonnet-4-6", api_key="test-key")
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.config = config
        provider._provider_type = ProviderType.CLAUDE

        # Mock response: pure text, no tool_use blocks
        mock_block = MagicMock()
        mock_block.text = "Hello there"
        mock_block.type = "text"

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.model = "claude-sonnet-4-6"
        mock_response.stop_reason = "end_turn"
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.id = "msg_001"

        provider.client = MagicMock()
        provider.client.messages.create = AsyncMock(return_value=mock_response)

        resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.content == "Hello there"
        assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_chat_with_tools_sends_tools_param(self):
        from xueban.llm.claude import ClaudeProvider
        config = LLMConfig(model="claude-sonnet-4-6", api_key="test-key")
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.config = config
        provider._provider_type = ProviderType.CLAUDE

        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.id = "toolu_01"
        mock_tool_block.name = "run"
        mock_tool_block.input = {"command": "pwd"}
        if hasattr(mock_tool_block, "text"):
            del mock_tool_block.text

        mock_response = MagicMock()
        mock_response.content = [mock_tool_block]
        mock_response.model = "claude-sonnet-4-6"
        mock_response.stop_reason = "tool_use"
        mock_response.usage.input_tokens = 20
        mock_response.usage.output_tokens = 10
        mock_response.id = "msg_002"

        provider.client = MagicMock()
        provider.client.messages.create = AsyncMock(return_value=mock_response)

        tools = [{"name": "run", "description": "Run shell command", "inputSchema": {
            "type": "object", "properties": {"command": {"type": "string"}}
        }}]
        resp = await provider.chat(
            [{"role": "user", "content": "what dir am i in?"}],
            tools=tools,
        )

        # Verify tools were forwarded to the API
        call_kwargs = provider.client.messages.create.call_args.kwargs
        assert "tools" in call_kwargs
        api_tools = call_kwargs["tools"]
        assert len(api_tools) == 1
        assert api_tools[0]["name"] == "run"
        assert "input_schema" in api_tools[0]

        # Verify tool_calls populated
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "toolu_01"
        assert resp.tool_calls[0].name == "run"
        assert resp.tool_calls[0].args == {"command": "pwd"}

    @pytest.mark.asyncio
    async def test_chat_mcp_input_schema_key_remapped(self):
        """MCP uses 'inputSchema'; Claude API wants 'input_schema'."""
        from xueban.llm.claude import ClaudeProvider
        config = LLMConfig(model="claude-sonnet-4-6", api_key="test-key")
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.config = config
        provider._provider_type = ProviderType.CLAUDE

        mock_response = MagicMock()
        mock_response.content = []
        mock_response.model = "claude-sonnet-4-6"
        mock_response.stop_reason = "end_turn"
        mock_response.usage.input_tokens = 5
        mock_response.usage.output_tokens = 5
        mock_response.id = "msg_003"

        provider.client = MagicMock()
        provider.client.messages.create = AsyncMock(return_value=mock_response)

        mcp_tools = [{"name": "search", "description": "web search", "inputSchema": {"type": "object"}}]
        await provider.chat([{"role": "user", "content": "search it"}], tools=mcp_tools)

        call_kwargs = provider.client.messages.create.call_args.kwargs
        assert call_kwargs["tools"][0]["input_schema"] == {"type": "object"}
        assert "inputSchema" not in call_kwargs["tools"][0]


# ---------------------------------------------------------------------------
# AC3 — OpenAIProvider.chat() with tools
# ---------------------------------------------------------------------------

class TestOpenAIProviderToolUse:
    """Tests use mocked openai.AsyncOpenAI — no API key needed."""

    def _make_provider(self):
        from xueban.llm.openai import OpenAIProvider
        config = LLMConfig(model="gpt-4o", api_key="test-key")
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.config = config
        provider._provider_type = ProviderType.OPENAI
        provider.client = MagicMock()
        return provider

    @pytest.mark.asyncio
    async def test_chat_without_tools_no_tool_calls(self):
        provider = self._make_provider()

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage.total_tokens = 15
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.id = "chatcmpl_001"

        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        resp = await provider.chat([{"role": "user", "content": "hi"}])
        assert resp.content == "Hello"
        assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_chat_with_tools_sends_openai_format(self):
        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_abc"
        mock_tc.function.name = "run"
        mock_tc.function.arguments = '{"command":"pwd"}'

        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage.total_tokens = 30
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 10
        mock_response.id = "chatcmpl_002"

        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        tools = [{"name": "run", "description": "Run shell", "inputSchema": {
            "type": "object", "properties": {"command": {"type": "string"}}
        }}]
        resp = await provider.chat([{"role": "user", "content": "pwd?"}], tools=tools)

        # Verify OpenAI format
        call_kwargs = provider.client.chat.completions.create.call_args.kwargs
        assert "tools" in call_kwargs
        assert call_kwargs["tools"][0]["type"] == "function"
        assert call_kwargs["tools"][0]["function"]["name"] == "run"

        # Verify tool_calls populated with parsed JSON
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_abc"
        assert resp.tool_calls[0].name == "run"
        assert resp.tool_calls[0].args == {"command": "pwd"}

    @pytest.mark.asyncio
    async def test_chat_with_tools_json_args_parsed(self):
        """Tool args as JSON string are parsed into dict."""
        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_x"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = '{"query": "xueban"}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage.total_tokens = 10
        mock_response.usage.prompt_tokens = 8
        mock_response.usage.completion_tokens = 2
        mock_response.id = "chatcmpl_003"

        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        tools = [{"name": "search", "description": "web", "inputSchema": {}}]
        resp = await provider.chat([{"role": "user", "content": "search"}], tools=tools)

        assert resp.tool_calls[0].args == {"query": "xueban"}
