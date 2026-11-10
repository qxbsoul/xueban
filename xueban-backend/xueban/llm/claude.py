"""
Claude API 实现

使用 anthropic SDK 调用 Claude API
"""

import asyncio
from typing import Dict, Any, AsyncGenerator, List, Optional

try:
    import anthropic
except ImportError:
    anthropic = None

from .base import LLMProvider, LLMResponse, LLMConfig, ProviderType, Message, ToolCall


def _normalize_vision_content(content):
    """Convert OpenAI-style content list to Claude API format.

    OpenAI image_url block:
      {"type": "image_url", "image_url": {"url": "data:<media_type>;base64,<data>"}}
    Claude image block:
      {"type": "image", "source": {"type": "base64", "media_type": "<media_type>", "data": "<data>"}}
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    result = []
    for part in content:
        if not isinstance(part, dict):
            result.append(part)
            continue
        if part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # Parse data URL: data:<media_type>;base64,<data>
                try:
                    header, data = url.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                except (ValueError, IndexError):
                    media_type = "image/jpeg"
                    data = url
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
            else:
                # Remote URL — Claude supports url source type
                result.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        else:
            result.append(part)
    return result


class ClaudeProvider(LLMProvider):
    """Claude API Provider"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._provider_type = ProviderType.CLAUDE

        if anthropic is None:
            raise ImportError(
                "anthropic package is required. Install it with: pip install anthropic"
            )

        if not config.api_key:
            raise ValueError("api_key is required for Claude provider")

        self.client = anthropic.AsyncAnthropic(
            api_key=config.api_key,
            timeout=config.timeout,
        )

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成响应"""
        config = self._merge_config(**kwargs)

        try:
            response = await self.client.messages.create(
                model=self.config.model,
                max_tokens=config.get("max_tokens", self.config.max_tokens),
                temperature=config.get("temperature", self.config.temperature),
                messages=[{"role": "user", "content": prompt}],
            )

            content = ""
            for block in response.content:
                if hasattr(block, "text"):
                    content += block.text

            return LLMResponse(
                content=content,
                model=response.model,
                provider=self.provider_type,
                tokens_used=response.usage.input_tokens + response.usage.output_tokens,
                finish_reason=response.stop_reason,
                metadata={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "id": response.id,
                },
            )
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude API error: {e}")
        except Exception as e:
            raise RuntimeError(f"Claude provider error: {e}")

    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        """流式生成"""
        config = self._merge_config(**kwargs)

        try:
            async with self.client.messages.stream(
                model=self.config.model,
                max_tokens=config.get("max_tokens", self.config.max_tokens),
                temperature=config.get("temperature", self.config.temperature),
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude API stream error: {e}")
        except Exception as e:
            raise RuntimeError(f"Claude provider stream error: {e}")

    async def chat(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> LLMResponse:
        """对话模式；传入 tools 时启用 tool_use 模式"""
        config = self._merge_config(**kwargs)

        # 转换消息格式；将 ReAct 中间格式转换为 Claude API 格式
        # Claude requires: all tool_result blocks for one assistant turn → single "user" message
        raw_messages: List[Dict[str, Any]] = []
        system_message = None

        for msg in messages:
            if isinstance(msg, Message):
                msg_dict = msg.to_dict()
            elif isinstance(msg, dict):
                msg_dict = dict(msg)  # 不修改原始消息
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")

            # _tool_calls 是 service.py 的规范中间格式 → Claude content blocks
            if msg_dict.get("role") == "assistant" and "_tool_calls" in msg_dict:
                blocks: list = []
                if msg_dict.get("content"):
                    blocks.append({"type": "text", "text": msg_dict["content"]})
                for tc in msg_dict.pop("_tool_calls"):
                    # P9: validate required fields before access
                    if not all(k in tc for k in ("id", "name", "args")):
                        raise ValueError(f"Tool call entry missing required fields (id/name/args): {tc!r}")
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["args"],
                    })
                msg_dict["content"] = blocks

            # role:tool 必须带 tool_call_id，否则是格式错误
            elif msg_dict.get("role") == "tool" and "tool_call_id" not in msg_dict:
                raise ValueError(
                    f"Message with role='tool' is missing required 'tool_call_id': {msg_dict!r}"
                )

            # OpenAI 风格的 tool 结果 → Claude tool_result block（暂存，稍后合并）
            elif msg_dict.get("role") == "tool" and "tool_call_id" in msg_dict:
                msg_dict = {
                    "role": "_tool_result",  # 内部标记，后续聚合
                    "content": {
                        "type": "tool_result",
                        "tool_use_id": msg_dict["tool_call_id"],
                        "content": str(msg_dict.get("content", "")),
                    },
                }

            if msg_dict["role"] == "system":
                system_message = msg_dict["content"]
            else:
                raw_messages.append(msg_dict)

        # 聚合：将连续的 _tool_result 条目合并为单个 role="user" 消息
        # （Claude API 要求：同一 assistant turn 的所有 tool_result 放在一条 user 消息里）
        api_messages: List[Dict[str, Any]] = []
        for raw in raw_messages:
            if raw["role"] == "_tool_result":
                if api_messages and api_messages[-1]["role"] == "user" and \
                        isinstance(api_messages[-1]["content"], list) and \
                        api_messages[-1].get("_is_tool_results"):
                    # 追加到已存在的 tool_result user 消息
                    api_messages[-1]["content"].append(raw["content"])
                else:
                    # 新建一个专用 tool_result user 消息
                    api_messages.append({"role": "user", "content": [raw["content"]], "_is_tool_results": True})
            else:
                # 普通消息：移除内部标记后追加
                clean = {k: v for k, v in raw.items() if k != "_is_tool_results"}
                if "content" in clean:
                    clean = dict(clean)
                    clean["content"] = _normalize_vision_content(clean["content"])
                api_messages.append(clean)

        # 移除内部聚合标记，不得传给 Anthropic API（会导致 400/422）
        api_messages = [
            {k: v for k, v in m.items() if k != "_is_tool_results"}
            for m in api_messages
        ]

        try:
            create_kwargs: Dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": config.get("max_tokens", self.config.max_tokens),
                "temperature": config.get("temperature", self.config.temperature),
                "messages": api_messages,
            }

            if system_message:
                create_kwargs["system"] = system_message

            # 将 MCP 工具定义格式化为 Claude tools 参数
            # MCP schema: {name, description, inputSchema}
            # Claude API schema: {name, description, input_schema}
            if tools:
                create_kwargs["tools"] = [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "input_schema": t.get("inputSchema", t.get("input_schema", {})),
                    }
                    for t in tools
                ]

            response = await self.client.messages.create(**create_kwargs)

            content = ""
            tool_calls: List[ToolCall] = []
            for block in response.content:
                if hasattr(block, "text"):
                    content += block.text
                elif getattr(block, "type", None) == "tool_use":
                    tool_calls.append(
                        ToolCall(id=block.id, name=block.name, args=block.input)
                    )

            return LLMResponse(
                content=content,
                model=response.model,
                provider=self.provider_type,
                tokens_used=response.usage.input_tokens + response.usage.output_tokens,
                finish_reason=response.stop_reason,
                metadata={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "id": response.id,
                },
                tool_calls=tool_calls,
            )
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude API chat error: {e}")
        except Exception as e:
            raise RuntimeError(f"Claude provider chat error: {e}")

    async def chat_stream(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """对话模式流式生成。

        当 tools 不为空时降级为非流式（tool_use 回合需要完整响应解析），
        保证多轮 ReAct 循环的正确性。
        """
        if tools:
            # tool_use 场景下无法可靠流式解析，委托给 chat() 再 yield
            response = await self.chat(messages, tools=tools, **kwargs)
            yield response.content
            return

        config = self._merge_config(**kwargs)

        # 纯文本对话：使用原生流式 API
        api_messages = []
        system_message = None

        for msg in messages:
            if isinstance(msg, Message):
                msg_dict = msg.to_dict()
            elif isinstance(msg, dict):
                msg_dict = dict(msg)
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")

            if msg_dict["role"] == "system":
                system_message = msg_dict["content"]
            else:
                api_messages.append({
                    "role": msg_dict["role"],
                    "content": _normalize_vision_content(msg_dict.get("content", "")),
                })

        try:
            create_kwargs: Dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": config.get("max_tokens", self.config.max_tokens),
                "temperature": config.get("temperature", self.config.temperature),
                "messages": api_messages,
            }

            if system_message:
                create_kwargs["system"] = system_message

            async with self.client.messages.stream(**create_kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude API chat stream error: {e}")
        except Exception as e:
            raise RuntimeError(f"Claude provider chat stream error: {e}")
