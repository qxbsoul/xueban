"""
Topology 类单元测试
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock

from agentgraph.core.topology import (
    Topology, TopologyConfig, HierarchicalTopology, MeshTopology,
    RingTopology, StarTopology, TopologyFactory, RouteStrategy,
    create_topology_from_dict
)

pytestmark = pytest.mark.legacy


class TestHierarchicalTopology:
    """测试层级拓扑"""

    @pytest.fixture
    def hierarchical_config(self):
        """创建层级拓扑配置"""
        return TopologyConfig(
            name="hierarchical_test",
            agents=["leader1", "worker1", "worker2", "worker3"],
            strategy=RouteStrategy.LEADER_DELEGATE
        )

    @pytest.fixture
    def hierarchical_topology(self, hierarchical_config):
        """创建层级拓扑实例"""
        return HierarchicalTopology(hierarchical_config, "leader1")

    def test_hierarchical_topology_creation(self, hierarchical_topology):
        """测试层级拓扑创建"""
        assert hierarchical_topology.config.name == "hierarchical_test"
        assert hierarchical_topology.leader_id == "leader1"
        assert hierarchical_topology.is_agent_valid("leader1") is True
        assert hierarchical_topology.is_agent_valid("worker1") is True
        assert hierarchical_topology.is_agent_valid("unknown") is False

    def test_hierarchical_topology_invalid_leader(self, hierarchical_config):
        """测试无效的 Leader ID"""
        with pytest.raises(ValueError, match="Leader ID invalid_leader must be in agents list"):
            HierarchicalTopology(hierarchical_config, "invalid_leader")

    @pytest.mark.asyncio
    async def test_hierarchical_topology_leader_to_worker(self, hierarchical_topology):
        """测试从 Leader 选择 Worker"""
        # Mock random.choice
        with patch('random.choice', return_value="worker2") as mock_choice:
            next_agent = hierarchical_topology.get_next_agent("leader1", {"task": "test"})
            assert next_agent == "worker2"
            mock_choice.assert_called_once_with(["worker1", "worker2", "worker3"])

    @pytest.mark.asyncio
    async def test_hierarchical_topology_worker_to_leader(self, hierarchical_topology):
        """测试从 Worker 返回 Leader"""
        next_agent = hierarchical_topology.get_next_agent("worker1", {"task": "test"})
        assert next_agent == "leader1"

    @pytest.mark.asyncio
    async def test_hierarchical_topology_unknown_current(self, hierarchical_topology):
        """测试未知当前 Agent"""
        with patch('random.choice', return_value="leader1") as mock_choice:
            next_agent = hierarchical_topology.get_next_agent("unknown", {"task": "test"})
            assert next_agent == "leader1"
            mock_choice.assert_called_once_with(["leader1", "worker1", "worker2", "worker3"])

    def test_hierarchical_topology_mermaid(self, hierarchical_topology):
        """测试生成 Mermaid 图"""
        mermaid = hierarchical_topology.to_mermaid()

        assert "graph TD" in mermaid
        assert "leader1[\"leader1 (Leader)\"]" in mermaid
        assert "worker1[\"worker1 (Worker)\"]" in mermaid
        assert "leader1 --> worker1" in mermaid
        assert "leader1 --> worker2" in mermaid
        assert "leader1 --> worker3" in mermaid

    def test_hierarchical_topology_route_strategy(self, hierarchical_topology):
        """测试路由策略"""
        assert hierarchical_topology.get_route_strategy() == RouteStrategy.LEADER_DELEGATE


class TestMeshTopology:
    """测试网格拓扑"""

    @pytest.fixture
    def mesh_config(self):
        """创建网格拓扑配置"""
        return TopologyConfig(
            name="mesh_test",
            agents=["agent1", "agent2", "agent3"],
            strategy=RouteStrategy.P2P_BIDDING
        )

    @pytest.fixture
    def mesh_topology(self, mesh_config):
        """创建网格拓扑实例"""
        return MeshTopology(mesh_config)

    def test_mesh_topology_creation(self, mesh_topology):
        """测试网格拓扑创建"""
        assert mesh_topology.config.name == "mesh_test"
        assert mesh_topology.get_all_agents() == {"agent1", "agent2", "agent3"}
        assert mesh_topology._agent_scores == {
            "agent1": 0.5,
            "agent2": 0.5,
            "agent3": 0.5
        }

    @pytest.mark.asyncio
    @patch('random.choices')
    @patch('random.choice')
    async def test_mesh_topology_next_agent(self, mock_random_choice, mock_random_choices, mesh_topology):
        """测试网格拓扑获取下一个 Agent"""
        # 当没有路由器时
        mesh_topology._router = None

        mock_random_choice.return_value = "agent2"
        next_agent = mesh_topology.get_next_agent("agent1", {"task": "test"})
        assert next_agent == "agent2"

    @pytest.mark.asyncio
    async def test_mesh_topology_with_router(self, mesh_topology):
        """测试带路由器的网格拓扑"""
        # Mock SwarmRouter
        mock_router = Mock()
        mock_router.route = AsyncMock(return_value="agent2")
        mesh_topology._router = mock_router

        next_agent = await mesh_topology.get_next_agent("agent1", {"task": "test"})
        assert next_agent == "agent2"
        mock_router.route.assert_called_once()

    def test_mesh_topology_update_score(self, mesh_topology):
        """测试更新 Agent 分数"""
        mesh_topology.update_agent_score("agent1", 0.8)
        mesh_topology.update_agent_score("agent2", 1.2)  # 应该被限制在 1.0

        assert mesh_topology._agent_scores["agent1"] == 0.8
        assert mesh_topology._agent_scores["agent2"] == 1.0

    def test_mesh_topology_mermaid(self, mesh_topology):
        """测试生成 Mermaid 图"""
        mermaid = mesh_topology.to_mermaid()

        assert "graph TD" in mermaid
        assert "agent1[\"agent1\"]" in mermaid
        assert "agent2[\"agent2\"]" in mermaid
        assert "agent3[\"agent3\"]" in mermaid
        assert "agent1 -. agent2" in mermaid
        assert "agent2 -. agent1" in mermaid
        assert "agent1 -. agent3" in mermaid

    def test_mesh_topology_route_strategy(self, mesh_topology):
        """测试路由策略"""
        assert mesh_topology.get_route_strategy() == RouteStrategy.P2P_BIDDING


class TestRingTopology:
    """测试环形拓扑"""

    @pytest.fixture
    def ring_config(self):
        """创建环形拓扑配置"""
        return TopologyConfig(
            name="ring_test",
            agents=["agent1", "agent2", "agent3", "agent4"],
            strategy=RouteStrategy.ROUND_ROBIN
        )

    @pytest.fixture
    def ring_topology(self, ring_config):
        """创建环形拓扑实例"""
        return RingTopology(ring_config, ["agent1", "agent2", "agent3", "agent4"])

    def test_ring_topology_creation(self, ring_topology):
        """测试环形拓扑创建"""
        assert ring_topology.config.name == "ring_test"
        assert ring_topology.ring_order == ["agent1", "agent2", "agent3", "agent4"]
        assert ring_topology._agent_to_index == {
            "agent1": 0,
            "agent2": 1,
            "agent3": 2,
            "agent4": 3
        }

    def test_ring_topology_invalid_order(self, ring_config):
        """测试无效的环形顺序"""
        with pytest.raises(ValueError, match="ring_order must contain all agents exactly once"):
            RingTopology(ring_config, ["agent1", "agent2"])  # 缺少 agent3 和 agent4

    @pytest.mark.asyncio
    async def test_ring_topology_next_agent_sequence(self, ring_topology):
        """测试环形拓扑 Agent 序列"""
        # agent1 -> agent2
        next_agent = ring_topology.get_next_agent("agent1", {"task": "test"})
        assert next_agent == "agent2"

        # agent2 -> agent3
        next_agent = ring_topology.get_next_agent("agent2", {"task": "test"})
        assert next_agent == "agent3"

        # agent3 -> agent4
        next_agent = ring_topology.get_next_agent("agent3", {"task": "test"})
        assert next_agent == "agent4"

        # agent4 -> agent1 (循环)
        next_agent = ring_topology.get_next_agent("agent4", {"task": "test"})
        assert next_agent == "agent1"

    @pytest.mark.asyncio
    async def test_ring_topology_unknown_current(self, ring_topology):
        """测试未知当前 Agent"""
        with patch('random.choice', return_value="agent1") as mock_choice:
            next_agent = ring_topology.get_next_agent("unknown", {"task": "test"})
            assert next_agent == "agent1"
            mock_choice.assert_called_once_with(["agent1", "agent2", "agent3", "agent4"])

    def test_ring_topology_mermaid(self, ring_topology):
        """测试生成 Mermaid 图"""
        mermaid = ring_topology.to_mermaid()

        assert "graph TD" in mermaid
        assert "agent1[\"agent1\"]" in mermaid
        assert "agent1 --> agent2" in mermaid
        assert "agent2 --> agent3" in mermaid
        assert "agent3 --> agent4" in mermaid
        assert "agent4 --> agent1" in mermaid

    def test_ring_topology_route_strategy(self, ring_topology):
        """测试路由策略"""
        assert ring_topology.get_route_strategy() == RouteStrategy.ROUND_ROBIN


class TestStarTopology:
    """测试星形拓扑"""

    @pytest.fixture
    def star_config(self):
        """创建星形拓扑配置"""
        return TopologyConfig(
            name="star_test",
            agents=["coordinator", "agent1", "agent2", "agent3"],
            strategy=RouteStrategy.COORDINATOR
        )

    @pytest.fixture
    def star_topology(self, star_config):
        """创建星形拓扑实例"""
        return StarTopology(star_config, "coordinator")

    def test_star_topology_creation(self, star_topology):
        """测试星形拓扑创建"""
        assert star_topology.config.name == "star_test"
        assert star_topology.coordinator_id == "coordinator"
        assert star_topology.is_agent_valid("coordinator") is True
        assert star_topology.is_agent_valid("agent1") is True

    def test_star_topology_invalid_coordinator(self, star_config):
        """测试无效的 Coordinator ID"""
        with pytest.raises(ValueError, match="Coordinator ID invalid_coordinator must be in agents list"):
            StarTopology(star_config, "invalid_coordinator")

    @pytest.mark.asyncio
    @patch('random.choice')
    async def test_star_topology_coordinator_to_agent(self, mock_choice, star_topology):
        """测试从 Coordinator 选择 Agent"""
        mock_choice.return_value = "agent2"

        next_agent = star_topology.get_next_agent("coordinator", {"task": "test"})
        assert next_agent == "agent2"
        mock_choice.assert_called_once_with(["agent1", "agent2", "agent3"])

    @pytest.mark.asyncio
    async def test_star_topology_agent_to_coordinator(self, star_topology):
        """测试从 Agent 返回 Coordinator"""
        next_agent = star_topology.get_next_agent("agent1", {"task": "test"})
        assert next_agent == "coordinator"

    @pytest.mark.asyncio
    async def test_star_topology_unknown_current(self, star_topology):
        """测试未知当前 Agent"""
        with patch('random.choice', return_value="coordinator") as mock_choice:
            next_agent = star_topology.get_next_agent("unknown", {"task": "test"})
            assert next_agent == "coordinator"
            mock_choice.assert_called_once_with(["coordinator", "agent1", "agent2", "agent3"])

    def test_star_topology_mermaid(self, star_topology):
        """测试生成 Mermaid 图"""
        mermaid = star_topology.to_mermaid()

        assert "graph TD" in mermaid
        assert "coordinator[\"coordinator (Coordinator)\"]" in mermaid
        assert "agent1[\"agent1\"]" in mermaid
        assert "coordinator <--> agent1" in mermaid
        assert "coordinator <--> agent2" in mermaid
        assert "coordinator <--> agent3" in mermaid

    def test_star_topology_route_strategy(self, star_topology):
        """测试路由策略"""
        assert star_topology.get_route_strategy() == RouteStrategy.COORDINATOR


class TestTopologyFactory:
    """测试拓扑工厂"""

    def test_create_hierarchical(self):
        """测试创建层级拓扑"""
        topology = TopologyFactory.create_hierarchical(
            agents=["leader", "worker1", "worker2"],
            leader_id="leader",
            custom_param="value"
        )

        assert isinstance(topology, HierarchicalTopology)
        assert topology.leader_id == "leader"
        assert topology.config.metadata["custom_param"] == "value"

    def test_create_mesh(self):
        """测试创建网格拓扑"""
        topology = TopologyFactory.create_mesh(
            agents=["agent1", "agent2", "agent3"],
            param1="value1"
        )

        assert isinstance(topology, MeshTopology)
        assert topology.config.metadata["param1"] == "value1"

    def test_create_ring(self):
        """测试创建环形拓扑"""
        topology = TopologyFactory.create_ring(
            agents=["a", "b", "c"],
            ring_order=["a", "b", "c"],
            param2="value2"
        )

        assert isinstance(topology, RingTopology)
        assert topology.ring_order == ["a", "b", "c"]
        assert topology.config.metadata["param2"] == "value2"

    def test_create_star(self):
        """测试创建星形拓扑"""
        topology = TopologyFactory.create_star(
            agents=["coordinator", "agent1", "agent2"],
            coordinator_id="coordinator",
            param3="value3"
        )

        assert isinstance(topology, StarTopology)
        assert topology.coordinator_id == "coordinator"
        assert topology.config.metadata["param3"] == "value3"


class TestCreateTopologyFromDict:
    """测试从字典创建拓扑"""

    def test_create_hierarchical_from_dict(self):
        """测试从字典创建层级拓扑"""
        config = {
            "type": "hierarchical",
            "agents": ["leader", "worker1", "worker2"],
            "leader_id": "leader",
            "metadata": {"custom": "value"}
        }

        topology = create_topology_from_dict(config)
        assert isinstance(topology, HierarchicalTopology)
        assert topology.leader_id == "leader"
        assert topology.config.metadata["custom"] == "value"

    def test_create_mesh_from_dict(self):
        """测试从字典创建网格拓扑"""
        config = {
            "type": "mesh",
            "agents": ["a", "b", "c"],
            "metadata": {"param": "value"}
        }

        topology = create_topology_from_dict(config)
        assert isinstance(topology, MeshTopology)
        assert topology.config.metadata["param"] == "value"

    def test_create_ring_from_dict(self):
        """测试从字典创建环形拓扑"""
        config = {
            "type": "ring",
            "agents": ["a", "b", "c"],
            "ring_order": ["a", "b", "c"],
            "metadata": {"param": "value"}
        }

        topology = create_topology_from_dict(config)
        assert isinstance(topology, RingTopology)
        assert topology.ring_order == ["a", "b", "c"]

    def test_create_star_from_dict(self):
        """测试从字典创建星形拓扑"""
        config = {
            "type": "star",
            "agents": ["coordinator", "agent1", "agent2"],
            "coordinator_id": "coordinator",
            "metadata": {"param": "value"}
        }

        topology = create_topology_from_dict(config)
        assert isinstance(topology, StarTopology)
        assert topology.coordinator_id == "coordinator"

    def test_create_unknown_topology_type(self):
        """测试创建未知拓扑类型"""
        config = {
            "type": "unknown",
            "agents": ["a", "b"]
        }

        with pytest.raises(ValueError, match="Unknown topology type: unknown"):
            create_topology_from_dict(config)


class TestBaseTopology:
    """测试基础拓扑功能"""

    @pytest.fixture
    def base_config(self):
        """创建基础配置"""
        return TopologyConfig(
            name="base_test",
            agents=["agent1", "agent2", "agent3"],
            strategy=RouteStrategy.COMPETITIVE
        )

    def test_topology_base_methods(self, base_config):
        """测试拓扑基类方法"""
        # 创建一个具体的拓扑实例来测试基类方法
        topology = HierarchicalTopology(base_config, "agent1")

        # 测试获取所有 Agent
        agents = topology.get_all_agents()
        assert agents == {"agent1", "agent2", "agent3"}

        # 测试检查 Agent 有效性
        assert topology.is_agent_valid("agent1") is True
        assert topology.is_agent_valid("unknown") is False

    def test_topology_router_initialization(self, base_config):
        """测试路由器初始化"""
        # 使用竞标策略的拓扑应该初始化路由器
        topology = MeshTopology(base_config)
        assert topology._router is not None

    def test_topology_router_none_for_non_competitive(self):
        """测试非竞标策略路由器为 None"""
        config = TopologyConfig(
            name="test",
            agents=["a", "b"],
            strategy=RouteStrategy.ROUND_ROBIN
        )

        topology = RingTopology(config, ["a", "b"])
        assert topology._router is None

    def test_topology_get_router(self, base_config):
        """测试获取路由器"""
        topology = HierarchicalTopology(base_config, "agent1")
        router = topology.get_router()
        # 层级拓扑没有路由器
        assert router is None


@pytest.mark.asyncio
class TestTopologyIntegration:
    """测试拓扑集成功能"""

    @pytest.mark.asyncio
    async def test_topologies_sequence_execution(self):
        """测试拓扑序列执行"""
        agents = ["a", "b", "c", "d"]

        # 创建不同的拓扑
        hierarchical = TopologyFactory.create_hierarchical(agents, "a")
        ring = TopologyFactory.create_ring(agents, ["a", "b", "c", "d"])
        star = TopologyFactory.create_star(agents, "b")

        states = [{"step": i} for i in range(5)]

        # 测试层级拓扑
        path = []
        current = "a"
        for state in states[:2]:
            current = hierarchical.get_next_agent(current, state)
            path.append(current)

        assert len(path) == 2
        assert path[0] in ["b", "c", "d"]  # 从 a 选择一个 worker

        # 测试环形拓扑
        current = "a"
        for state in states[:2]:
            current = ring.get_next_agent(current, state)
            path.append(current)

        assert len(path) == 4
        # 环形应该按顺序: a->b->c
        assert path[2] == "b"
        assert path[3] == "c"

        # 测试星形拓扑
        current = "b"
        for state in states[:2]:
            current = star.get_next_agent(current, state)
            path.append(current)

        assert len(path) == 6
        assert path[4] == "b"  # 从 agent 返回 coordinator
        assert path[5] in ["a", "c", "d"]  # 从 coordinator 选择一个 agent

    @pytest.mark.asyncio
    async def test_topology_route_strategies(self):
        """测试不同路由策略"""
        agents = ["a", "b", "c"]

        # 创建不同策略的拓扑
        hierarchical = TopologyFactory.create_hierarchical(agents, "a")
        mesh = TopologyFactory.create_mesh(agents)
        ring = TopologyFactory.create_ring(agents)
        star = TopologyFactory.create_star(agents, "a")

        # 验证路由策略
        assert hierarchical.get_route_strategy() == RouteStrategy.LEADER_DELEGATE
        assert mesh.get_route_strategy() == RouteStrategy.P2P_BIDDING
        assert ring.get_route_strategy() == RouteStrategy.ROUND_ROBIN
        assert star.get_route_strategy() == RouteStrategy.COORDINATOR
