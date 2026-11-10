#!/usr/bin/env python3
"""
Agent 配置系统使用示例
"""

import asyncio
from agentgraph.core.agent import AgentConfig, Agent, agent_node

# 示例 1: 使用装饰器注册 Agent
@agent_node({
    "name": "code_reviewer",
    "role": "代码审查专家",
    "prompt": "审查代码质量、安全性和性能问题",
    "tools": ["linter", "security_scanner"],
    "memory_scope": "session",
    "topology_role": "worker",
    "llm_config": {
        "provider": "anthropic",
        "model": "claude-3-sonnet-20240229",
        "temperature": 0.2
    }
})
async def review_code(input: str, state: dict) -> str:
    """代码审查实现"""
    return "代码审查完成，发现3个潜在问题。"

# 示例 2: 从 YAML 文件加载配置
async def load_agent_from_yaml():
    """从 YAML 文件加载 Agent 配置"""
    config = AgentConfig.from_yaml("../examples/data_analyzer_config.yaml")

    # 创建 Agent 实例
    agent = Agent(config, "data_analyzer")

    # 模拟工具注册（实际使用时会有真实的工具函数）
    async def calculator(input_data: str, state: dict) -> float:
        return 42.0

    async def data_visualizer(input_data: str, state: dict) -> str:
        return "生成折线图"

    agent.register_tool("calculator", calculator)
    agent.register_tool("data_visualizer", data_visualizer)

    # 执行 Agent
    result = await agent.invoke("分析用户增长数据", {"user_id": "123", "period": "2024Q1"})

    print("=== 执行结果 ===")
    print(f"Agent: {result.agent_id}")
    print(f"输出: {result.output}")
    print(f"推理: {result.reasoning}")
    print(f"置信度: {result.confidence}")
    print(f"工具调用: {result.tool_calls}")

# 示例 3: 验证配置
async def validate_config():
    """验证配置示例"""
    config_dict = {
        "name": "test_agent",
        "role": "测试代理",
        "prompt": "这是一个测试代理",
        "tools": ["test_tool"],
        "memory_scope": "session",
        "topology_role": "worker"
    }

    config = AgentConfig.from_dict(config_dict)

    if config.validate():
        print("配置验证成功！")
    else:
        print("配置验证失败！")

# 示例 4: Agent 注册管理
async def registry_example():
    """Agent 注册表示例"""
    from agentgraph.core.agent import list_agents, get_agent, register_agent

    # 列出所有注册的 Agent
    print("已注册的 Agent:")
    for agent_id in list_agents():
        print(f"  - {agent_id}")

    # 获取特定 Agent
    agent_func = get_agent("code_reviewer")
    if agent_func:
        print(f"找到 Agent: {agent_func.agent_id}")

async def main():
    """主函数，运行所有示例"""
    print("=== Agent 配置系统示例 ===\n")

    # 运行示例
    await validate_config()
    print("\n" + "="*50 + "\n")

    await registry_example()
    print("\n" + "="*50 + "\n")

    await load_agent_from_yaml()

    # 测试装饰器创建的 Agent
    result = await review_code("审查这段代码", {"file_path": "example.py"})
    print("\n装饰器 Agent 输出:", result)

if __name__ == "__main__":
    asyncio.run(main())