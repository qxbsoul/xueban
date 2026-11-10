from abc import ABC, abstractmethod
from typing import Dict, Optional, List, Any, Callable, TYPE_CHECKING
import random
import json
import os
from datetime import datetime
from dataclasses import dataclass, field

from agentgraph.core.agent import Agent

if TYPE_CHECKING:
    from agentgraph.core.topology import Topology, TopologyConfig, RouteStrategy


class Router(ABC):
    @abstractmethod
    async def route(
        self, 
        state: Dict, 
        agents: Dict[str, Agent], 
        current_id: Optional[str] = None
    ) -> Optional[str]:
        pass

class RuleRouter(Router):
    def __init__(self, rules: List[Dict[str, any]]):
        self.rules = rules
    
    async def route(
        self, 
        state: Dict, 
        agents: Dict[str, Agent], 
        current_id: Optional[str] = None
    ) -> Optional[str]:
        for rule in self.rules:
            if self._match_rule(rule, state, current_id):
                return rule["agent_id"]
        return None
    
    def _match_rule(
        self, 
        rule: Dict[str, any], 
        state: Dict, 
        current_id: Optional[str]
    ) -> bool:
        if "from_agent" in rule and rule["from_agent"] != current_id:
            return False
        
        condition = rule.get("condition", {})
        for key, value in condition.items():
            if state.get(key) != value:
                return False
        
        return True

class TopologyRouter(Router):
    """基于拓扑的路由器"""

    def __init__(self, topology: "Topology"):
        self.topology = topology
        self._current_route: Dict[str, Optional[str]] = {}  # 记录每个 Agent 的下一个路由
        self._route_history: List[Dict] = []

    async def route(
        self,
        state: Dict,
        agents: Dict[str, Agent],
        current_id: Optional[str] = None
    ) -> Optional[str]:
        """基于拓扑结构进行路由"""
        # 如果当前没有 Agent 或拓扑中没有配置 Agent，返回 None
        if not agents or not self.topology.agents:
            return None

        # 获取下一个 Agent
        next_agent = self.topology.get_next_agent(current_id or "", state)

        # 验证下一个 Agent 是否存在
        if next_agent in agents:
            # 记录路由历史
            self._record_route(current_id, next_agent, state)
            return next_agent

        # 默认：随机选择一个 Agent
        available_agents = list(agents.keys())
        return random.choice(available_agents)

    def _record_route(self, current_id: Optional[str], next_agent: str, state: Dict):
        """记录路由历史"""
        self._route_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "from_agent": current_id,
            "to_agent": next_agent,
            "input": state.get("input", "")[:200],
            "strategy": self.topology.get_route_strategy().value
        })

        # 限制历史记录数量
        if len(self._route_history) > 1000:
            self._route_history = self._route_history[-1000:]

    def get_route_history(self, limit: int = 50) -> List[Dict]:
        """获取路由历史"""
        return self._route_history[-limit:] if limit < len(self._route_history) else self._route_history.copy()

    def get_topology_summary(self) -> Dict[str, Any]:
        """获取拓扑摘要信息"""
        return {
            "name": self.topology.config.name,
            "type": self.topology.__class__.__name__,
            "strategy": self.topology.get_route_strategy().value,
            "agents": list(self.topology.get_all_agents()),
            "total_routes": len(self._route_history),
            "mermaid": self.topology.to_mermaid()
        }

    def reset_route_history(self):
        """重置路由历史"""
        self._route_history.clear()


class CompositeRouter(Router):
    """复合路由器 - 支持多个拓扑的切换"""

    def __init__(self):
        self.topologies: Dict[str, TopologyRouter] = {}
        self.current_topology_id: Optional[str] = None

    def add_topology(self, topology_id: str, topology: "Topology"):
        """添加拓扑"""
        self.topologies[topology_id] = TopologyRouter(topology)
        if self.current_topology_id is None:
            self.current_topology_id = topology_id

    def switch_topology(self, topology_id: str):
        """切换拓扑"""
        if topology_id in self.topologies:
            self.current_topology_id = topology_id
            return True
        return False

    async def route(
        self,
        state: Dict,
        agents: Dict[str, Agent],
        current_id: Optional[str] = None
    ) -> Optional[str]:
        """路由"""
        if self.current_topology_id and self.current_topology_id in self.topologies:
            return await self.topologies[self.current_topology_id].route(state, agents, current_id)
        return None

    def get_active_topology(self) -> Optional[TopologyRouter]:
        """获取当前活跃的拓扑"""
        if self.current_topology_id:
            return self.topologies.get(self.current_topology_id)
        return None

    def get_topology_ids(self) -> List[str]:
        """获取所有拓扑 ID"""
        return list(self.topologies.keys())

    def get_topology_summary(self, topology_id: Optional[str] = None) -> Dict[str, Any]:
        """获取拓扑摘要"""
        if topology_id:
            router = self.topologies.get(topology_id)
            if router:
                return router.get_topology_summary()
        else:
            summaries = {}
            for tid, router in self.topologies.items():
                summaries[tid] = router.get_topology_summary()
            return summaries