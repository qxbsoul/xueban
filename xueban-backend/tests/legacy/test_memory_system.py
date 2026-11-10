import pytest
import asyncio
from agentgraph.memory.base import BaseMemory
from agentgraph.memory.sqlite import SQLiteMemory
from agentgraph.memory.session import SessionMemory

pytestmark = pytest.mark.legacy

class TestMemory(BaseMemory):
    def __init__(self):
        self.data = []

    async def save(self, key: str, value: any) -> None:
        self.data.append((key, value))

    async def load(self, key: str) -> any:
        for k, v in self.data:
            if k == key:
                return v
        return None

@pytest.mark.asyncio
async def test_base_memory():
    """测试基础记忆系统"""
    memory = TestMemory()

    # 测试保存和加载
    await memory.save("test_key", "test_value")
    result = await memory.load("test_key")
    assert result == "test_value"

@pytest.mark.asyncio
async def test_sqlite_memory_initialization():
    """测试 SQLite 记忆初始化"""
    memory = SQLiteMemory(":memory:")
    assert memory is not None

@pytest.mark.asyncio
async def test_sqlite_memory_operations():
    """测试 SQLite 记忆操作"""
    memory = SQLiteMemory(":memory:")

    # 测试保存交互
    await memory.save_interaction(
        user_id="user1",
        agent_id="agent1",
        input="test input",
        output="test output",
        reasoning="test reasoning",
        confidence=0.8
    )

    # 测试获取历史
    history = await memory.get_history("user1", limit=10)
    assert len(history) == 1
    assert history[0]["input"] == "test input"
    assert history[0]["output"] == "test output"

@pytest.mark.asyncio
async def test_session_memory():
    """测试会话记忆"""
    session_memory = SessionMemory()

    # 创建会话
    session_id = await session_memory.create_session("user1")
    assert session_id is not None

    # 添加会话数据
    await session_memory.add_session_data(
        session_id,
        "key1",
        {"value": "test"}
    )

    # 获取会话数据
    data = await session_memory.get_session_data(session_id, "key1")
    assert data["value"] == "test"

    # 获取所有会话
    sessions = await session_memory.get_user_sessions("user1")
    assert len(sessions) == 1

@pytest.mark.asyncio
async def test_memory_error_handling():
    """测试记忆系统错误处理"""
    memory = TestMemory()

    # 测试加载不存在的键
    result = await memory.load("nonexistent")
    assert result is None

    # 测试空数据保存
    await memory.save("", "empty_key")
    result = await memory.load("")
    assert result == "empty_key"
