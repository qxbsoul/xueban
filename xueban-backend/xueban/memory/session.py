"""
会话级记忆 - 内存存储，会话结束即销毁

SessionMemory 提供会话级别的交互记忆，数据仅存储在内存中，
当会话结束时，这些数据会被释放。
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
from collections import deque
import uuid

from .base import BaseMemory, Interaction


class SessionMemory(BaseMemory):
    """
    会话级记忆实现

    特点：
    - 数据存储在内存中
    - 访问速度快
    - 会话结束后数据丢失
    - 适合存储当前对话的上下文
    """

    def __init__(self, session_id: Optional[str] = None):
        """
        初始化会话记忆

        Args:
            session_id: 会话 ID，如果未提供则自动生成
        """
        self.session_id = session_id or str(uuid.uuid4())
        # 使用 deque 实现固定大小的交互历史
        self._interactions: deque[Interaction] = deque(maxlen=1000)
        # 交互索引，方便快速查找
        self._interactions_by_id: Dict[str, Interaction] = {}
        # 用户交互索引
        self._interactions_by_user: Dict[str, List[Interaction]] = {}

    @property
    def id(self) -> str:
        """获取会话 ID"""
        return self.session_id

    async def save(self, interaction: Interaction) -> None:
        """
        保存交互记录

        Args:
            interaction: 交互记录对象
        """
        # 确保交互关联到当前会话
        interaction.session_id = self.session_id

        # 添加到交互历史
        self._interactions.append(interaction)

        # 更新索引
        self._interactions_by_id[interaction.id] = interaction

        # 更新用户索引
        if interaction.user_id not in self._interactions_by_user:
            self._interactions_by_user[interaction.user_id] = []
        self._interactions_by_user[interaction.user_id].append(interaction)

    async def get_recent(self, user_id: str, limit: int = 10) -> List[Interaction]:
        """
        获取最近的交互记录

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            最近的交互记录列表
        """
        user_interactions = self._interactions_by_user.get(user_id, [])
        return user_interactions[-limit:] if len(user_interactions) > limit else user_interactions

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
        query_lower = query.lower()
        results: List[tuple[Interaction, int]] = []

        interactions_to_search = (
            self._interactions_by_user.get(user_id, [])
            if user_id
            else list(self._interactions)
        )

        for interaction in interactions_to_search:
            score = self._calculate_relevance_score(interaction, query_lower)
            if score > 0:
                results.append((interaction, score))

        # 按相关性排序并返回
        results.sort(key=lambda x: x[1], reverse=True)
        return [interaction for interaction, _ in results[:limit]]

    async def get_by_id(self, interaction_id: str) -> Optional[Interaction]:
        """
        根据 ID 获取交互记录

        Args:
            interaction_id: 交互记录 ID

        Returns:
            交互记录对象，如果不存在则返回 None
        """
        return self._interactions_by_id.get(interaction_id)

    async def get_all(self) -> List[Interaction]:
        """
        获取当前会话的所有交互记录

        Returns:
            所有交互记录列表
        """
        return list(self._interactions)

    async def get_statistics(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取会话统计信息

        Args:
            user_id: 可选的用户 ID

        Returns:
            统计信息字典
        """
        interactions = (
            self._interactions_by_user.get(user_id, [])
            if user_id
            else list(self._interactions)
        )

        if not interactions:
            return {"total": 0}

        total = len(interactions)
        avg_confidence = sum(i.confidence for i in interactions) / total

        agent_count: Dict[str, int] = {}
        for interaction in interactions:
            agent_id = interaction.agent_id
            agent_count[agent_id] = agent_count.get(agent_id, 0) + 1

        return {
            "total": total,
            "average_confidence": avg_confidence,
            "agents": agent_count,
            "session_id": self.session_id
        }

    async def clear(self) -> None:
        """清空会话记忆"""
        self._interactions.clear()
        self._interactions_by_id.clear()
        self._interactions_by_user.clear()

    async def export(self) -> List[Dict[str, Any]]:
        """
        导出会话数据为字典列表

        Returns:
            交互记录的字典表示列表
        """
        return [
            {
                "id": interaction.id,
                "user_id": interaction.user_id,
                "agent_id": interaction.agent_id,
                "session_id": interaction.session_id,
                "input": interaction.input,
                "output": interaction.output,
                "reasoning": interaction.reasoning,
                "confidence": interaction.confidence,
                "timestamp": interaction.timestamp.isoformat() if interaction.timestamp else None,
                "metadata": interaction.metadata
            }
            for interaction in self._interactions
        ]

    def _calculate_relevance_score(self, interaction: Interaction, query: str) -> int:
        """
        计算交互记录与查询的相关性分数

        Args:
            interaction: 交互记录
            query: 查询字符串（小写）

        Returns:
            相关性分数
        """
        score = 0

        # 在输入中搜索
        if query in interaction.input.lower():
            score += 10

        # 在输出中搜索
        if query in interaction.output.lower():
            score += 8

        # 在推理中搜索
        if interaction.reasoning and query in interaction.reasoning.lower():
            score += 5

        # 在元数据中搜索
        if interaction.metadata:
            for value in interaction.metadata.values():
                if isinstance(value, str) and query in value.lower():
                    score += 2
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and query in item.lower():
                            score += 2

        return score
