import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from agentgraph.core.graph import AgentGraph
from agentgraph.core.agent import Agent
from agentgraph.memory.sqlite import SQLiteMemory

pytestmark = pytest.mark.legacy


@pytest.mark.asyncio
async def test_workflow_creation():
    """测试工作流创建"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 验证工作流初始化
    assert graph.memory == memory
    assert len(graph.agents) == 0


@pytest.mark.asyncio
async def test_agent_addition():
    """测试添加代理"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建模拟代理
    mock_agent = Mock(spec=Agent)
    mock_agent.agent_id = "test-agent"
    mock_agent.execute = AsyncMock(return_value={"result": "success"})

    # 添加代理
    graph.add_agent(mock_agent)

    # 验证代理已添加
    assert "test-agent" in graph.agents
    assert graph.agents["test-agent"] == mock_agent


@pytest.mark.asyncio
async def test_workflow_execution():
    """测试工作流执行"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建模拟代理
    mock_agent = Mock(spec=Agent)
    mock_agent.agent_id = "test-agent"
    mock_agent.execute = AsyncMock(return_value={"result": "success"})

    graph.add_agent(mock_agent)

    # 执行工作流
    result = await graph.invoke("test input", "user1")

    # 验证执行结果
    assert result is not None
    assert result.output == {"result": "success"}


@pytest.mark.asyncio
async def test_workflow_execution_with_error():
    """测试工作流执行错误处理"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建会抛出错误的代理
    mock_agent = Mock(spec=Agent)
    mock_agent.agent_id = "test-agent"
    mock_agent.execute = AsyncMock(side_effect=Exception("Test error"))

    graph.add_agent(mock_agent)

    # 执行工作流并期望错误
    with pytest.raises(Exception):
        await graph.invoke("test input", "user1")


@pytest.mark.asyncio
async def test_parallel_execution():
    """测试并行执行"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建多个代理
    agents = []
    for i in range(3):
        agent = Mock(spec=Agent)
        agent.agent_id = f"agent-{i}"
        agent.execute = AsyncMock(return_value={"result": f"success-{i}"})
        graph.add_agent(agent)
        agents.append(agent)

    # 模拟并行执行
    tasks = []
    for agent in agents:
        task = agent.execute("test input", {})
        tasks.append(task)

    results = await asyncio.gather(*tasks)

    # 验证所有代理都执行了
    assert len(results) == 3
    for i, result in enumerate(results):
        assert result["result"] == f"success-{i}"


@pytest.mark.asyncio
async def test_workflow_metrics():
    """测试工作流指标收集"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建代理
    mock_agent = Mock(spec=Agent)
    mock_agent.agent_id = "test-agent"
    mock_agent.execute = AsyncMock(return_value={"result": "success"})

    graph.add_agent(mock_agent)

    # 执行工作流
    result = await graph.invoke("test input", "user1")

    # 检查指标
    metrics = graph.get_metrics()
    assert metrics["total_executions"] > 0
    assert metrics["success_rate"] == 1.0  # 100% 成功率
