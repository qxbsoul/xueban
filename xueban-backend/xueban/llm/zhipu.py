"""
Zhipu GLM API Provider (OpenAI-compatible)

智谱 GLM 系列模型，接口与 OpenAI chat/completions 兼容。
默认 base_url: https://open.bigmodel.cn/api/paas/v4
支持模型: glm-4, glm-4-flash, glm-4-air, glm-4-airx, glm-3-turbo
"""
from __future__ import annotations

from .base import LLMConfig, ProviderType
from .openai import OpenAIProvider

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


class ZhipuProvider(OpenAIProvider):
    """智谱 GLM Provider（复用 OpenAIProvider，只覆盖 base_url 和 provider_type）"""

    def __init__(self, config: LLMConfig):
        if not config.base_url:
            config.base_url = ZHIPU_BASE_URL
        super().__init__(config)
        self._provider_type = ProviderType.ZHIPU
