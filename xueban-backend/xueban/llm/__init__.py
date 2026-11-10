"""
LLM Provider 抽象层

提供统一的 LLM API 接口，支持多种 LLM Provider：
- Claude (anthropic)
- Gemini (google-generativeai)
- OpenAI (openai)
- DeepSeek (openai 兼容)
- Ollama (本地)
"""

from .base import (
    LLMProvider,
    LLMResponse,
    LLMConfig,
    ProviderType,
)
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .openai import OpenAIProvider
from .ollama import OllamaProvider
from .zhipu import ZhipuProvider
from .fallback import AllProvidersFailed, FallbackProvider

__all__ = [
    # 基类和数据类
    "LLMProvider",
    "LLMResponse",
    "LLMConfig",
    "ProviderType",
    # Provider 实现
    "ClaudeProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "ZhipuProvider",
    # Fallback chain
    "AllProvidersFailed",
    "FallbackProvider",
]


def create_provider(provider: ProviderType, config: LLMConfig) -> LLMProvider:
    """
    创建 LLM Provider 实例

    Args:
        provider: Provider 类型
        config: LLM 配置

    Returns:
        LLMProvider 实例

    Raises:
        ValueError: 不支持的 Provider 类型
    """
    provider_map = {
        ProviderType.CLAUDE: ClaudeProvider,
        ProviderType.GEMINI: GeminiProvider,
        ProviderType.OPENAI: OpenAIProvider,
        ProviderType.DEEPSEEK: OpenAIProvider,  # DeepSeek 使用 OpenAI 兼容接口
        ProviderType.OLLAMA: OllamaProvider,
        ProviderType.ZHIPU: ZhipuProvider,
    }

    provider_class = provider_map.get(provider)
    if provider_class is None:
        raise ValueError(f"Unsupported provider type: {provider}")

    return provider_class(config)
