"""
蜂群拓扑系统实现
包含四种拓扑结构：层级、网格、环形、星形
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, List, Any, Set, TYPE_CHECKING
from dataclasses import dataclass, field
import json
import random
from enum import Enum

if TYPE_CHECKING:
    from .router import SwarmRouter, Router


class RouteStrategy(Enum):
    """路由策略枚举"""
    COMPETITIVE = "competitive"  # 竞标机制（默认）
    LEADER_DELEGATE = "leader_delegate"  # Leader 分配
    P2P_BIDDING = "p2p_bidding"  # P2P 竞标
    ROUND_ROBIN = "round_robin"  # 轮询
    COORDINATOR = "coordinator"  # 协调者分配


@dataclass
class TopologyConfig:
    """拓扑配置"""
    name: str
    agents: List[str]
    strategy: RouteStrategy = RouteStrategy.COMPETITIVE
    metadata: Dict[str, Any] = field(default_factory=dict)


class Topology(ABC):
    """拓扑基类"""

    def __init__(self, config: TopologyConfig):
        self.config = config
        self._router: Optional[Router] = None
        self._initialize_router()

    @abstractmethod
    def get_next_agent(self, current_id: str, state: Dict) -> str:
        """获取下一个要执行的 Agent"""
        pass

    @abstractmethod
    def get_route_strategy(self) -> RouteStrategy:
        """获取路由策略"""
        pass

    @abstractmethod
    def to_mermaid(self) -> str:
        """生成 Mermaid 图"""
        pass

    def _initialize_router(self):
        """初始化路由器"""
        if self.get_route_strategy() == RouteStrategy.COMPETITIVE:
            # 延迟导入避免循环依赖
            from .router import SwarmRouter
            self._router = SwarmRouter()

    def get_router(self) -> Optional["Router"]:
        """获取路由器"""
        return self._router

    def get_all_agents(self) -> Set[str]:
        """获取所有 Agent ID"""
        return set(self.config.agents)

    def is_agent_valid(self, agent_id: str) -> bool:
        """检查 Agent 是否在拓扑中"""
        return agent_id in self.config.agents


class HierarchicalTopology(Topology):
    """层级拓扑 - Leader-Worker 模式"""

    def __init__(self, config: TopologyConfig, leader_id: str):
        self.leader_id = leader_id
        super().__init__(config)
        self._validate_config()

    def _validate_config(self):
        """验证配置"""
        if self.leader_id not in self.config.agents:
            raise ValueError(f"Leader ID {self.leader_id} must be in agents list")

    def get_next_agent(self, current_id: str, state: Dict) -> str:
        """获取下一个要执行的 Agent"""
        # 如果当前是 Leader，选择一个 Worker
        if current_id == self.leader_id:
            available_workers = [agent for agent in self.config.agents if agent != self.leader_id]
            if available_workers:
                # 可以根据状态选择最合适的 Worker
                # 简单实现：随机选择
                return random.choice(available_workers)

        # 如果当前是 Worker，返回 Leader
        if current_id in self.config.agents and current_id != self.leader_id:
            return self.leader_id

        # 其他情况，随机选择
        return random.choice(self.config.agents)

    def get_route_strategy(self) -> RouteStrategy:
        """获取路由策略"""
        return RouteStrategy.LEADER_DELEGATE

    def to_mermaid(self) -> str:
        """生成 Mermaid 图"""
        lines = [
            "graph TD",
            f"    {self.leader_id}[\"{self.leader_id} (Leader)\"]",
        ]

        # 添加 Worker 节点
        for agent in self.config.agents:
            if agent != self.leader_id:
                lines.append(f"    {agent}[\"{agent} (Worker)\"]")
                lines.append(f"    {self.leader_id} --> {agent}")

        return "\n".join(lines)


class MeshTopology(Topology):
    """网格拓扑 - 全连接 P2P 模式"""

    def __init__(self, config: TopologyConfig):
        super().__init__(config)
        self._agent_scores: Dict[str, float] = {agent: 0.5 for agent in config.agents}

    def get_next_agent(self, current_id: str, state: Dict) -> str:
        """获取下一个要执行的 Agent"""
        available_agents = [agent for agent in self.config.agents if agent != current_id]
        if not available_agents:
            return current_id

        # 使用竞标机制选择下一个 Agent
        if self._router and isinstance(self._router, SwarmRouter):
            # 使用 SwarmRouter 进行竞标
            return self._router.route(state, {
                agent_id: type('MockAgent', (), {'bid': lambda self, i, s: (self._agent_scores[agent_id], "P2P bidding")})()
                for agent_id in self.config.agents
            }, current_id) or random.choice(available_agents)

        # 简单实现：基于历史分数随机选择
        weights = [self._agent_scores[agent] for agent in available_agents]
        total_weight = sum(weights)
        if total_weight > 0:
            normalized_weights = [w / total_weight for w in weights]
            return random.choices(available_agents, weights=normalized_weights)[0]

        return random.choice(available_agents)

    def get_route_strategy(self) -> RouteStrategy:
        """获取路由策略"""
        return RouteStrategy.P2P_BIDDING

    def update_agent_score(self, agent_id: str, score: float):
        """更新 Agent 分数"""
        if agent_id in self._agent_scores:
            self._agent_scores[agent_id] = max(0.0, min(1.0, score))

    def to_mermaid(self) -> str:
        """生成 Mermaid 图"""
        lines = ["graph TD"]

        # 添加所有节点
        for i, agent in enumerate(self.config.agents):
            lines.append(f"    {agent}[\"{agent}\"]")

        # 添加全连接边
        for i, agent1 in enumerate(self.config.agents):
            for j, agent2 in enumerate(self.config.agents):
                if i < j:  # 避免重复边
                    lines.append(f"    {agent1} -. {agent2}")
                    lines.append(f"    {agent2} -. {agent1}")

        return "\n".join(lines)


class RingTopology(Topology):
    """环形拓扑 - 循环传递模式"""

    def __init__(self, config: TopologyConfig, ring_order: Optional[List[str]] = None):
        self.ring_order = ring_order or config.agents.copy()
        super().__init__(config)
        self._validate_config()
        self._build_ring_indices()

    def _validate_config(self):
        """验证配置"""
        if set(self.ring_order) != set(self.config.agents):
            raise ValueError("ring_order must contain all agents exactly once")

    def _build_ring_indices(self):
        """构建环形索引"""
        self._agent_to_index = {agent: i for i, agent in enumerate(self.ring_order)}
        self._index_to_agent = {i: agent for i, agent in enumerate(self.ring_order)}

    def get_next_agent(self, current_id: str, state: Dict) -> str:
        """获取下一个要执行的 Agent"""
        if current_id not in self._agent_to_index:
            return random.choice(self.config.agents)

        current_index = self._agent_to_index[current_id]
        next_index = (current_index + 1) % len(self.ring_order)
        return self._index_to_agent[next_index]

    def get_route_strategy(self) -> RouteStrategy:
        """获取路由策略"""
        return RouteStrategy.ROUND_ROBIN

    def to_mermaid(self) -> str:
        """生成 Mermaid 图"""
        lines = ["graph TD"]

        # 添加节点
        for i, agent in enumerate(self.ring_order):
            lines.append(f"    {agent}[\"{agent}\"]")

        # 添加环形边
        for i in range(len(self.ring_order)):
            current_agent = self.ring_order[i]
            next_agent = self.ring_order[(i + 1) % len(self.ring_order)]
            lines.append(f"    {current_agent} --> {next_agent}")

        return "\n".join(lines)


class StarTopology(Topology):
    """星形拓扑 - 中心协调模式"""

    def __init__(self, config: TopologyConfig, coordinator_id: str):
        self.coordinator_id = coordinator_id
        super().__init__(config)
        self._validate_config()

    def _validate_config(self):
        """验证配置"""
        if self.coordinator_id not in self.config.agents:
            raise ValueError(f"Coordinator ID {self.coordinator_id} must be in agents list")

    def get_next_agent(self, current_id: str, state: Dict) -> str:
        """获取下一个要执行的 Agent"""
        # 如果当前是 Coordinator，选择一个普通 Agent
        if current_id == self.coordinator_id:
            regular_agents = [agent for agent in self.config.agents if agent != self.coordinator_id]
            if regular_agents:
                # 可以根据状态选择最合适的 Agent
                # 简单实现：随机选择
                return random.choice(regular_agents)

        # 如果当前是普通 Agent，返回 Coordinator
        if current_id in self.config.agents and current_id != self.coordinator_id:
            return self.coordinator_id

        # 其他情况，随机选择
        return random.choice(self.config.agents)

    def get_route_strategy(self) -> RouteStrategy:
        """获取路由策略"""
        return RouteStrategy.COORDINATOR

    def to_mermaid(self) -> str:
        """生成 Mermaid 图"""
        lines = [
            "graph TD",
            f"    {self.coordinator_id}[\"{self.coordinator_id} (Coordinator)\"]",
        ]

        # 添加普通节点
        for agent in self.config.agents:
            if agent != self.coordinator_id:
                lines.append(f"    {agent}[\"{agent}\"]")
                lines.append(f"    {self.coordinator_id} <--> {agent}")

        return "\n".join(lines)


class TopologyFactory:
    """拓扑工厂类"""

    @staticmethod
    def create_hierarchical(agents: List[str], leader_id: str, **kwargs) -> HierarchicalTopology:
        """创建层级拓扑"""
        config = TopologyConfig(
            name="hierarchical",
            agents=agents,
            strategy=RouteStrategy.LEADER_DELEGATE,
            metadata=kwargs
        )
        return HierarchicalTopology(config, leader_id)

    @staticmethod
    def create_mesh(agents: List[str], **kwargs) -> MeshTopology:
        """创建网格拓扑"""
        config = TopologyConfig(
            name="mesh",
            agents=agents,
            strategy=RouteStrategy.P2P_BIDDING,
            metadata=kwargs
        )
        return MeshTopology(config)

    @staticmethod
    def create_ring(agents: List[str], ring_order: Optional[List[str]] = None, **kwargs) -> RingTopology:
        """创建环形拓扑"""
        config = TopologyConfig(
            name="ring",
            agents=agents,
            strategy=RouteStrategy.ROUND_ROBIN,
            metadata=kwargs
        )
        return RingTopology(config, ring_order)

    @staticmethod
    def create_star(agents: List[str], coordinator_id: str, **kwargs) -> StarTopology:
        """创建星形拓扑"""
        config = TopologyConfig(
            name="star",
            agents=agents,
            strategy=RouteStrategy.COORDINATOR,
            metadata=kwargs
        )
        return StarTopology(config, coordinator_id)


def create_topology_from_dict(config_dict: Dict[str, Any]) -> Topology:
    """从字典创建拓扑实例"""
    topology_type = config_dict["type"]
    agents = config_dict["agents"]

    if topology_type == "hierarchical":
        return TopologyFactory.create_hierarchical(
            agents=agents,
            leader_id=config_dict["leader_id"],
            **config_dict.get("metadata", {})
        )
    elif topology_type == "mesh":
        return TopologyFactory.create_mesh(
            agents=agents,
            **config_dict.get("metadata", {})
        )
    elif topology_type == "ring":
        return TopologyFactory.create_ring(
            agents=agents,
            ring_order=config_dict.get("ring_order"),
            **config_dict.get("metadata", {})
        )
    elif topology_type == "star":
        return TopologyFactory.create_star(
            agents=agents,
            coordinator_id=config_dict["coordinator_id"],
            **config_dict.get("metadata", {})
        )
    else:
        raise ValueError(f"Unknown topology type: {topology_type}")