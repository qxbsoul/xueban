import pytest
import asyncio
from unittest.mock import Mock, patch
from agentgraph.llm.base import LLMProvider, LLMConfig, LLMResponse, ProviderType

pytestmark = pytest.mark.legacy


class MockLLM(LLMProvider):
    """Mock LLM Provider for testing"""

    def __init__(self, name="mock", model="mock-model"):
        config = LLMConfig(model=model)
        super().__init__(config)
        self.name = name
        self._provider_type = ProviderType.CLAUDE  # 使用一个有效的 provider type
        self.mock_response_content = "Mock response"
        self.metrics = {
            "requests": 0,
            "tokens_used": 0
        }

    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成响应"""
        self.metrics["requests"] += 1
        self.metrics["tokens_used"] += len(prompt) if prompt else 0
        return LLMResponse(
            content=self.mock_response_content,
            model=self.config.model,
            provider=self._provider_type,
            tokens_used=len(prompt) if prompt else 0
        )

    async def stream(self, prompt: str, **kwargs):
        """流式生成"""
        self.metrics["requests"] += 1
        yield self.mock_response_content

    async def chat(self, messages: list, **kwargs) -> LLMResponse:
        """对话模式"""
        self.metrics["requests"] += 1
        total_tokens = sum(len(m.get("content", "")) if isinstance(m, dict) else len(m.content) for m in messages)
        self.metrics["tokens_used"] += total_tokens
        return LLMResponse(
            content=self.mock_response_content,
            model=self.config.model,
            provider=self._provider_type,
            tokens_used=total_tokens
        )


@pytest.mark.asyncio
async def test_mock_llm_initialization():
    """测试 Mock LLM 初始化"""
    llm = MockLLM()
    assert llm.name == "mock"
    assert llm.config.model == "mock-model"


@pytest.mark.asyncio
async def test_mock_llm_generate():
    """测试 Mock LLM 生成"""
    llm = MockLLM()
    response = await llm.generate("Test prompt")
    assert response.content == "Mock response"


@pytest.mark.asyncio
async def test_mock_llm_chat():
    """测试 Mock LLM 聊天"""
    llm = MockLLM()
    messages = [{"role": "user", "content": "Hello"}]
    response = await llm.chat(messages)
    assert response.content == "Mock response"


@pytest.mark.asyncio
async def test_llm_error_handling():
    """测试 LLM 错误处理"""
    llm = MockLLM()

    # 测试空输入
    response = await llm.generate("")
    assert response is not None


@pytest.mark.asyncio
async def test_llm_metrics():
    """测试 LLM 指标收集"""
    llm = MockLLM()

    # 初始指标
    assert llm.metrics["requests"] == 0
    assert llm.metrics["tokens_used"] == 0

    # 执行请求
    await llm.generate("Test prompt")

    # 检查指标更新
    assert llm.metrics["requests"] == 1
    assert llm.metrics["tokens_used"] > 0


@pytest.mark.asyncio
@patch('agentgraph.llm.openai.AsyncOpenAI')
async def test_openai_llm(mock_openai):
    """测试 OpenAI LLM 集成"""
    mock_client = Mock()
    mock_response = Mock()
    mock_response.choices = [Mock()]
    mock_response.choices[0].message.content = "OpenAI response"
    mock_client.chat.completions.create.return_value = mock_response
    mock_openai.return_value = mock_client

    from agentgraph.llm.openai import OpenAILLM
    llm = OpenAILLM(api_key="test-key", model="gpt-3.5-turbo")
    response = await llm.chat([{"role": "user", "content": "Hello"}])

    assert response.content == "OpenAI response"
    mock_client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
@patch('agentgraph.llm.claude.AsyncAnthropic')
async def test_claude_llm(mock_anthropic):
    """测试 Claude LLM 集成"""
    mock_client = Mock()
    mock_response = Mock()
    mock_response.content = [Mock()]
    mock_response.content[0].text = "Claude response"
    mock_client.messages.create.return_value = mock_response
    mock_anthropic.return_value = mock_client

    from agentgraph.llm.claude import ClaudeLLM
    llm = ClaudeLLM(api_key="test-key", model="claude-3-sonnet-20240229")
    response = await llm.chat([{"role": "user", "content": "Hello"}])

    assert response.content == "Claude response"
    mock_client.messages.create.assert_called_once()
