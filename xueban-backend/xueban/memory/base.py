"""
Memory 基础接口和定义

定义三层记忆架构的抽象接口、数据类和枚举类型。
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MemoryScope(Enum):
    """记忆层级范围"""
    SESSION = "session"   # 会话级，内存
    USER = "user"         # 用户级，持久化
    GLOBAL = "global"     # 全局级，持久化


@dataclass
class Interaction:
    """用户交互记录数据类"""
    id: str
    user_id: str
    agent_id: str
    session_id: str
    input: str
    output: str
    reasoning: Optional[str] = None
    confidence: float = 0.0
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class Pattern:
    """模式数据类"""
    id: str
    pattern_type: str  # "user_preference", "task_pattern", "best_practice"
    key: str
    value: Any
    confidence: float = 0.5
    usage_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


# 保留原有的 Memory 接口以兼容现有代码
class Memory(ABC):
    """原有的 Memory 接口（向后兼容）"""

    @abstractmethod
    async def save_interaction(
        self,
        user_id: str,
        agent_id: str,
        input: str,
        output: str,
        reasoning: Optional[str] = None,
        confidence: Optional[float] = None
    ) -> None:
        pass

    @abstractmethod
    async def get_history(
        self,
        user_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def get_summary(
        self,
        user_id: str,
        role: Optional[str] = None
    ) -> str:
        pass

    @abstractmethod
    async def clear_history(self, user_id: str) -> None:
        pass


# 新的三层记忆架构基础接口
class BaseMemory(ABC):
    """
    三层记忆架构的基础抽象接口

    所有记忆实现（会话级、用户级）都需要实现这个接口。
    """

    @abstractmethod
    async def save(self, interaction: Interaction) -> None:
        """
        保存交互记录

        Args:
            interaction: 交互记录对象
        """
        pass

    @abstractmethod
    async def get_recent(self, user_id: str, limit: int = 10) -> List[Interaction]:
        """
        获取最近的交互记录

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            最近的交互记录列表
        """
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 5
    ) -> List[Interaction]:
        """
        搜索交互记录

        Args:
            query: 搜索查询
            user_id: 可选的用户 ID 过滤器
            limit: 返回数量限制

        Returns:
            匹配的交互记录列表
        """
        pass