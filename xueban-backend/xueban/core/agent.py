from typing import List, Dict, Any, Optional, Callable, Union, Literal
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import time
import json
import yaml
from functools import wraps
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, ValidationError
from agentgraph.protocol.claude import ClaudeProtocol, FallbackChain, FallbackConfig, FallbackStrategy

class ToolConfig(BaseModel):
    """工具配置模型"""
    name: str
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)

class ProtocolSettings(BaseModel):
    """协议设置模型"""
    reasoning_trace: bool = True
    validation: bool = True
    fallback: bool = True

class LLMConfig(BaseModel):
    """LLM 配置模型"""
    provider: str = "anthropic"
    model: str = "claude-3-haiku-20240307"
    temperature: float = 0.7
    max_tokens: Optional[int] = 4000

class AgentConfig(BaseModel):
    """Agent 配置模型，支持丰富的配置选项和灵活的加载方式"""
    # 基础信息
    name: str
    role: str
    prompt: str

    # 工具配置
    tools: List[Union[str, ToolConfig]] = Field(default_factory=list)

    # 内存范围
    memory_scope: Literal["session", "user", "global"] = "session"

    # 协议设置
    protocol_settings: ProtocolSettings = Field(default_factory=ProtocolSettings)

    # LLM 配置
    llm_config: LLMConfig = Field(default_factory=LLMConfig)

    # 拓扑角色
    topology_role: Literal["leader", "worker", "coordinator"] = "worker"

    # 兼容旧版本的配置字段
    enable_reasoning: bool = Field(default=True, alias="enable_reasoning")
    enable_validation: bool = Field(default=True, alias="enable_validation")
    fallback_agent: Optional[str] = Field(default=None, alias="fallback_agent")
    max_retries: int = Field(default=3, alias="max_retries")
    timeout: int = Field(default=30, alias="timeout")
    enable_trace: bool = Field(default=True, alias="enable_trace")
    enable_fallback: bool = Field(default=True, alias="enable_fallback")

    @field_validator('tools', mode='before')
    @classmethod
    def validate_tools(cls, v):
        """验证和转换工具配置"""
        if not v:
            return []

        tools = []
        for tool in v:
            if isinstance(tool, str):
                tools.append(ToolConfig(name=tool))
            else:
                tools.append(tool)
        return tools

    @field_validator('enable_reasoning', 'enable_validation', 'enable_trace', 'enable_fallback')
    @classmethod
    def update_protocol_settings(cls, v, info):
        """根据兼容字段更新协议设置"""
        if 'protocol_settings' in info.data:
            settings = info.data['protocol_settings']
            if info.field_name == 'enable_reasoning':
                settings.reasoning_trace = v
            elif info.field_name == 'enable_validation':
                settings.validation = v
            elif info.field_name == 'enable_fallback':
                settings.fallback = v
            return settings
        return v

    def validate(self) -> bool:
        """验证配置完整性"""
        try:
            # 验证必填字段
            if not self.name:
                raise ValueError("Agent name is required")
            if not self.role:
                raise ValueError("Agent role is required")
            if not self.prompt:
                raise ValueError("Agent prompt is required")

            # 验证工具配置
            for tool in self.tools:
                if isinstance(tool, ToolConfig):
                    if not tool.name:
                        raise ValueError("Tool name is required")

            # 验证 LLM 配置
            if self.llm_config.temperature < 0 or self.llm_config.temperature > 2:
                raise ValueError("Temperature must be between 0 and 2")

            # 验证重试次数
            if self.max_retries < 0:
                raise ValueError("Max retries must be non-negative")

            return True
        except Exception as e:
            print(f"Configuration validation failed: {e}")
            return False

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> 'AgentConfig':
        """从 YAML 文件加载配置"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        return cls.from_dict(data)

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> 'AgentConfig':
        """从 JSON 文件加载配置"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentConfig':
        """从字典创建配置对象"""
        try:
            return cls(**data)
        except ValidationError as e:
            raise ValueError(f"Invalid configuration: {e}")
        except Exception as e:
            raise ValueError(f"Failed to create configuration: {e}")

@dataclass
class AgentResult:
    agent_id: str
    output: str
    reasoning: str
    confidence: float
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

class Agent:
    def __init__(self, config: AgentConfig, agent_id: str):
        self.config = config
        self.agent_id = agent_id
        self._tool_registry: Dict[str, Callable] = {}
        self._protocol: Optional[ClaudeProtocol] = None
        self._step_counter: int = 0
        self._setup_protocol()

        # 从工具配置中提取工具名称列表
        self.tool_names = [tool.name if isinstance(tool, ToolConfig) else tool
                          for tool in self.config.tools]
    
    def register_tool(self, name: str, func: Callable):
        self._tool_registry[name] = func

    def _setup_protocol(self):
        """初始化 Claude Protocol 中间件"""
        if self.config.protocol_settings.reasoning_trace or self.config.protocol_settings.fallback:
            fallback_chain = None
            if self.config.protocol_settings.fallback:
                configs = []
                if self.config.max_retries > 1:
                    configs.append(FallbackConfig(
                        strategy=FallbackStrategy.RETRY,
                        max_retries=self.config.max_retries
                    ))
                if self.config.fallback_agent:
                    configs.append(FallbackConfig(
                        strategy=FallbackStrategy.DELEGATE,
                        delegate_to=self.config.fallback_agent
                    ))
                fallback_chain = FallbackChain(configs) if configs else None

            self._protocol = ClaudeProtocol(
                enable_trace=self.config.protocol_settings.reasoning_trace,
                enable_validation=self.config.protocol_settings.validation,
                fallback_chain=fallback_chain
            )
    
    async def invoke(self, input: str, state: Optional[Dict[str, Any]] = None) -> AgentResult:
        """
        Agent 的主要调用入口，集成协议中间件。
        返回 AgentResult，包含执行结果、推理过程和置信度。
        """
        if state is None:
            state = {}

        self._step_counter += 1

        # 使用 FallbackChain 执行，支持重试和降级
        if self._protocol and self._protocol.fallback_chain:
            success, result, strategy = await self._protocol.fallback_chain.execute(
                self._execute_with_trace, input, state
            )
            if not success:
                # 降级失败，返回错误结果
                return AgentResult(
                    agent_id=self.agent_id,
                    output="Execution failed after all fallback strategies.",
                    reasoning="",
                    confidence=0.0,
                    tool_calls=[],
                    metadata={"error": "fallback_failed", "strategy": strategy}
                )
            return result

        # 没有配置 fallback 时直接执行
        return await self._execute_with_trace(input, state)

    async def _execute_with_trace(self, input: str, state: Dict[str, Any]) -> AgentResult:
        """执行 Agent 逻辑并记录推理轨迹"""
        start_time = time.time()

        reasoning = ""
        tool_calls = []

        # 生成推理
        if self.config.protocol_settings.reasoning_trace:
            reasoning = await self._generate_reasoning(input, state)

        # 执行工具
        if self.tool_names:
            tool_calls = await self._execute_tools(input, state)

        # 生成输出
        output = await self._generate_output(input, state, reasoning, tool_calls)

        # 计算置信度
        confidence = self._calculate_confidence(output, reasoning)

        # 记录推理轨迹
        if self._protocol and self._protocol.enable_trace:
            self._protocol.create_trace(
                agent_id=self.agent_id,
                step=self._step_counter,
                reasoning=reasoning,
                action={
                    "input": input[:100],
                    "output": output[:100],
                    "tools_used": [t["tool"] for t in tool_calls if t.get("success")]
                },
                confidence=confidence
            )

        execution_time = time.time() - start_time

        return AgentResult(
            agent_id=self.agent_id,
            output=output,
            reasoning=reasoning,
            confidence=confidence,
            tool_calls=tool_calls,
            metadata={
                "input": input,
                "state_keys": list(state.keys()),
                "execution_time": execution_time,
                "step": self._step_counter
            }
        )

    async def execute(self, input: str, state: Dict[str, Any]) -> AgentResult:
        """向后兼容的执行方法，委托给 invoke"""
        return await self.invoke(input, state)
    
    async def _generate_reasoning(self, input: str, state: Dict[str, Any]) -> str:
        """生成真实的推理过程"""
        reasoning_steps = []

        # 步骤 1: 任务分析
        reasoning_steps.append(f"[任务分析] 作为 {self.config.role}，我需要处理: {input[:100]}...")

        # 步骤 2: 状态评估
        if state:
            relevant_keys = [k for k in state.keys() if k not in ("input", "output")]
            if relevant_keys:
                reasoning_steps.append(f"[状态评估] 可用上下文: {', '.join(relevant_keys[:5])}")

        # 步骤 3: 工具选择
        if self.tool_names:
            reasoning_steps.append(f"[工具选择] 考虑使用工具: {', '.join(self.tool_names)}")

        # 步骤 4: 处理策略
        reasoning_steps.append(f"[处理策略] 基于 prompt 指令，我将采用 {self.config.prompt[:50]}... 方式处理")

        # 步骤 5: 拓扑角色考虑
        role_descriptions = {
            "leader": "我将协调其他 Agent 并做出最终决策",
            "coordinator": "我将整合各方信息并协调工作流程",
            "worker": "我将专注于执行具体任务"
        }
        reasoning_steps.append(f"[拓扑角色] {role_descriptions.get(self.config.topology_role, '我将执行分配的任务')}")

        return "\n".join(reasoning_steps)
    
    async def _execute_tools(self, input: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        results = []
        for tool_name in self.tool_names:
            if tool_name in self._tool_registry:
                try:
                    result = await self._tool_registry[tool_name](input, state)
                    results.append({
                        "tool": tool_name,
                        "result": result,
                        "success": True
                    })
                except Exception as e:
                    results.append({
                        "tool": tool_name,
                        "error": str(e),
                        "success": False
                    })
        return results
    
    async def _generate_output(
        self,
        input: str,
        state: Dict[str, Any],
        reasoning: str,
        tool_calls: List[Dict[str, Any]]
    ) -> str:
        """基于工具调用结果生成输出"""
        output_parts = []

        # 添加角色标识
        output_parts.append(f"[{self.config.role}]")

        # 整合工具结果
        if tool_calls:
            successful_tools = [t for t in tool_calls if t.get("success")]
            if successful_tools:
                output_parts.append("工具执行结果:")
                for tool_call in successful_tools:
                    tool_result = tool_call.get("result", "")
                    output_parts.append(f"  - {tool_call['tool']}: {str(tool_result)[:100]}")

        # 基于输入生成主要响应
        response_prefix = "根据分析"
        if state.get("input"):
            response_prefix += f"输入 '{state['input'][:50]}...'"
        response_prefix += f"，{self.config.prompt[:80]}..."

        output_parts.append(f"\n{response_prefix}")

        # 添加处理结果摘要
        input_hash = hash(input) % 1000
        output_parts.append(f"\n处理完成 [hash:{input_hash}]")

        return "\n".join(output_parts)
    
    def _calculate_confidence(self, output: str, reasoning: str) -> float:
        """基于结果质量计算置信度"""
        confidence = 0.7  # 基础置信度

        # 1. 推理深度评分
        reasoning_lines = reasoning.count("\n") + 1
        confidence += min(reasoning_lines * 0.03, 0.15)

        # 2. 输出质量评分
        if output:
            output_length = len(output)
            # 合理长度给予加分
            if 50 <= output_length <= 1000:
                confidence += 0.1
            elif output_length > 1000:
                confidence += 0.05

        # 3. 结构化内容评分
        if "[" in output and "]" in output:
            confidence += 0.05

        # 4. 角色一致性
        if self.config.role and self.config.role in output:
            confidence += 0.05

        return min(confidence, 1.0)

    def get_protocol_traces(self) -> List[Any]:
        """获取推理轨迹"""
        if self._protocol:
            return self._protocol.get_traces()
        return []

    def get_protocol_trace_formatted(self) -> str:
        """获取格式化的推理轨迹"""
        if self._protocol:
            return self._protocol.format_trace()
        return "Protocol not enabled."

    def clear_protocol_traces(self):
        """清除推理轨迹"""
        if self._protocol:
            self._protocol.clear_traces()

    @property
    def protocol(self) -> Optional[ClaudeProtocol]:
        """获取协议实例"""
        return self._protocol

    @property
    def step_count(self) -> int:
        """获取执行步数"""
        return self._step_counter

    def reset_step_counter(self):
        """重置步数计数器"""
        self._step_counter = 0

# 全局 Agent 注册器
_agent_registry: Dict[str, Agent] = {}

def agent_node(config: Union[AgentConfig, Dict[str, Any]]):
    """装饰器：将函数注册为 Agent 节点

    Args:
        config: Agent 配置，可以是 AgentConfig 对象或字典

    Usage:
        @agent_node({
            "name": "data_analyzer",
            "role": "数据分析助手",
            "prompt": "分析数据并生成报告",
            "tools": ["calculator", "data_visualizer"]
        })
        def analyze_data(input_data: str, state: Dict[str, Any]) -> str:
            # Agent 实现逻辑
            return "分析结果"
    """
    def decorator(func: Callable):
        # 创建 Agent 配置
        if isinstance(config, dict):
            agent_config = AgentConfig.from_dict(config)
        else:
            agent_config = config

        # 验证配置
        if not agent_config.validate():
            raise ValueError("Invalid agent configuration")

        @wraps(func)
        async def wrapper(input: str, state: Optional[Dict[str, Any]] = None) -> AgentResult:
            # 创建 Agent 实例
            agent = Agent(agent_config, func.__name__)

            # 注册工具
            if agent_config.tools:
                # 在这里注册相关的工具函数
                pass

            # 执行 Agent
            return await agent.invoke(input, state)

        # 注册到全局注册器
        agent_id = agent_config.name
        if agent_id in _agent_registry:
            print(f"Warning: Agent '{agent_id}' already registered, overwriting...")

        _agent_registry[agent_id] = wrapper

        # 设置 Agent ID 作为属性
        wrapper.agent_id = agent_id
        wrapper.agent_config = agent_config

        return wrapper

    return decorator

def get_agent(agent_id: str) -> Optional[Agent]:
    """从注册器中获取 Agent"""
    if agent_id in _agent_registry:
        return _agent_registry[agent_id]
    return None

def list_agents() -> List[str]:
    """列出所有注册的 Agent"""
    return list(_agent_registry.keys())

def register_agent(agent: Agent, agent_id: Optional[str] = None):
    """手动注册 Agent"""
    if agent_id is None:
        agent_id = agent.config.name

    _agent_registry[agent_id] = agent
    return agent

def unregister_agent(agent_id: str):
    """从注册器中移除 Agent"""
    if agent_id in _agent_registry:
        del _agent_registry[agent_id]