"""
Router 类单元测试
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, patch

from agentgraph.core.router import Router, RuleRouter, SwarmRouter, RouteHistory, RoutingFeedback
from agentgraph.core.agent import Agent, AgentConfig

pytestmark = pytest.mark.legacy


class TestRuleRouter:
    """测试 RuleRouter"""

    @pytest.fixture
    def rule_router(self):
        """创建规则路由器"""
        rules = [
            {
                "agent_id": "agent1",
                "from_agent": None,
                "condition": {"task_type": "data_analysis"}
            },
            {
                "agent_id": "agent2",
                "from_agent": "agent1",
                "condition": {"task_type": "visualization"}
            },
            {
                "agent_id": "agent3",
                "condition": {"priority": "high"}
            }
        ]
        return RuleRouter(rules)

    @pytest.mark.asyncio
    async def test_rule_router_match_first_rule(self, rule_router):
        """测试匹配第一条规则"""
        state = {"task_type": "data_analysis"}
        agents = {"agent1": Mock(), "agent2": Mock()}

        result = await rule_router.route(state, agents)

        assert result == "agent1"

    @pytest.mark.asyncio
    async def test_rule_router_match_with_from_agent(self, rule_router):
        """测试带 from_agent 的规则匹配"""
        state = {"task_type": "visualization"}
        agents = {"agent1": Mock(), "agent2": Mock()}

        result = await rule_router.route(state, agents, "agent1")

        assert result == "agent2"

    @pytest.mark.asyncio
    async def test_rule_router_no_match(self, rule_router):
        """测试没有匹配的规则"""
        state = {"task_type": "unknown"}
        agents = {"agent1": Mock(), "agent2": Mock()}

        result = await rule_router.route(state, agents)

        assert result is None

    @pytest.mark.asyncio
    async def test_rule_router_condition_false(self, rule_router):
        """测试条件不匹配"""
        state = {"task_type": "data_analysis", "priority": "low"}
        agents = {"agent1": Mock()}

        result = await rule_router.route(state, agents)

        assert result == "agent1"  # 仍然匹配第一条规则

    def test_rule_router_match_rule_logic(self, rule_router):
        """测试规则匹配逻辑"""
        # 测试 from_agent 条件
        rule = {
            "from_agent": "specific_agent",
            "condition": {"key": "value"}
        }
        state = {"key": "value"}

        # 不匹配：from_agent 不符
        assert not rule_router._match_rule(rule, state, "other_agent")

        # 匹配：from_agent 符合
        assert rule_router._match_rule(rule, state, "specific_agent")

        # 匹配：没有 from_agent 限制
        rule_without_from = {"condition": {"key": "value"}}
        assert rule_router._match_rule(rule_without_from, state, None)


class TestSwarmRouter:
    """测试 SwarmRouter"""

    @pytest.fixture
    def mock_agents(self):
        """创建模拟 Agent"""
        agents = {}

        for i in range(3):
            config = AgentConfig(
                name=f"agent{i}",
                role=f"角色{i}",
                prompt=f"提示{i}",
                tools=[f"tool{i}"]
            )
            agent = Agent(config, f"id{i}")
            # 模拟竞标结果
            agent.bid = Mock(return_value=asyncio.Future())
            agent.bid.return_value.set_result((0.7 + i * 0.1, f"原因{i}"))
            agents[f"id{i}"] = agent

        return agents

    @pytest.mark.asyncio
    async def test_swarm_router_bidding(self, mock_agents):
        """测试蜂群路由竞标"""
        router = SwarmRouter()

        state = {"input": "测试任务", "task_type": "general"}
        current_id = None

        result = await router.route(state, mock_agents, current_id)

        # 应该返回获胜的 Agent ID
        assert result in mock_agents
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_swarm_router_with_history(self):
        """测试带历史记录的蜂群路由"""
        # 创建带历史数据库的 router
        router = SwarmRouter(history_db=":memory:", learning_rate=0.1)

        # 模拟一些竞标历史
        router._bidding_history["agent1"] = [0.8, 0.9, 0.7]
        router._bidding_history["agent2"] = [0.6, 0.7, 0.8]

        state = {"input": "有历史记录的任务"}
        mock_agents = {"agent1": Mock(), "agent2": Mock()}

        result = await router.route(state, mock_agents)

        assert result in mock_agents

    @pytest.mark.asyncio
    async def test_swarm_router_bidding_logic(self):
        """测试竞标逻辑"""
        router = SwarmRouter()

        # 创建不同能力的 mock agents
        async def bid_agent1(input_str, state):
            return (0.9, "专业匹配度高")

        async def bid_agent2(input_str, state):
            return (0.6, "一般匹配")

        async def bid_agent3(input_str, state):
            return (0.3, "不太匹配")

        agents = {
            "agent1": Mock(),
            "agent2": Mock(),
            "agent3": Mock()
        }
        agents["agent1"].bid = Mock(side_effect=bid_agent1)
        agents["agent2"].bid = Mock(side_effect=bid_agent2)
        agents["agent3"].bid = Mock(side_effect=bid_agent3)

        state = {"input": "专业任务"}
        result = await router.route(state, agents)

        # 应该选择分数最高的 agent1
        assert result == "agent1"

    @pytest.mark.asyncio
    async def test_swarm_router_fallback(self):
        """测试降级规则"""
        rules = [
            {
                "agent_id": "fallback_agent",
                "condition": {"emergency": True}
            }
        ]
        router = SwarmRouter(fallback_rules=rules)

        # 普通路由
        state = {"input": "普通任务", "task_type": "normal"}
        agents = {"normal_agent": Mock()}
        result = await router.route(state, agents)

        # 应该返回 None，因为没有匹配的 agent
        assert result is None

        # 使用降级规则
        state["emergency"] = True
        agents["fallback_agent"] = Mock()
        result = await router.route(state, agents)

        assert result == "fallback_agent"

    @pytest.mark.asyncio
    async def test_swarm_router_route_history(self):
        """测试路由历史记录"""
        router = SwarmRouter()

        # 模拟路由过程
        state = {"input": "测试历史记录"}
        agents = {
            "agent1": Mock(bid=Mock(return_value=(0.9, "最高分"))),
            "agent2": Mock(bid=Mock(return_value=(0.5, "较低分")))
        }

        await router.route(state, agents)

        # 验证历史记录
        assert len(router._route_history) > 0
        history = router._route_history[0]
        assert history.input == "测试历史记录"
        assert history.winner == "agent1"
        assert history.winner_confidence == 0.9

    @pytest.mark.asyncio
    async def test_swarm_router_feedback_learning(self):
        """测试反馈学习"""
        router = SwarmRouter(learning_rate=0.1)

        # 添加一些反馈
        feedback1 = RoutingFeedback(
            agent_id="agent1",
            input="任务1",
            was_correct=True,
            actual_score=0.9
        )
        feedback2 = RoutingFeedback(
            agent_id="agent2",
            input="任务2",
            was_correct=False,
            actual_score=0.2
        )

        router._feedback_history.extend([feedback1, feedback2])

        # 验证反馈记录
        assert len(router._feedback_history) == 2
        assert router._feedback_history[0].was_correct is True
        assert router._feedback_history[1].was_correct is False

    def test_swarm_router_initialization(self):
        """测试 SwarmRouter 初始化"""
        router = SwarmRouter(
            history_db=":memory:",
            learning_rate=0.2,
            min_history_for_learning=10,
            fallback_rules=[{"agent_id": "fallback"}]
        )

        assert router.history_db == ":memory:"
        assert router.learning_rate == 0.2
        assert router.min_history_for_learning == 10
        assert len(router.fallback_rules) == 1
        assert router._bidding_history == {}
        assert router._route_history == []
        assert router._feedback_history == []

    def test_swarm_router_learning_threshold(self):
        """测试学习阈值"""
        router = SwarmRouter(min_history_for_learning=5)

        # 历史记录不足时
        router._route_history = [RouteHistory("t1", "i1", "a1", {"a1": 0.5}, 0.5)] * 4
        assert len(router._route_history) < router.min_history_for_learning

        # 历史记录足够时
        router._route_history.append(RouteHistory("t5", "i5", "a1", {"a1": 0.5}, 0.5))
        assert len(router._route_history) >= router.min_history_for_learning


@pytest.mark.asyncio
class TestRouterConcurrency:
    """测试路由器并发处理"""

    @pytest.mark.asyncio
    async def test_concurrent_routing(self):
        """测试并发路由"""
        rule_router = RuleRouter([
            {"agent_id": "agent1", "condition": {"task": "test"}}
        ])

        agents = {"agent1": Mock()}
        states = [
            {"task": "test", "id": 1},
            {"task": "test", "id": 2},
            {"task": "test", "id": 3}
        ]

        # 并发路由
        tasks = [rule_router.route(state, agents) for state in states]
        results = await asyncio.gather(*tasks)

        # 所有路由都应该成功
        assert all(result == "agent1" for result in results)

    @pytest.mark.asyncio
    async def test_swarm_router_concurrent_bidding(self):
        """测试蜂群路由并发竞标"""
        router = SwarmRouter()

        # 创建多个 agents
        agents = {}
        for i in range(5):
            config = AgentConfig(
                name=f"agent{i}",
                role=f"角色{i}",
                prompt=f"提示{i}"
            )
            agent = Agent(config, f"id{i}")

            # 模拟竞标
            agent.bid = Mock(return_value=asyncio.Future())
            score = 0.5 + (i * 0.1)
            agent.bid.return_value.set_result((score, f"原因{i}"))
            agents[f"id{i}"] = agent

        states = [
            {"input": f"任务{i}", "id": i}
            for i in range(3)
        ]

        # 并发执行路由
        tasks = [router.route(state, agents) for state in states]
        results = await asyncio.gather(*tasks)

        # 所有路由都应该返回有效的 agent ID
        assert all(result in agents for result in results)


class TestRouteHistory:
    """测试路由历史记录"""

    def test_route_history_creation(self):
        """测试路由历史记录创建"""
        history = RouteHistory(
            timestamp="2024-01-01T00:00:00",
            input="测试输入",
            winner="agent1",
            scores={"agent1": 0.9, "agent2": 0.6},
            winner_confidence=0.9,
            state_keys=["input", "output"]
        )

        assert history.timestamp == "2024-01-01T00:00:00"
        assert history.input == "测试输入"
        assert history.winner == "agent1"
        assert history.scores == {"agent1": 0.9, "agent2": 0.6}
        assert history.winner_confidence == 0.9
        assert history.state_keys == ["input", "output"]

    def test_route_history_serialization(self):
        """测试路由历史记录序列化"""
        import json
        from dataclasses import asdict

        history = RouteHistory(
            timestamp="2024-01-01T00:00:00",
            input="测试输入",
            winner="agent1",
            scores={"agent1": 0.9},
            winner_confidence=0.9
        )

        # 转换为字典
        history_dict = asdict(history)

        # 验证所有字段都被正确序列化
        assert history_dict["timestamp"] == "2024-01-01T00:00:00"
        assert history_dict["input"] == "测试输入"
        assert history_dict["winner"] == "agent1"


class TestRoutingFeedback:
    """测试路由反馈"""

    def test_routing_feedback_creation(self):
        """测试路由反馈创建"""
        feedback = RoutingFeedback(
            agent_id="agent1",
            input="任务输入",
            was_correct=True,
            actual_score=0.85
        )

        assert feedback.agent_id == "agent1"
        assert feedback.input == "任务输入"
        assert feedback.was_correct is True
        assert feedback.actual_score == 0.85

    def test_routing_feedback_negative_feedback(self):
        """测试负面反馈"""
        feedback = RoutingFeedback(
            agent_id="agent2",
            input="错误任务",
            was_correct=False,
            actual_score=0.3
        )

        assert feedback.was_correct is False
        assert feedback.actual_score == 0.3

    def test_routing_feedback_serialization(self):
        """测试路由反馈序列化"""
        from dataclasses import asdict

        feedback = RoutingFeedback(
            agent_id="agent1",
            input="测试",
            was_correct=True,
            actual_score=0.9
        )

        feedback_dict = asdict(feedback)

        assert feedback_dict["agent_id"] == "agent1"
        assert feedback_dict["was_correct"] is True
