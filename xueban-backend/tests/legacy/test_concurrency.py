import pytest
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from agentgraph.core.graph import AgentGraph
from agentgraph.core.agent import Agent
from agentgraph.memory.sqlite import SQLiteMemory

pytestmark = pytest.mark.legacy

@pytest.mark.asyncio
async def test_concurrent_agent_execution():
    """测试并发代理执行"""
    memory = SQLiteMemory(":memory:")
    graph = AgentGraph(memory=memory)

    # 创建多个代理
    agents = []
    for i in range(10):
        agent = Mock(spec=Agent)
        agent.agent_id = f"agent-{i}"
        agent.execute = AsyncMock(
            side_effect=lambda input, **kwargs: asyncio.sleep(0.1) or {"result": f"success-{i}"}
        )
        graph.add_agent(agent)
        agents.append(agent)

    # 并发执行
    start_time = time.time()
    tasks = []
    for agent in agents:
        task = agent.execute("test input", {})
        tasks.append(task)

    results = await asyncio.gather(*tasks)
    end_time = time.time()

    # 验证结果
    assert len(results) == 10
    execution_time = end_time - start_time
    print(f"Concurrent execution time: {execution_time:.2f}s")

    # 并发执行应该比顺序执行快
    assert execution_time < 1.0

@pytest.mark.asyncio
async def test_semaphore_limitation():
    """测试信号量限制并发数"""
    from agentgraph.core.utils.concurrency import ConcurrencyLimiter

    limiter = ConcurrencyLimiter(max_concurrent=3)

    # 创建需要资源的任务
    tasks = []
    for i in range(5):
        task = limiter.execute(
            lambda x=i: asyncio.sleep(0.2) or f"task-{x}-completed"
        )
        tasks.append(task)

    start_time = time.time()
    results = await asyncio.gather(*tasks)
    end_time = time.time()

    # 验证结果
    assert len(results) == 5
    assert all("completed" in result for result in results)

    # 限制并发数应该增加执行时间
    execution_time = end_time - start_time
    print(f"Limited execution time: {execution_time:.2f}s")
    assert execution_time >= 0.4  # 至少 5 * 0.2 / 3 ≈ 0.33s

@pytest.mark.asyncio
async def test_memory_concurrent_access():
    """测试并发内存访问"""
    memory = SQLiteMemory(":memory:")

    # 并发写入
    tasks = []
    for i in range(10):
        task = memory.save_interaction(
            user_id="user1",
            agent_id=f"agent-{i}",
            input=f"input-{i}",
            output=f"output-{i}",
            reasoning=f"reasoning-{i}",
            confidence=0.8
        )
        tasks.append(task)

    await asyncio.gather(*tasks)

    # 验证数据一致性
    history = await memory.get_history("user1", limit=20)
    assert len(history) == 10

@pytest.mark.asyncio
async def test_circuit_breaker_concurrent():
    """测试并发断路器"""
    from agentgraph.core.errors import CircuitBreaker

    circuit_breaker = CircuitBreaker(threshold=5, timeout=1)

    # 并发请求
    tasks = []
    for i in range(10):
        task = asyncio.create_task(
            circuit_breaker.execute(
                lambda i=i: {"result": f"success-{i}"} if i < 5 else Exception("failure")
            )
        )
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 验证部分成功，部分失败
    success_count = sum(1 for r in results if not isinstance(r, Exception))
    failure_count = sum(1 for r in results if isinstance(r, Exception))

    assert success_count == 5
    assert failure_count == 5

@pytest.mark.asyncio
async def test_performance_concurrent_requests():
    """测试并发请求性能"""
    from agentgraph.core.utils.performance import PerformanceTracker

    tracker = PerformanceTracker()

    # 创建大量并发请求
    num_requests = 100
    request_size = 1024  # 1KB data

    async def mock_request(i):
        with tracker.track_request(f"request-{i}"):
            # 模拟工作负载
            data = "x" * request_size
            await asyncio.sleep(0.01)  # 10ms delay
            return {"data": data}

    start_time = time.time()
    tasks = [mock_request(i) for i in range(num_requests)]
    results = await asyncio.gather(*tasks)
    end_time = time.time()

    # 验证性能指标
    total_time = end_time - start_time
    print(f"Total time for {num_requests} requests: {total_time:.2f}s")

    stats = tracker.get_stats()
    assert stats["total_requests"] == num_requests
    assert stats["success_rate"] == 1.0

    # 验证平均响应时间
    avg_response_time = stats["total_time"] / num_requests
    print(f"Average response time: {avg_response_time:.3f}s")
    assert avg_response_time > 0.009  # 应该接近 10ms

@pytest.mark.asyncio
async def test_memory_locking():
    """测试内存锁机制"""
    from agentgraph.core.utils.locking import SharedLock

    lock = SharedLock()
    shared_value = 0

    async def increment_value():
        async with lock.acquire("counter"):
            # 临界区
            current = shared_value
            await asyncio.sleep(0.01)  # 模拟工作
            shared_value = current + 1

    # 并发递增
    tasks = [increment_value() for _ in range(10)]
    await asyncio.gather(*tasks)

    # 验证结果
    assert shared_value == 10
