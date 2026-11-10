"""
Ollama 本地 LLM 实现

使用 httpx 调用本地 Ollama API
"""

import json
from typing import Dict, Any, AsyncGenerator

try:
    import httpx
except ImportError:
    httpx = None

from .base import LLMProvider, LLMResponse, LLMConfig, ProviderType, Message


class OllamaProvider(LLMProvider):
    """本地 Ollama Provider"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._provider_type = ProviderType.OLLAMA

        if httpx is None:
            raise ImportError(
                "httpx package is required. Install it with: pip install httpx"
            )

        # 设置默认的 Ollama base_url
        self.base_url = config.base_url or "http://localhost:11434"
        self.timeout = config.timeout

    def _get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端"""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        )

    def _build_options(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """构建 Ollama API options"""
        options = {
            "temperature": config.get("temperature", self.config.temperature),
            "num_predict": config.get("max_tokens", self.config.max_tokens),
        }

        if self.config.top_p is not None:
            options["top_p"] = self.config.top_p

        return options

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成响应"""
        config = self._merge_config(**kwargs)

        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": self._build_options(config),
        }

        async with self._get_client() as client:
            try:
                response = await client.post("/api/generate", json=payload)
                response.raise_for_status()

                data = response.json()

                return LLMResponse(
                    content=data.get("response", ""),
                    model=data.get("model", self.config.model),
                    provider=self.provider_type,
                    tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
                    finish_reason=data.get("done_reason", "stop"),
                    metadata={
                        "eval_count": data.get("eval_count", 0),
                        "prompt_eval_count": data.get("prompt_eval_count", 0),
                        "eval_duration": data.get("eval_duration"),
                        "prompt_eval_duration": data.get("prompt_eval_duration"),
                    },
                )
            except httpx.HTTPError as e:
                raise RuntimeError(f"Ollama API error: {e}")
            except Exception as e:
                raise RuntimeError(f"Ollama provider error: {e}")

    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        """流式生成"""
        config = self._merge_config(**kwargs)

        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": True,
            "options": self._build_options(config),
        }

        async with self._get_client() as client:
            try:
                async with client.stream("POST", "/api/generate", json=payload) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if "response" in data:
                                yield data["response"]
                        except json.JSONDecodeError:
                            continue
            except httpx.HTTPError as e:
                raise RuntimeError(f"Ollama API stream error: {e}")
            except Exception as e:
                raise RuntimeError(f"Ollama provider stream error: {e}")

    async def chat(self, messages: list, **kwargs) -> LLMResponse:
        """对话模式"""
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

        payload = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": False,
            "options": self._build_options(config),
        }

        async with self._get_client() as client:
            try:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()

                data = response.json()

                content = ""
                if "message" in data and "content" in data["message"]:
                    content = data["message"]["content"]

                return LLMResponse(
                    content=content,
                    model=data.get("model", self.config.model),
                    provider=self.provider_type,
                    tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
                    finish_reason=data.get("done_reason", "stop"),
                    metadata={
                        "eval_count": data.get("eval_count", 0),
                        "prompt_eval_count": data.get("prompt_eval_count", 0),
                        "eval_duration": data.get("eval_duration"),
                        "prompt_eval_duration": data.get("prompt_eval_duration"),
                    },
                )
            except httpx.HTTPError as e:
                raise RuntimeError(f"Ollama API chat error: {e}")
            except Exception as e:
                raise RuntimeError(f"Ollama provider chat error: {e}")

    async def chat_stream(self, messages: list, **kwargs) -> AsyncGenerator[str, None]:
        """对话模式流式生成"""
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

        payload = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "options": self._build_options(config),
        }

        async with self._get_client() as client:
            try:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if "message" in data and "content" in data["message"]:
                                yield data["message"]["content"]
                        except json.JSONDecodeError:
                            continue
            except httpx.HTTPError as e:
                raise RuntimeError(f"Ollama API chat stream error: {e}")
            except Exception as e:
                raise RuntimeError(f"Ollama provider chat stream error: {e}")
