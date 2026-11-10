"""
Gemini API 实现

使用 google-generativeai SDK 调用 Gemini API
"""

import asyncio
from typing import Dict, Any, AsyncGenerator

try:
    import google.generativeai as genai
except ImportError:
    genai = None

from .base import LLMProvider, LLMResponse, LLMConfig, ProviderType, Message


class GeminiProvider(LLMProvider):
    """Gemini API Provider"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._provider_type = ProviderType.GEMINI

        if genai is None:
            raise ImportError(
                "google-generativeai package is required. Install it with: pip install google-generativeai"
            )

        if not config.api_key:
            raise ValueError("api_key is required for Gemini provider")

        # 配置 API key
        genai.configure(api_key=config.api_key)

        # 初始化模型
        self.model = genai.GenerativeModel(config.model)

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成响应"""
        config = self._merge_config(**kwargs)

        try:
            # Gemini 使用生成器，即使是非流式也返回生成器
            result = await asyncio.to_thread(
                self.model.generate_content,
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=config.get("temperature", self.config.temperature),
                    max_output_tokens=config.get("max_tokens", self.config.max_tokens),
                ),
            )

            content = result.text

            # 获取 token 使用情况（如果可用）
            tokens_used = 0
            if hasattr(result, "usage_metadata") and result.usage_metadata:
                tokens_used = result.usage_metadata.total_token_count

            return LLMResponse(
                content=content,
                model=self.config.model,
                provider=self.provider_type,
                tokens_used=tokens_used,
                finish_reason="stop",
                metadata={
                    "candidates": len(result.candidates) if hasattr(result, "candidates") else 0,
                },
            )
        except Exception as e:
            raise RuntimeError(f"Gemini provider error: {e}")

    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        """流式生成"""
        config = self._merge_config(**kwargs)

        try:
            result = await asyncio.to_thread(
                self.model.generate_content,
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=config.get("temperature", self.config.temperature),
                    max_output_tokens=config.get("max_tokens", self.config.max_tokens),
                ),
                stream=True,
            )

            for chunk in result:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            raise RuntimeError(f"Gemini provider stream error: {e}")

    async def chat(self, messages: list, **kwargs) -> LLMResponse:
        """对话模式"""
        config = self._merge_config(**kwargs)

        try:
            # 构建 Gemini 的聊天历史
            history = []
            system_instruction = None

            for msg in messages:
                if isinstance(msg, Message):
                    role = msg.role
                    content = msg.content
                elif isinstance(msg, dict):
                    role = msg["role"]
                    content = msg["content"]
                else:
                    raise ValueError(f"Invalid message type: {type(msg)}")

                # 处理角色映射
                if role == "system":
                    system_instruction = content
                    continue
                elif role == "user":
                    gemini_role = "user"
                elif role == "assistant":
                    gemini_role = "model"
                else:
                    gemini_role = "user"

                history.append(
                    genai.types.HarmBlockThreshold(
                        role=gemini_role,
                        parts=[{"text": content}],
                    )
                )

            # 如果有系统提示，创建带系统指令的模型
            model = self.model
            if system_instruction:
                model = genai.GenerativeModel(
                    self.config.model,
                    system_instruction=system_instruction,
                )

            # 开始聊天
            chat = model.start_chat(history=history[:-1] if len(history) > 1 else [])

            # 发送最后一条消息
            last_message = history[-1]["parts"][0]["text"]
            result = await asyncio.to_thread(
                chat.send_message,
                last_message,
                generation_config=genai.types.GenerationConfig(
                    temperature=config.get("temperature", self.config.temperature),
                    max_output_tokens=config.get("max_tokens", self.config.max_tokens),
                ),
            )

            content = result.text

            # 获取 token 使用情况
            tokens_used = 0
            if hasattr(result, "usage_metadata") and result.usage_metadata:
                tokens_used = result.usage_metadata.total_token_count

            return LLMResponse(
                content=content,
                model=self.config.model,
                provider=self.provider_type,
                tokens_used=tokens_used,
                finish_reason="stop",
                metadata={
                    "candidates": len(result.candidates) if hasattr(result, "candidates") else 0,
                },
            )
        except Exception as e:
            raise RuntimeError(f"Gemini provider chat error: {e}")

    async def chat_stream(self, messages: list, **kwargs) -> AsyncGenerator[str, None]:
        """对话模式流式生成"""
        config = self._merge_config(**kwargs)

        try:
            # 构建 Gemini 的聊天历史
            history = []
            system_instruction = None

            for msg in messages:
                if isinstance(msg, Message):
                    role = msg.role
                    content = msg.content
                elif isinstance(msg, dict):
                    role = msg["role"]
                    content = msg["content"]
                else:
                    raise ValueError(f"Invalid message type: {type(msg)}")

                # 处理角色映射
                if role == "system":
                    system_instruction = content
                    continue
                elif role == "user":
                    gemini_role = "user"
                elif role == "assistant":
                    gemini_role = "model"
                else:
                    gemini_role = "user"

                history.append(
                    {
                        "role": gemini_role,
                        "parts": [{"text": content}],
                    }
                )

            # 如果有系统提示，创建带系统指令的模型
            model = self.model
            if system_instruction:
                model = genai.GenerativeModel(
                    self.config.model,
                    system_instruction=system_instruction,
                )

            # 开始聊天
            chat = model.start_chat(history=history[:-1] if len(history) > 1 else [])

            # 发送最后一条消息（流式）
            last_message = history[-1]["parts"][0]["text"]
            result = await asyncio.to_thread(
                chat.send_message,
                last_message,
                generation_config=genai.types.GenerationConfig(
                    temperature=config.get("temperature", self.config.temperature),
                    max_output_tokens=config.get("max_tokens", self.config.max_tokens),
                ),
                stream=True,
            )

            for chunk in result:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            raise RuntimeError(f"Gemini provider chat stream error: {e}")
