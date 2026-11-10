import pytest
import asyncio
from agentgraph.core.errors import (
    AgentGraphError, ValidationError, ExecutionError, ErrorCode,
    ErrorLogger, ErrorHandler, CircuitBreaker, raise_if_empty,
    raise_if_length, validate_type
)
from agentgraph.core.state import AgentState, AgentStatus

pytestmark = pytest.mark.legacy

def test_agent_graph_error():
    """测试 AgentGraph 错误"""
    error = AgentGraphError("Test error")
    assert "Test error" in str(error)
    assert error.code == ErrorCode.UNKNOWN

def test_validation_error():
    """测试验证错误"""
    error = ValidationError("Invalid input")
    assert "Invalid input" in str(error)
    assert error.code == ErrorCode.VALIDATION_ERROR

def test_execution_error():
    """测试执行错误"""
    error = ExecutionError("Execution failed")
    assert "Execution failed" in str(error)
    assert error.code == ErrorCode.EXECUTION_ERROR

@pytest.mark.asyncio
async def test_agent_state_error_handling():
    """测试 Agent 状态错误处理"""
    state = AgentState(agent_id="test-agent")

    # 测试状态更新 - 有效状态
    state.update_status(AgentStatus.RUNNING)
    assert state.status == AgentStatus.RUNNING

@pytest.mark.asyncio
async def test_memory_error_handling():
    """测试记忆系统错误处理"""
    from agentgraph.memory.sqlite import SQLiteMemory

    # SQLiteMemory 接受空字符串，创建内存数据库
    memory = SQLiteMemory("")
    assert memory is not None

@pytest.mark.asyncio
async def test_error_logging():
    """测试错误日志记录"""
    logger = ErrorLogger()

    # 记录错误
    error = AgentGraphError("Test error")
    logger.log(error)

    # 检查日志
    history = logger.get_history()
    assert len(history) == 1
    assert history[0].error == error

@pytest.mark.asyncio
async def test_error_recovery():
    """测试错误恢复机制"""
    handler = ErrorHandler()

    # 注册错误处理器
    def custom_error_handler(error, context=None):
        return {"recovered": True, "original_error": str(error)}

    handler.register_fallback(AgentGraphError, custom_error_handler)

    # 测试错误处理
    error = AgentGraphError("Test error")
    result = handler.handle(error)

    assert result["recovered"] is True
    assert "Test error" in result["original_error"]

@pytest.mark.asyncio
async def test_validation_errors():
    """测试各种验证错误"""
    # 测试空值验证
    with pytest.raises(ValidationError):
        raise_if_empty(None, "value")

    # 测试长度验证 (使用正确参数顺序: value, max_length, name)
    with pytest.raises(ValidationError):
        raise_if_length("a" * 100, 10, "string")

    # 测试类型验证
    with pytest.raises(ValidationError):
        validate_type(123, str, "value")

@pytest.mark.asyncio
async def test_circuit_breaker():
    """测试断路器模式"""
    circuit_breaker = CircuitBreaker(name="test", failure_threshold=3, timeout=10)

    # 初始状态应该是关闭的
    assert circuit_breaker.is_closed

    # 通过 call 方法执行
    async def success_func():
        return {"result": "ok"}

    result = await circuit_breaker.call(success_func)
    assert result["result"] == "ok"
