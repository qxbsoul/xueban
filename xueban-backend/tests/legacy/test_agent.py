"""
Agent 类单元测试
"""

import pytest
import asyncio
from datetime import datetime
from dataclasses import asdict

from agentgraph.core.agent import Agent, AgentConfig, AgentResult

pytestmark = pytest.mark.legacy


class TestAgentCreation:
    """测试 Agent 创建"""

    def test_agent_creation_with_valid_config(self):
        """测试使用有效配置创建 Agent"""
        config = AgentConfig(
            name="test_agent",
            role="测试角色",
            prompt="这是一个测试提示"
        )
        agent = Agent(config, "agent1")

        assert agent.config.name == "test_agent"
        assert agent.config.role == "测试角色"
        assert agent.agent_id == "agent1"
        assert agent.step_count == 0
        assert agent._tool_registry == {}

    def test_agent_creation_with_full_config(self):
        """测试使用完整配置创建 Agent"""
        from agentgraph.core.agent import ToolConfig
    config = AgentConfig(
        name="full_agent",
        role="完整测试角色",
        prompt="完整的测试提示",
        tools=["tool1", "tool2"],  # 字符串列表
        enable_reasoning=True,
        enable_validation=False,
        fallback_agent="fallback_agent",
        max_retries=5,
        timeout=60,
        enable_trace=False,
        enable_fallback=True
    )
    agent = Agent(config, "full_agent_id")

    assert agent.config.name == "full_agent"
    # tools 现在是 ToolConfig 列表，需要检查名称
    assert len(agent.config.tools) == 2
    assert agent.config.tools[0].name == "tool1"
    assert agent.config.tools[1].name == "tool2"
    assert agent.config.max_retries == 5
    assert agent.config.timeout == 60

    def test_agent_config_validation(self):
        """测试 Agent 配置验证"""
        # 测试必需字段
        config = AgentConfig(
            name="required_fields",
            role="角色",
            prompt="提示"
        )
        agent = Agent(config, "test_id")

        assert config.name is not None
        assert config.role is not None
        assert config.prompt is not None
        assert config.enable_reasoning is True
        assert config.enable_validation is True

    def test_agent_from_yaml(self):
        """测试从 YAML 创建 Agent"""
        yaml_content = """
        name: yaml_agent
        role: YAML 角色
        prompt: YAML 提示
        tools:
          - tool1
          - tool2
        enable_reasoning: false
        max_retries: 2
        """

        import yaml
        config_dict = yaml.safe_load(yaml_content)
        config = AgentConfig(**config_dict)
        agent = Agent(config, "yaml_agent_id")

        assert agent.config.name == "yaml_agent"
        assert agent.config.role == "YAML 角色"
        # tools 现在是 ToolConfig 列表
        assert len(agent.config.tools) == 2
        assert agent.config.tools[0].name == "tool1"
        assert agent.config.tools[1].name == "tool2"
        assert agent.config.enable_reasoning is False

    def test_agent_from_dict(self):
        """测试从字典创建 Agent"""
        config_dict = {
            "name": "dict_agent",
            "role": "字典角色",
            "prompt": "字典提示",
            "tools": ["dict_tool"],
            "enable_validation": True
        }

        config = AgentConfig(**config_dict)
        agent = Agent(config, "dict_agent_id")

        assert agent.config.name == "dict_agent"
        # tools 现在是 ToolConfig 列表
        assert len(agent.config.tools) == 1
        assert agent.config.tools[0].name == "dict_tool"
        assert agent.config.enable_validation is True


class TestAgentBid:
    """测试 Agent 竞标功能"""

    @pytest.fixture
    def test_agent(self):
        """创建测试 Agent"""
        config = AgentConfig(
            name="bid_test",
            role="数据分析专家",
            prompt="我擅长分析数据和生成报告",
            tools=["calculator", "chart_generator"]
        )
        return Agent(config, "bid_agent")

    @pytest.mark.asyncio
    async def test_agent_bid_basic(self, test_agent):
        """测试基本竞标功能"""
        input_text = "分析销售数据并生成月度报告"
        state = {"input": input_text}

        score, reason = await test_agent.bid(input_text, state)

        assert 0.0 <= score <= 1.0
        assert isinstance(reason, str)
        assert len(reason) > 0

    @pytest.mark.asyncio
    async def test_agent_bid_role_match(self, test_agent):
        """测试角色匹配度对竞标分数的影响"""
        # 测试与角色高度匹配的输入
        high_match_input = "分析销售数据并生成数据分析报告"
        state = {"input": high_match_input}
        score_high, _ = await test_agent.bid(high_match_input, state)

        # 测试与角色低匹配的输入
        low_match_input = "写一首关于爱情的诗"
        state = {"input": low_match_input}
        score_low, _ = await test_agent.bid(low_match_input, state)

        # 高匹配应该得到更高分数
        assert score_high > score_low

    @pytest.mark.asyncio
    async def test_agent_bid_with_tools(self, test_agent):
        """测试工具可用性对竞标分数的影响"""
        config_with_tools = AgentConfig(
            name="tool_agent",
            role="技术专家",
            prompt="解决技术问题",
            tools=["python", "javascript", "database"]
        )
        tool_agent = Agent(config_with_tools, "tool_agent_id")

        input_text = "帮我修复一个 Python 错误"
        state = {"input": input_text}

        score, reason = await tool_agent.bid(input_text, state)

        # 有工具的 Agent 应该获得额外加分
        assert 0.0 <= score <= 1.0
        assert "Tools:" in reason

    @pytest.mark.asyncio
    async def test_agent_bid_reason_generation(self, test_agent):
        """测试竞标原因生成"""
        input_text = "分析用户行为数据"
        state = {"input": input_text, "user_id": "user123"}

        score, reason = await test_agent.bid(input_text, state)

        # 验证原因包含关键信息
        assert "Role:" in reason
        assert "Confidence:" in reason
        assert "0." in reason  # 置信度分数
        assert str(score) in reason

    @pytest.mark.asyncio
    async def test_agent_bid_multiple_calls(self, test_agent):
        """测试多次竞标的一致性"""
        input_text = "分析数据"
        state = {"input": input_text}

        scores = []
        reasons = []

        for _ in range(5):
            score, reason = await test_agent.bid(input_text, state)
            scores.append(score)
            reasons.append(reason)

        # 分数应该在合理范围内保持一致
        assert all(0.0 <= s <= 1.0 for s in scores)
        # 相同输入的原因应该基本一致
        assert len(set(reasons)) <= 2  # 允许微小差异


class TestAgentFunctionality:
    """测试 Agent 功能"""

    @pytest.fixture
    def sample_agent(self):
        """创建示例 Agent"""
        config = AgentConfig(
            name="sample",
            role="示例角色",
            prompt="示例提示"
        )
        return Agent(config, "sample_id")

    @pytest.mark.asyncio
    async def test_agent_invoke_without_state(self, sample_agent):
        """测试没有状态时的调用"""
        result = await sample_agent.invoke("测试输入")

        assert isinstance(result, AgentResult)
        assert result.agent_id == "sample_id"
        assert result.output
        assert result.confidence >= 0.0
        assert result.timestamp
        assert "示例角色" in result.output

    @pytest.mark.asyncio
    async def test_agent_invoke_with_state(self, sample_agent):
        """测试有状态时的调用"""
        state = {
            "input": "有状态的测试",
            "user_id": "test_user",
            "context": {"key": "value"}
        }

        result = await sample_agent.invoke("测试输入", state)

        assert isinstance(result, AgentResult)
        assert result.agent_id == "sample_id"
        assert result.metadata["state_keys"] == ["context"]
        assert "execution_time" in result.metadata

    @pytest.mark.asyncio
    async def test_agent_tool_registration(self, sample_agent):
        """测试工具注册"""
        def mock_tool1(input_text, state):
            return "工具1的结果"

        def mock_tool2(input_text, state):
            return "工具2的结果"

        sample_agent.register_tool("tool1", mock_tool1)
        sample_agent.register_tool("tool2", mock_tool2)

        assert "tool1" in sample_agent._tool_registry
        assert "tool2" in sample_agent._tool_registry
        assert sample_agent._tool_registry["tool1"] == mock_tool1

    @pytest.mark.asyncio
    async def test_agent_tool_execution(self, sample_agent):
        """测试工具执行"""
        def mock_tool(input_text, state):
            return f"处理了: {input_text}"

        sample_agent.register_tool("mock", mock_tool)
        sample_agent.config.tools = ["mock"]

        result = await sample_agent.invoke("测试工具")

        assert isinstance(result, AgentResult)
        assert "工具执行结果" in result.output
        assert "mock:" in result.output
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["tool"] == "mock"
        assert result.tool_calls[0]["success"] is True

    @pytest.mark.asyncio
    async def test_agent_tool_error_handling(self, sample_agent):
        """测试工具错误处理"""
        def failing_tool(input_text, state):
            raise ValueError("工具执行失败")

        sample_agent.register_tool("failing", failing_tool)
        sample_agent.config.tools = ["failing"]

        result = await sample_agent.invoke("测试失败工具")

        assert isinstance(result, AgentResult)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["tool"] == "failing"
        assert result.tool_calls[0]["success"] is False
        assert "error" in result.tool_calls[0]

    def test_agent_step_counter(self, sample_agent):
        """测试步数计数器"""
        assert sample_agent.step_count == 0

        sample_agent._step_counter = 5
        assert sample_agent.step_count == 5

        sample_agent.reset_step_counter()
        assert sample_agent.step_count == 0

    def test_agent_protocol_traces(self, sample_agent):
        """测试协议追踪"""
        # 初始时没有追踪
        traces = sample_agent.get_protocol_traces()
        assert traces == []

        formatted = sample_agent.get_protocol_trace_formatted()
        assert formatted == "Protocol not enabled."

        # 清除追踪
        sample_agent.clear_protocol_traces()
        assert sample_agent.get_protocol_traces() == []


class TestAgentResult:
    """测试 AgentResult"""

    def test_agent_result_creation(self):
        """测试 AgentResult 创建"""
        result = AgentResult(
            agent_id="test_agent",
            output="测试输出",
            reasoning="测试推理",
            confidence=0.85,
            tool_calls=[{"tool": "test", "success": True}],
            metadata={"key": "value"}
        )

        assert result.agent_id == "test_agent"
        assert result.output == "测试输出"
        assert result.reasoning == "测试推理"
        assert result.confidence == 0.85
        assert result.tool_calls == [{"tool": "test", "success": True}]
        assert result.metadata == {"key": "value"}
        assert isinstance(result.timestamp, datetime)

    def test_agent_result_serialization(self):
        """测试 AgentResult 序列化"""
        result = AgentResult(
            agent_id="test_agent",
            output="测试输出",
            reasoning="测试推理",
            confidence=0.85
        )

        # 转换为字典
        result_dict = asdict(result)

        assert result_dict["agent_id"] == "test_agent"
        assert result_dict["output"] == "测试输出"
        assert result_dict["confidence"] == 0.85
        assert "timestamp" in result_dict


@pytest.mark.asyncio
class TestAgentConcurrency:
    """测试 Agent 并发处理"""

    @pytest.mark.asyncio
    async def test_multiple_agent_bids(self):
        """测试多个 Agent 同时竞标"""
        # 创建多个 Agent
        agents = []
        for i in range(3):
            config = AgentConfig(
                name=f"agent{i}",
                role=f"角色{i}",
                prompt=f"提示{i}"
            )
            agent = Agent(config, f"id{i}")
            agents.append(agent)

        input_text = "测试输入"
        state = {"input": input_text}

        # 并发执行竞标
        bids = await asyncio.gather(*[
            agent.bid(input_text, state) for agent in agents
        ])

        # 验证所有竞标都成功
        assert len(bids) == 3
        for score, reason in bids:
            assert 0.0 <= score <= 1.0
            assert isinstance(reason, str)

    @pytest.mark.asyncio
    async def test_agent_concurrent_invocation(self):
        """测试 Agent 并发调用"""
        config = AgentConfig(
            name="concurrent_agent",
            role="并发测试",
            prompt="提示"
        )
        agent = Agent(config, "concurrent_id")

        # 并发执行多次调用
        tasks = [agent.invoke(f"输入{i}") for i in range(5)]
        results = await asyncio.gather(*tasks)

        # 验证所有调用都成功
        assert len(results) == 5
        for result in results:
            assert isinstance(result, AgentResult)
            assert result.agent_id == "concurrent_id"
            assert result.step_count > 0
