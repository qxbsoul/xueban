"""
蜂群拓扑系统使用示例
演示四种拓扑结构的创建和使用
"""

import asyncio
from agentgraph.core.agent import AgentConfig, Agent
from agentgraph.core.topology import (
    TopologyFactory,
    create_topology_from_dict,
    HierarchicalTopology,
    MeshTopology,
    RingTopology,
    StarTopology,
    TopologyConfig,
    RouteStrategy
)
from agentgraph.core.router import TopologyRouter


async def demo_hierarchical_topology():
    """演示层级拓扑"""
    print("\n=== 层级拓扑示例 ===")

    # 创建 Agent
    agents = {}
    config_leader = AgentConfig(
        name="Leader",
        role="任务分配者",
        prompt="负责任务分配和结果汇总",
        tools=["task_allocation", "result_aggregation"]
    )
    config_worker1 = AgentConfig(
        name="Worker1",
        role="数据处理员",
        prompt="专门负责数据处理和清理",
        tools=["data_cleaning", "data_processing"]
    )
    config_worker2 = AgentConfig(
        name="Worker2",
        role="分析员",
        prompt="负责数据分析和报告生成",
        tools=["data_analysis", "report_generation"]
    )

    agents["leader"] = Agent(config_leader, "leader")
    agents["worker1"] = Agent(config_worker1, "worker1")
    agents["worker2"] = Agent(config_worker2, "worker2")

    # 创建层级拓扑
    hierarchical = TopologyFactory.create_hierarchical(
        agents=["leader", "worker1", "worker2"],
        leader_id="leader"
    )

    # 创建路由器
    router = TopologyRouter(hierarchical)

    # 模拟路由
    state = {"input": "处理销售数据并生成月度报告", "task_type": "data_analysis"}

    # Leader 分配任务给 Worker1
    next_agent = await router.route(state, agents, "leader")
    print(f"Leader -> {next_agent}")

    # Worker1 处理后返回给 Leader
    next_agent = await router.route({"input": "数据处理完成"}, agents, "worker1")
    print(f"Worker1 -> {next_agent}")

    # 显示拓扑图
    print("\n层级拓扑图 (Mermaid):")
    print(hierarchical.to_mermaid())


async def demo_mesh_topology():
    """演示网格拓扑"""
    print("\n=== 网格拓扑示例 ===")

    # 创建多个 Agent
    agents = {}
    agent_configs = [
        AgentConfig(name="Agent1", role="专家", prompt="数据分析专家", tools=["analysis"]),
        AgentConfig(name="Agent2", role="工程师", prompt="系统工程师", tools=["engineering"]),
        AgentConfig(name="Agent3", role="设计师", prompt="UI/UX设计师", tools=["design"]),
        AgentConfig(name="Agent4", role="测试员", prompt="质量测试员", tools=["testing"])
    ]

    for i, config in enumerate(agent_configs):
        agents[f"agent{i+1}"] = Agent(config, f"agent{i+1}")

    # 创建网格拓扑
    mesh = TopologyFactory.create_mesh(
        agents=["agent1", "agent2", "agent3", "agent4"]
    )

    # 创建路由器
    router = TopologyRouter(mesh)

    # 模拟路由
    state = {"input": "设计一个全新的产品功能"}

    # 随机路由
    current = "agent1"
    for i in range(5):
        next_agent = await router.route(state, agents, current)
        print(f"{current} -> {next_agent}")
        current = next_agent

    # 显示拓扑图
    print("\n网格拓扑图 (Mermaid):")
    print(mesh.to_mermaid())


async def demo_ring_topology():
    """演示环形拓扑"""
    print("\n=== 环形拓扑示例 ===")

    # 创建 Agent
    agents = {}
    for i in range(4):
        config = AgentConfig(
            name=f"Agent{i+1}",
            role=f"处理节点{i+1}",
            prompt=f"处理流程的第{i+1}个步骤",
            tools=[f"step{i+1}_processing"]
        )
        agents[f"agent{i+1}"] = Agent(config, f"agent{i+1}")

    # 创建环形拓扑
    ring = TopologyFactory.create_ring(
        agents=["agent1", "agent2", "agent3", "agent4"],
        ring_order=["agent1", "agent2", "agent3", "agent4"]
    )

    # 创建路由器
    router = TopologyRouter(ring)

    # 模拟环形路由
    state = {"input": "开始处理任务"}

    # 按顺序流转
    current = "agent1"
    for i in range(6):
        next_agent = await router.route(state, agents, current)
        print(f"{current} -> {next_agent}")
        current = next_agent

    # 显示拓扑图
    print("\n环形拓扑图 (Mermaid):")
    print(ring.to_mermaid())


async def demo_star_topology():
    """演示星形拓扑"""
    print("\n=== 星形拓扑示例 ===")

    # 创建 Agent
    agents = {}
    coordinator_config = AgentConfig(
        name="Coordinator",
        role="协调者",
        prompt="负责协调各个子任务的执行",
        tools=["coordination", "task_management"]
    )

    sub_agents_config = [
        AgentConfig(name="Researcher", role="研究员", prompt="负责研究分析", tools=["research"]),
        AgentConfig(name="Writer", role="写手", prompt="负责内容创作", tools=["writing"]),
        AgentConfig(name="Editor", role="编辑", prompt="负责内容编辑", tools=["editing"])
    ]

    agents["coordinator"] = Agent(coordinator_config, "coordinator")
    for i, config in enumerate(sub_agents_config):
        agents[f"sub_agent{i+1}"] = Agent(config, f"sub_agent{i+1}")

    # 创建星形拓扑
    star = TopologyFactory.create_star(
        agents=["coordinator", "sub_agent1", "sub_agent2", "sub_agent3"],
        coordinator_id="coordinator"
    )

    # 创建路由器
    router = TopologyRouter(star)

    # 模拟路由
    state = {"input": "创建一个完整的项目文档"}

    # Coordinator 分配任务
    current = "coordinator"
    for i in range(4):
        next_agent = await router.route(state, agents, current)
        print(f"{current} -> {next_agent}")
        current = next_agent

    # 显示拓扑图
    print("\n星形拓扑图 (Mermaid):")
    print(star.to_mermaid())


async def demo_composite_router():
    """演示复合路由器 - 拓扑切换"""
    print("\n=== 复合路由器示例 ===")

    from agentgraph.core.router import CompositeRouter

    # 创建多个拓扑
    hierarchical = TopologyFactory.create_hierarchical(
        agents=["leader", "worker1", "worker2"],
        leader_id="leader"
    )

    mesh = TopologyFactory.create_mesh(
        agents=["agent1", "agent2", "agent3"]
    )

    # 创建复合路由器
    composite = CompositeRouter()
    composite.add_topology("hierarchical", hierarchical)
    composite.add_topology("mesh", mesh)

    # 创建简单的 Agent
    agents = {
        "leader": Agent(AgentConfig(name="Leader", role="Leader", prompt=""), "leader"),
        "worker1": Agent(AgentConfig(name="Worker1", role="Worker", prompt=""), "worker1"),
        "worker2": Agent(AgentConfig(name="Worker2", role="Worker", prompt=""), "worker2"),
        "agent1": Agent(AgentConfig(name="Agent1", role="Agent", prompt=""), "agent1"),
        "agent2": Agent(AgentConfig(name="Agent2", role="Agent", prompt=""), "agent2"),
        "agent3": Agent(AgentConfig(name="Agent3", role="Agent", prompt=""), "agent3")
    }

    state = {"input": "处理任务"}

    # 使用层级拓扑
    composite.switch_topology("hierarchical")
    print("切换到层级拓扑:")
    for i in range(3):
        current = "leader" if i == 0 else "worker1" if i == 1 else "worker2"
        next_agent = await composite.route(state, agents, current)
        print(f"{current} -> {next_agent}")

    # 切换到网格拓扑
    composite.switch_topology("mesh")
    print("\n切换到网格拓扑:")
    current = "agent1"
    for i in range(4):
        next_agent = await composite.route(state, agents, current)
        print(f"{current} -> {next_agent}")

    # 显示所有拓扑摘要
    print("\n所有拓扑摘要:")
    summaries = composite.get_topology_summary()
    for tid, summary in summaries.items():
        print(f"\n{tid}:")
        print(f"  Type: {summary['type']}")
        print(f"  Strategy: {summary['strategy']}")
        print(f"  Agents: {summary['agents']}")


async def demo_from_dict():
    """从字典创建拓扑示例"""
    print("\n=== 从字典创建拓扑示例 ===")

    # 定义拓扑配置
    topology_config = {
        "type": "hierarchical",
        "agents": ["manager", "developer1", "developer2", "tester"],
        "leader_id": "manager",
        "metadata": {
            "department": "engineering",
            "team_size": 4
        }
    }

    # 从字典创建拓扑
    topology = create_topology_from_dict(topology_config)
    print(f"创建的拓扑类型: {topology.__class__.__name__}")
    print(f"拓扑名称: {topology.config.name}")
    print(f"包含的 Agent: {topology.config.agents}")

    # 显示拓扑图
    print("\n拓扑图 (Mermaid):")
    print(topology.to_mermaid())


async def main():
    """主函数 - 运行所有示例"""
    await demo_hierarchical_topology()
    await demo_mesh_topology()
    await demo_ring_topology()
    await demo_star_topology()
    await demo_composite_router()
    await demo_from_dict()


if __name__ == "__main__":
    asyncio.run(main())