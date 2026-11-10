"""
AgentState - 基于 LangGraph 的状态管理基类

提供完整的状态管理功能，包括：
- 状态持久化和恢复
- 状态版本管理
- 状态变更通知
- 状态验证机制
"""

from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass, field, asdict
from datetime import datetime
import json
import hashlib
import uuid
from abc import ABC, abstractmethod

try:
    from langgraph.graph import Graph
    from langgraph.graph.message import AddableDict
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    # 创建兼容的基类
    class Graph:
        pass
    class AddableDict(dict):
        pass

from agentgraph.core.state import State


@dataclass
class StateSnapshot:
    """状态快照"""
    state_id: str
    version: int
    timestamp: datetime
    state_data: Dict[str, Any]
    checksum: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgentState(State, ABC):
    """
    基于 LangGraph 的 Agent 状态管理基类

    提供增强的状态管理功能：
    1. 继承 LangGraph Graph（如果可用）
    2. 状态快照和版本管理
    3. 状态持久化
    4. 状态变更通知
    5. 状态验证
    """

    def __init__(self, user_id: str, state_id: Optional[str] = None):
        # 初始化基础状态
        super().__init__(input="", user_id=user_id)

        # 状态管理
        self._state_id = state_id or str(uuid.uuid4())
        self._version = 0
        self._snapshots: List[StateSnapshot] = []
        self._state_history: List[Dict[str, Any]] = []

        # 状态回调
        self._state_listeners: List[callable] = []

        # 如果 LangGraph 可用，初始化图状态
        if LANGGRAPH_AVAILABLE:
            self._graph_state = AddableDict()
            self._graph = Graph()
        else:
            self._graph_state = {}
            self._graph = None

    @property
    def state_id(self) -> str:
        """获取状态 ID"""
        return self._state_id

    @property
    def version(self) -> int:
        """获取当前版本"""
        return self._version

    @property
    def graph_state(self) -> Union[AddableDict, dict]:
        """获取 LangGraph 状态（如果可用）"""
        return self._graph_state

    def create_snapshot(self, comment: str = "") -> StateSnapshot:
        """
        创建状态快照

        Args:
            comment: 快照说明

        Returns:
            StateSnapshot: 快照对象
        """
        snapshot_data = self.to_dict()

        # 计算校验和
        checksum = hashlib.md5(json.dumps(snapshot_data, sort_keys=True).encode()).hexdigest()

        snapshot = StateSnapshot(
            state_id=self._state_id,
            version=self._version,
            timestamp=datetime.now(),
            state_data=snapshot_data,
            checksum=checksum
        )

        self._snapshots.append(snapshot)
        return snapshot

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """
        恢复到指定快照

        Args:
            snapshot_id: 快照 ID

        Returns:
            bool: 是否恢复成功
        """
        for snapshot in self._snapshots:
            if snapshot.state_id == snapshot_id:
                # 验证校验和
                current_checksum = hashlib.md5(
                    json.dumps(snapshot.state_data, sort_keys=True).encode()
                ).hexdigest()

                if current_checksum == snapshot.checksum:
                    # 恢复状态
                    self.from_dict(snapshot.state_data)
                    self._version = snapshot.version
                    self._notify_state_change("restore", snapshot.state_data)
                    return True
        return False

    def get_snapshots(self, limit: int = 10) -> List[StateSnapshot]:
        """
        获取最近的快照

        Args:
            limit: 返回数量限制

        Returns:
            List[StateSnapshot]: 快照列表
        """
        return self._snapshots[-limit:]

    def add_state_listener(self, callback: callable):
        """
        添加状态变更监听器

        Args:
            callback: 回调函数，接收 (action, old_state, new_state) 参数
        """
        self._state_listeners.append(callback)

    def remove_state_listener(self, callback: callable):
        """
        移除状态变更监听器

        Args:
            callback: 要移除的回调函数
        """
        if callback in self._state_listeners:
            self._state_listeners.remove(callback)

    def _notify_state_change(self, action: str, old_state: Dict[str, Any], new_state: Dict[str, Any] = None):
        """
        通知状态变更

        Args:
            action: 变更动作
            old_state: 旧状态
            new_state: 新状态
        """
        for listener in self._state_listeners:
            try:
                listener(action, old_state, new_state)
            except Exception as e:
                print(f"State listener error: {e}")

    def validate(self) -> bool:
        """
        验证状态有效性

        Returns:
            bool: 是否有效
        """
        # 基础验证
        if not self.user_id:
            return False

        # 子类可以重写此方法实现自定义验证
        return True

    def get_state_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取状态变更历史

        Args:
            limit: 返回数量限制

        Returns:
            List[Dict]: 状态变更历史
        """
        return self._state_history[-limit:]

    def _record_state_change(self, action: str, data: Dict[str, Any]):
        """
        记录状态变更

        Args:
            action: 变更动作
            data: 变更数据
        """
        self._state_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "data": data,
            "version": self._version
        })

    # 重写 set 方法以支持版本管理
    def set(self, key: str, value: Any):
        """
        设置状态值（带版本管理）

        Args:
            key: 键
            value: 值
        """
        old_state = self.to_dict()
        super().set(key, value)
        self._version += 1
        self._record_state_change("set", {key: value})
        self._notify_state_change("set", old_state, self.to_dict())

    # 重写 get 方法以支持默认值
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取状态值

        Args:
            key: 键
            default: 默认值

        Returns:
            Any: 值
        """
        return self.metadata.get(key, default)

    def update(self, updates: Dict[str, Any]):
        """
        批量更新状态

        Args:
            updates: 更新字典
        """
        old_state = self.to_dict()
        for key, value in updates.items():
            super().set(key, value)
        self._version += 1
        self._record_state_change("update", updates)
        self._notify_state_change("update", old_state, self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典

        Returns:
            Dict: 状态字典
        """
        result = super().to_dict()
        result.update({
            "state_id": self._state_id,
            "version": self._version,
            "timestamp": datetime.now().isoformat()
        })
        return result

    def from_dict(self, data: Dict[str, Any]):
        """
        从字典恢复状态

        Args:
            data: 状态字典
        """
        # 恢复基础字段
        self.input = data.get("input", "")
        self.user_id = data.get("user_id", "")
        self.history = data.get("history", [])
        self.metadata = data.get("metadata", {})

        # 恢复管理字段
        self._state_id = data.get("state_id", self._state_id)
        self._version = data.get("version", 0)

    def save_to_file(self, filepath: str):
        """
        保存状态到文件

        Args:
            filepath: 文件路径
        """
        data = self.to_dict()
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str, user_id: str) -> 'AgentState':
        """
        从文件加载状态

        Args:
            filepath: 文件路径
            user_id: 用户 ID

        Returns:
            AgentState: 状态对象
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        state = cls(user_id=data.get("user_id", user_id))
        state.from_dict(data)
        return state

    def __str__(self) -> str:
        return f"AgentState(id={self._state_id}, version={self._version})"

    def __repr__(self) -> str:
        return self.__str__()