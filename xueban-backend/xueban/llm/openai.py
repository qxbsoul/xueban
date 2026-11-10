"""
OpenAI/DeepSeek API 实现

使用 openai SDK，支持 OpenAI 和 DeepSeek（通过 base_url 切换）
"""

import asyncio
import json
import logging
from typing import Dict, Any, AsyncGenerator, List, Optional

logger = logging.getLogger(__name__)

try:
    import openai
except ImportError:
    openai = None

from .base import LLMProvider, LLMResponse, LLMConfig, ProviderType, Message, ToolCall


class OpenAIProvider(LLMProvider):
    """OpenAI/DeepSeek API Provider"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._provider_type = ProviderType.OPENAI

        if openai is None:
            raise ImportError(
                "openai package is required. Install it with: pip install openai"
            )

        if not config.api_key:
            raise ValueError("api_key is required for OpenAI provider")

        # 检测是否为 DeepSeek
        if config.base_url and "deepseek" in config.base_url.lower():
            self._provider_type = ProviderType.DEEPSEEK

        # 使用 base_url（如果提供）
        client_kwargs = {
            "api_key": config.api_key,
            "timeout": config.timeout,
        }

        if config.base_url:
            client_kwargs["base_url"] = config.base_url

        self.client = openai.AsyncOpenAI(**client_kwargs)

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成响应"""
        config = self._merge_config(**kwargs)

        try:
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", self.config.temperature),
                max_tokens=config.get("max_tokens", self.config.max_tokens),
            )

            choice = response.choices[0]
            content = choice.message.content or ""

            return LLMResponse(
                content=content,
                model=response.model,
                provider=self.provider_type,
                tokens_used=response.usage.total_tokens if response.usage else 0,
                finish_reason=choice.finish_reason or "stop",
                metadata={
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "id": response.id,
                },
            )
        except openai.APIError as e:
            raise RuntimeError(f"OpenAI API error: {e}")
        except Exception as e:
            raise RuntimeError(f"OpenAI provider error: {e}")

    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        """流式生成"""
        config = self._merge_config(**kwargs)

        try:
            stream = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", self.config.temperature),
                max_tokens=config.get("max_tokens", self.config.max_tokens),
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content
        except openai.APIError as e:
            raise RuntimeError(f"OpenAI API stream error: {e}")
        except Exception as e:
            raise RuntimeError(f"OpenAI provider stream error: {e}")

    async def chat(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> LLMResponse:
        """对话模式；传入 tools 时启用 OpenAI tool_calls 模式"""
        config = self._merge_config(**kwargs)

        # 转换消息格式；将 ReAct 中间格式转换为 OpenAI API 格式
        api_messages = []
        for msg in messages:
            if isinstance(msg, Message):
                api_messages.append(msg.to_dict())
            elif isinstance(msg, dict):
                converted = dict(msg)
                # _tool_calls 是 service.py 的规范中间格式 → OpenAI tool_calls
                if converted.get("role") == "assistant" and "_tool_calls" in converted:
                    converted["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"], ensure_ascii=False),
                            },
                        }
                        for tc in converted.pop("_tool_calls")
                    ]
                api_messages.append(converted)
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")

        try:
            create_kwargs: Dict[str, Any] = {
                "model": self.config.model,
                "messages": api_messages,
                "temperature": config.get("temperature", self.config.temperature),
                "max_tokens": config.get("max_tokens", self.config.max_tokens),
            }

            # 将 MCP 工具定义格式化为 OpenAI tools 参数
            # MCP schema: {name, description, inputSchema}
            # OpenAI API schema: {type:"function", function:{name, description, parameters}}
            if tools:
                create_kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("inputSchema", t.get("input_schema", {})),
                        },
                    }
                    for t in tools
                ]
                create_kwargs["tool_choice"] = "auto"

            response = await self.client.chat.completions.create(**create_kwargs)

            choice = response.choices[0]
            content = choice.message.content or ""

            # 解析 tool_calls
            tool_calls: List[ToolCall] = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception as exc:
                            logger.warning(
                                "Failed to parse tool call arguments for '%s': %s — using {}",
                                tc.function.name, exc,
                            )
                            args = {}
                    tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

            return LLMResponse(
                content=content,
                model=response.model,
                provider=self.provider_type,
                tokens_used=response.usage.total_tokens if response.usage else 0,
                finish_reason=choice.finish_reason or "stop",
                metadata={
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "id": response.id,
                },
                tool_calls=tool_calls,
            )
        except openai.APIError as e:
            raise RuntimeError(f"OpenAI API chat error: {e}")
        except Exception as e:
            raise RuntimeError(f"OpenAI provider chat error: {e}")

    async def chat_stream(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """对话模式流式生成。

        当 tools 不为空时降级为非流式（streaming tool_call chunk 聚合复杂），
        委托给 chat() 保证正确性。
        """
        if tools:
            response = await self.chat(messages, tools=tools, **kwargs)
            yield response.content
            return

        config = self._merge_config(**kwargs)

        # 转换消息格式
        api_messages = []
        for msg in messages:
            if isinstance(msg, Message):
                api_messages.append(msg.to_dict())
            elif isinstance(msg, dict):
                api_messages.append(msg)
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")

        try:
            stream = await self.client.chat.completions.create(
                model=self.config.model,
                messages=api_messages,
                temperature=config.get("temperature", self.config.temperature),
                max_tokens=config.get("max_tokens", self.config.max_tokens),
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content
        except openai.APIError as e:
            raise RuntimeError(f"OpenAI API chat stream error: {e}")
        except Exception as e:
            raise RuntimeError(f"OpenAI provider chat stream error: {e}")
