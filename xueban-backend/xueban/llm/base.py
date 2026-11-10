"""
LLM Provider 抽象基类

定义所有 LLM Provider 必须实现的接口
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum


class ProviderType(Enum):
    """Provider 类型枚举"""
    CLAUDE = "claude"
    GEMINI = "gemini"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"
    ZHIPU = "zhipu"


@dataclass
class ToolCall:
    """LLM 工具调用数据类"""
    id: str
    name: str
    args: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args}


@dataclass
class LLMResponse:
    """LLM 响应数据类"""
    content: str
    model: str
    provider: ProviderType
    tokens_used: int = 0
    finish_reason: str = "stop"
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[ToolCall] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "content": self.content,
            "model": self.model,
            "provider": self.provider.value,
            "tokens_used": self.tokens_used,
            "finish_reason": self.finish_reason,
            "metadata": self.metadata,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
        }


@dataclass
class LLMConfig:
    """LLM 配置数据类"""
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 60
    # 其他可选参数
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None


@dataclass
class Message:
    """消息数据类"""
    role: str  # system, user, assistant
    content: str

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {"role": self.role, "content": self.content}


class LLMProvider(ABC):
    """LLM Provider 抽象基类"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._provider_type: Optional[ProviderType] = None

    @property
    def provider_type(self) -> ProviderType:
        """获取 Provider 类型"""
        if self._provider_type is None:
            raise NotImplementedError("Subclass must set _provider_type")
        return self._provider_type

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """
        生成响应

        Args:
            prompt: 输入提示
            **kwargs: 额外参数

        Returns:
            LLMResponse 对象
        """
        pass

    @abstractmethod
    async def stream(self, prompt: str, **kwargs) -> AsyncGenerator[str, None]:
        """
        流式生成

        Args:
            prompt: 输入提示
            **kwargs: 额外参数

        Yields:
            生成的文本片段
        """
        pass

    @abstractmethod
    async def chat(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        对话模式

        Args:
            messages: 消息列表，每个元素是 Message 或字典
            tools: MCP 工具定义列表（可选）。每个元素形如
                   {"name": str, "description": str, "inputSchema": {...}}
            **kwargs: 额外参数

        Returns:
            LLMResponse 对象；若 LLM 发出工具调用则 tool_calls 非空
        """
        pass

    async def chat_stream(
        self,
        messages: list,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """
        对话模式流式生成（默认实现）

        Args:
            messages: 消息列表
            tools: MCP 工具定义列表（可选）
            **kwargs: 额外参数

        Yields:
            生成的文本片段
        """
        response = await self.chat(messages, tools=tools, **kwargs)
        yield response.content

    def _merge_config(self, **kwargs) -> Dict[str, Any]:
        """
        合并配置和额外参数

        Args:
            **kwargs: 额外参数

        Returns:
            合并后的配置字典
        """
        config_dict = {
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        if self.config.top_p is not None:
            config_dict["top_p"] = self.config.top_p
        if self.config.frequency_penalty is not None:
            config_dict["frequency_penalty"] = self.config.frequency_penalty
        if self.config.presence_penalty is not None:
            config_dict["presence_penalty"] = self.config.presence_penalty

        # kwargs 中的参数会覆盖默认值
        config_dict.update(kwargs)

        return config_dict
