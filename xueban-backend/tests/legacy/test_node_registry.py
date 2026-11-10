import pytest
from agentgraph.core.registry import NodeRegistry
from agentgraph.core.node import BaseNode, NodeConfig

pytestmark = pytest.mark.legacy


def test_node_registry_initialization():
    """测试节点注册表初始化"""
    registry = NodeRegistry()
    assert registry is not None
    assert len(registry.list()) == 0


def test_node_registration():
    """测试节点注册"""
    registry = NodeRegistry()
    registry.clear()  # 清除之前的注册

    # 创建测试节点
    class TestNode(BaseNode):
        def execute(self, inputs: dict) -> dict:
            return {"result": "test"}

    # 注册节点
    node_id = registry.register(TestNode, name="Test Node", description="A test node")

    # 验证注册成功
    assert registry.get(node_id) is not None


def test_node_retrieval():
    """测试节点检索"""
    registry = NodeRegistry()
    registry.clear()

    class TestNode(BaseNode):
        def execute(self, inputs: dict) -> dict:
            return {"result": "test"}

    # 注册节点
    node_id = registry.register(TestNode, name="Test Node", description="A test node")

    # 检索节点
    retrieved = registry.get(node_id)
    assert retrieved is not None
    assert retrieved.node_class == TestNode


def test_node_retrieval_nonexistent():
    """测试检索不存在的节点"""
    registry = NodeRegistry()
    registry.clear()
    retrieved = registry.get("nonexistent")
    assert retrieved is None


def test_node_list():
    """测试列出所有节点"""
    registry = NodeRegistry()
    registry.clear()

    class Node1(BaseNode):
        def execute(self, inputs: dict) -> dict:
            return {}

    class Node2(BaseNode):
        def execute(self, inputs: dict) -> dict:
            return {}

    registry.register(Node1, name="Node 1", description="First node")
    registry.register(Node2, name="Node 2", description="Second node")

    nodes = registry.list()
    assert len(nodes) == 2
