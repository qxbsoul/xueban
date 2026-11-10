"""
三层记忆架构使用示例

演示如何使用 SessionMemory、UserMemory、GlobalMemory 和 PatternManager。
"""

import asyncio
from datetime import datetime
import uuid

from agentgraph.memory import (
    SessionMemory,
    UserMemory,
    GlobalMemory,
    PatternManager,
    Pattern,
    PatternType,
    Interaction
)


async def example_session_memory():
    """会话记忆示例"""
    print("=== 会话记忆示例 ===")

    # 创建会话记忆
    session = SessionMemory(session_id="session-123")
    print(f"会话 ID: {session.id}")

    # 保存交互
    interaction = Interaction(
        id=str(uuid.uuid4()),
        user_id="user-001",
        agent_id="coder",
        session_id=session.id,
        input="如何使用 Python 读写文件？",
        output="可以使用 open() 函数，配合 read() 和 write() 方法...",
        reasoning="Python 文件操作基础",
        confidence=0.95,
        timestamp=datetime.now(),
        metadata={"language": "python", "topic": "file-io"}
    )
    await session.save(interaction)
    print(f"已保存交互: {interaction.input[:30]}...")

    # 获取最近的交互
    recent = await session.get_recent("user-001", limit=5)
    print(f"最近的交互数: {len(recent)}")

    # 搜索
    results = await session.search("文件", user_id="user-001", limit=3)
    print(f"搜索 '文件' 的结果数: {len(results)}")

    # 获取统计
    stats = await session.get_statistics("user-001")
    print(f"统计信息: {stats}")

    print()


async def example_user_memory():
    """用户记忆示例"""
    print("=== 用户记忆示例 ===")

    # 创建用户记忆
    user_memory = UserMemory(db_path="test_memory.db")

    # 保存多个交互
    for i in range(3):
        interaction = Interaction(
            id=str(uuid.uuid4()),
            user_id="user-001",
            agent_id="assistant",
            session_id=f"session-{i}",
            input=f"问题 {i+1}: 什么是 Python？",
            output=f"回答 {i+1}: Python 是一种编程语言...",
            reasoning=f"Python 基础问题 {i+1}",
            confidence=0.9 - i * 0.1,
            timestamp=datetime.now(),
            metadata={"question": i+1}
        )
        await user_memory.save(interaction)

    print(f"已保存 3 个交互")

    # 获取最近交互
    recent = await user_memory.get_recent("user-001", limit=5)
    print(f"最近交互数: {len(recent)}")

    # 搜索
    results = await user_memory.search("Python", user_id="user-001", limit=5)
    print(f"搜索 'Python' 的结果数: {len(results)}")

    # 获取统计信息
    stats = await user_memory.get_statistics("user-001")
    print(f"用户统计: {stats}")

    # 清理测试数据
    await user_memory.clear_history("user-001")
    print("已清理测试数据")

    print()


async def example_global_memory():
    """全局记忆示例"""
    print("=== 全局记忆示例 ===")

    # 创建全局记忆
    global_memory = GlobalMemory(db_path="test_global.db")

    # 保存模式
    pattern = Pattern(
        id="pattern-001",
        pattern_type=PatternType.BEST_PRACTICE,
        key="python_file_reading",
        value={
            "approach": "使用 with 语句确保文件正确关闭",
            "code": "with open('file.txt', 'r') as f: content = f.read()"
        },
        confidence=0.95,
        usage_count=10,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        metadata={
            "description": "Python 文件读取最佳实践",
            "tags": ["python", "best-practice", "file-io"]
        }
    )
    await global_memory.save_pattern(pattern)
    print(f"已保存模式: {pattern.key}")

    # 获取模式
    patterns = await global_memory.get_patterns(
        pattern_type=PatternType.BEST_PRACTICE,
        limit=5
    )
    print(f"获取到 {len(patterns)} 个最佳实践模式")

    # 学习新模式
    new_pattern_id = await global_memory.learn_pattern(
        pattern_type="task_pattern",
        key="json_parsing",
        value={"use": "json.loads()"},
        description="JSON 解析方法",
        tags=["python", "json"],
        confidence=0.8
    )
    print(f"学习了新模式: {new_pattern_id}")

    # 搜索模式
    results = await global_memory.search_patterns("python", limit=3)
    print(f"搜索 'python' 的结果数: {len(results)}")

    # 获取统计信息
    stats = await global_memory.get_statistics()
    print(f"全局统计: {stats}")

    # 清理测试数据
    await global_memory.delete_pattern("pattern-001")
    await global_memory.delete_pattern(new_pattern_id)
    print("已清理测试数据")

    print()


async def example_pattern_manager():
    """模式管理器示例"""
    print("=== 模式管理器示例 ===")

    # 创建全局记忆和管理器
    global_memory = GlobalMemory(db_path="test_patterns.db")
    manager = PatternManager(global_memory)

    # 学习一些模式
    pattern_ids = []
    for i in range(3):
        pid = await global_memory.learn_pattern(
            pattern_type="code_pattern",
            key=f"error_handling_{i}",
            value=f"try-except 模式 {i}",
            description=f"错误处理模式 {i}",
            tags=["python", "error-handling"],
            confidence=0.8
        )
        pattern_ids.append(pid)

    print(f"学习了 {len(pattern_ids)} 个模式")

    # 推荐模式
    recommendations = await manager.recommend_patterns(
        task_context="如何处理 Python 异常？",
        pattern_type="code_pattern",
        limit=2
    )
    print(f"推荐了 {len(recommendations)} 个模式")

    # 获取模式统计
    stats = await manager.get_pattern_statistics()
    print(f"模式统计: {stats}")

    # 查找相似模式
    if pattern_ids:
        similar = await manager.find_similar_patterns(pattern_ids[0], limit=2)
        print(f"找到 {len(similar)} 个相似模式")

    # 清理测试数据
    for pid in pattern_ids:
        await global_memory.delete_pattern(pid)
    print("已清理测试数据")

    print()


async def main():
    """运行所有示例"""
    await example_session_memory()
    await example_user_memory()
    await example_global_memory()
    await example_pattern_manager()
    print("所有示例运行完成！")


if __name__ == "__main__":
    asyncio.run(main())
