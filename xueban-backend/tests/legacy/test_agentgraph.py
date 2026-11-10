import pytest
import asyncio
from agentgraph import AgentGraph, Agent, AgentConfig, SQLiteMemory
from agentgraph.memory.layers import LayeredMemory

pytestmark = pytest.mark.legacy

@pytest.mark.asyncio
async def test_agent_creation():
    config = AgentConfig(
        name="test_agent",
        role="Test Role",
        prompt="Test prompt"
    )
    agent = Agent(config, "test_id")
    assert agent.agent_id == "test_id"
    assert agent.config.name == "test_agent"

@pytest.mark.asyncio
async def test_agent_execution():
    config = AgentConfig(
        name="test_agent",
        role="Test Role",
        prompt="Test prompt"
    )
    agent = Agent(config, "test_id")
    result = await agent.execute("test input", {})
    assert result.agent_id == "test_id"
    assert result.output is not None
    assert result.confidence > 0

@pytest.mark.asyncio
async def test_graph_creation():
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)
    
    config = AgentConfig(
        name="test_agent",
        role="Test Role",
        prompt="Test prompt"
    )
    agent = Agent(config, "test_id")
    graph.add_agent(agent)
    
    assert "test_id" in graph.agents

@pytest.mark.asyncio
async def test_graph_execution():
    memory = SQLiteMemory(":memory:")
    await memory._initialize()
    # 直接使用 SQLiteMemory，而不是 LayeredMemory
    graph = AgentGraph(memory=memory)
    
    config = AgentConfig(
        name="test_agent",
        role="Test Role",
        prompt="Test prompt"
    )
    agent = Agent(config, "test_id")
    graph.add_agent(agent)
    
    result = await graph.invoke("test input", "test_user")
    assert result.output is not None
    assert len(result.steps) > 0

@pytest.mark.asyncio
async def test_memory_save_and_retrieve():
    memory = SQLiteMemory(":memory:")
    await memory._initialize()  # 确保数据库已初始化

    await memory.save_interaction(
        user_id="test_user",
        agent_id="test_agent",
        input="test input",
        output="test output",
        reasoning="test reasoning",
        confidence=0.9
    )
    
    history = await memory.get_history("test_user", limit=10)
    assert len(history) == 1
    assert history[0]["agent_id"] == "test_agent"
    assert history[0]["output"] == "test output"
