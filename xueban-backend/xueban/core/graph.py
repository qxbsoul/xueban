from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import asyncio
import logging
import re

from agentgraph.core.agent import Agent, AgentResult
from agentgraph.core.router import Router
from agentgraph.memory.base import Memory
from agentgraph.memory.layers import LayeredMemory, LayerType

logger = logging.getLogger(__name__)

@dataclass
class Edge:
    from_id: str
    to_id: str
    condition: Optional[str] = None
    edge_type: str = "default"

@dataclass
class GraphResult:
    output: str
    steps: List[AgentResult]
    metadata: Dict[str, Any] = field(default_factory=dict)

class AgentGraph:
    def __init__(
        self,
        memory: Optional[LayeredMemory] = None,
        router: Optional[Router] = None
    ):
        self.agents: Dict[str, Agent] = {}
        self.edges: List[Edge] = []
        self.memory = memory or LayeredMemory()  # 默认使用三层记忆
        self.router = router
        self._start_agent: Optional[str] = None
    
    def add_agent(self, agent: Agent):
        self.agents[agent.agent_id] = agent
        if not self._start_agent:
            self._start_agent = agent.agent_id
    
    def add_edge(self, edge: Edge):
        self.edges.append(edge)
    
    def set_start_agent(self, agent_id: str):
        if agent_id not in self.agents:
            raise ValueError(f"Agent {agent_id} not found")
        self._start_agent = agent_id
    
    async def invoke(self, input: str, user_id: str) -> GraphResult:
        state = {
            "input": input,
            "user_id": user_id,
            "history": [],
        }
        
        if self.memory:
            state["history"] = await self.memory.get_history(user_id)
        
        steps = []
        current_agent_id = self._start_agent
        
        max_steps = 100
        step_count = 0
        
        while current_agent_id and step_count < max_steps:
            agent = self.agents.get(current_agent_id)
            if not agent:
                break
            
            result = await agent.execute(input, state)
            steps.append(result)
            
            if self.memory:
                await self.memory.save_interaction(
                    user_id=user_id,
                    agent_id=agent.agent_id,
                    input=input,
                    output=result.output,
                    reasoning=result.reasoning,
                    confidence=result.confidence
                )
            
            current_agent_id = await self._find_next_agent(current_agent_id, result, state)
            step_count += 1
        
        final_output = steps[-1].output if steps else "No output generated"
        
        return GraphResult(
            output=final_output,
            steps=steps,
            metadata={
                "total_steps": len(steps),
                "user_id": user_id,
            }
        )
    
    async def _find_next_agent(
        self, 
        current_id: str, 
        result: AgentResult, 
        state: Dict[str, Any]
    ) -> Optional[str]:
        if self.router:
            return await self.router.route(state, self.agents, current_id)
        
        for edge in self.edges:
            if edge.from_id == current_id:
                if self._match_condition(edge.condition, result, state):
                    return edge.to_id
        
        return None
    
    def _match_condition(
        self,
        condition: Optional[str],
        result: AgentResult,
        state: Dict[str, Any]
    ) -> bool:
        if not condition:
            return True

        condition = condition.strip()

        # 处理组合条件 (&& 或 and)
        if '&&' in condition or ' and ' in condition:
            parts = re.split(r'\s*&&\s*|\s+and\s+', condition)
            return all(self._match_condition(part.strip(), result, state) for part in parts)

        # 处理对象前缀 (result.xxx 或 state.xxx)
        if '.' in condition:
            parts = condition.split('.', 1)
            obj_name = parts[0].strip()
            expr = parts[1].strip()

            if obj_name == 'result':
                return self._eval_expr(expr, result)
            elif obj_name == 'state':
                return self._eval_expr(expr, state)

        # 默认从 result 属性检查
        return self._eval_expr(condition, result.__dict__)

    def _eval_expr(self, expr: str, obj: Any) -> bool:
        """评估条件表达式

        支持的操作符:
        - 数值比较: >, >=, <, <=, ==, !=
        - 字符串包含: contains, includes
        """
        # 移除引号并处理空格
        expr = expr.strip()

        # 正则匹配: 属性名 操作符 值
        # 值可能带引号，也可能不带
        pattern = r'(\w+)\s*(>=|<=|>|<|==|!=|contains|includes)\s*[\'"]?(.+?)[\'"]?$'
        match = re.match(pattern, expr, re.IGNORECASE)

        if not match:
            return False

        attr, op, value = match.groups()
        attr = attr.strip()
        op = op.lower().strip()
        value = value.strip()

        # 获取属性值
        if isinstance(obj, dict):
            attr_value = obj.get(attr)
        else:
            attr_value = getattr(obj, attr, None)

        if attr_value is None:
            # 对于 contains/includes，None 值返回 False
            return False

        # 类型转换值
        try:
            if isinstance(attr_value, (int, float)):
                value = float(value)
            elif isinstance(attr_value, bool):
                value = value.lower() in ('true', '1', 'yes')
        except ValueError:
            pass

        # 根据操作符进行比较
        if op == '>':
            return attr_value > value
        elif op == '>=':
            return attr_value >= value
        elif op == '<':
            return attr_value < value
        elif op == '<=':
            return attr_value <= value
        elif op == '==':
            return attr_value == value
        elif op == '!=':
            return attr_value != value
        elif op in ('contains', 'includes'):
            return str(value).lower() in str(attr_value).lower()

        return False

    def to_mermaid(self) -> str:
        """
        生成 Mermaid 流程图代码

        Returns:
            Mermaid 格式的流程图字符串
        """
        lines = ["```mermaid", "graph TD"]

        # START 节点
        lines.append(f"    START((START))")

        # 添加所有 Agent 节点
        for agent_id, agent in self.agents.items():
            label = agent.config.name[:15]  # 截断长名称
            if agent_id == self._start_agent:
                lines.append(f"    START --> {agent_id}[\"{label}\"]")
            lines.append(f"    {agent_id}[\"{label}\"]")

        # 添加边
        for edge in self.edges:
            condition_label = f"|{edge.condition[:20]}|" if edge.condition else ""
            if edge.to_id == "END" or edge.to_id.upper() == "END":
                lines.append(f"    {edge.from_id} --> {condition_label} END((END))")
            else:
                lines.append(f"    {edge.from_id} --> {condition_label} {edge.to_id}")

        # 如果有 Router，添加路由节点
        if self.router:
            lines.insert(2, "    subgraph Router")
            lines.insert(3, f"    ROUTER{{\"{type(self.router).__name__}\"}}")
            lines.insert(4, "    end")

        lines.append("```")
        return "\n".join(lines)

    def draw(self) -> str:
        """
        生成 ASCII 艺术图（终端友好）

        Returns:
            ASCII 格式的流程图字符串
        """
        lines = []
        lines.append("┌─────────────────────────────────┐")
        lines.append("│         AgentGraph Flow          │")
        lines.append("└─────────────────────────────────┘")
        lines.append("")
        lines.append(f"START → [{self._start_agent}]")

        for edge in self.edges:
            arrow = f" --[{edge.condition}]--> " if edge.condition else " --> "
            lines.append(f"  {edge.from_id}{arrow}{edge.to_id}")

        if self.router:
            lines.append(f"\n[Router: {type(self.router).__name__}]")

        return "\n".join(lines)

    def save_diagram(self, filepath: str, format: str = "mermaid") -> None:
        """
        保存图表到文件

        Args:
            filepath: 文件路径
            format: "mermaid" 或 "ascii"
        """
        content = self.to_mermaid() if format == "mermaid" else self.draw()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)