import pytest
import asyncio
from agentgraph.core.state import AgentState, AgentContext, AgentStatus
from agentgraph.core.agent import Agent
from agentgraph.core.graph import AgentGraph

pytestmark = pytest.mark.legacy

@pytest.mark.asyncio
async def test_agent_state_initialization():
    """测试 Agent 状态初始化"""
    state = AgentState(agent_id="test-agent")

    assert state.agent_id == "test-agent"
    assert state.status == AgentStatus.IDLE
    assert state.memory == {}
    assert state.metrics == {}

@pytest.mark.asyncio
async def test_agent_state_update():
    """测试 Agent 状态更新"""
    state = AgentState(agent_id="test-agent")

    # 更新状态
    state.update_status(AgentStatus.RUNNING)
    assert state.status == AgentStatus.RUNNING

    # 记录指标
    state.record_metric("executions", 5)
    assert len(state.metrics["executions"]) == 1
    assert state.metrics["executions"][0]["value"] == 5

    # 设置记忆
    state.set_memory("test_key", {"type": "execution", "content": "test"})
    assert state.memory["test_key"]["type"] == "execution"

@pytest.mark.asyncio
async def test_agent_context():
    """测试 Agent 上下文"""
    context = AgentContext(
        workflow_id="test-workflow",
        variables={"input": "test"},
        history=[]
    )

    assert context.workflow_id == "test-workflow"
    assert context.variables["input"] == "test"
    assert context.history == []

@pytest.mark.asyncio
async def test_agent_state_serialization():
    """测试 Agent 状态序列化"""
    state = AgentState(agent_id="test-agent")
    state.update_status(AgentStatus.RUNNING)
    state.record_metric("executions", 3)

    # 序列化
    serialized = state.to_dict()
    assert serialized["agent_id"] == "test-agent"
    assert serialized["status"] == "running"
    assert "executions" in serialized["metrics"]

    # 反序列化
    new_state = AgentState.from_dict(serialized)
    assert new_state.agent_id == "test-agent"
    assert new_state.status == AgentStatus.RUNNING
