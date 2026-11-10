"""
模式存储和检索

提供模式的高级操作功能，包括模式聚类、相似性计算等。
"""

from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
import re
from collections import defaultdict

from .base import Pattern
from .global_memory import GlobalMemory, PatternType


class PatternManager:
    """
    模式管理器

    提供模式的高级操作功能：
    - 模式聚类和分组
    - 模式相似性计算
    - 模式推荐
    - 模式生命周期管理
    """

    def __init__(self, global_memory: GlobalMemory):
        """
        初始化模式管理器

        Args:
            global_memory: 全局记忆实例
        """
        self.global_memory = global_memory
        self._cache: Dict[str, List[Pattern]] = {}
        self._cache_time: Dict[str, datetime] = {}
        self._cache_ttl = timedelta(minutes=5)

    async def get_relevant_patterns(
        self,
        context: str,
        pattern_type: Optional[str] = None,
        limit: int = 5,
        min_confidence: float = 0.3
    ) -> List[Pattern]:
        """
        根据上下文获取相关模式

        Args:
            context: 上下文文本
            pattern_type: 可选的模式类型过滤器
            limit: 返回数量限制
            min_confidence: 最小置信度

        Returns:
            相关模式列表，按相关性排序
        """
        # 先尝试从缓存获取
        cache_key = f"relevant:{pattern_type or 'all'}:{hash(context)}"
        if cache_key in self._cache:
            cache_time = self._cache_time.get(cache_key, datetime.min)
            if datetime.now() - cache_time < self._cache_ttl:
                cached = self._cache[cache_key]
                # 过滤和限制
                filtered = [p for p in cached if p.confidence >= min_confidence]
                return filtered[:limit]

        # 从数据库搜索
        patterns = await self.global_memory.search_patterns(context, pattern_type, limit * 2)

        # 计算相关性并排序
        scored_patterns = [
            (pattern, self._calculate_relevance(pattern, context))
            for pattern in patterns
            if pattern.confidence >= min_confidence
        ]

        scored_patterns.sort(key=lambda x: x[1], reverse=True)

        # 缓存结果
        result = [pattern for pattern, _ in scored_patterns[:limit]]
        self._cache[cache_key] = [pattern for pattern, _ in scored_patterns[:limit * 2]]
        self._cache_time[cache_key] = datetime.now()

        return result

    async def find_similar_patterns(
        self,
        pattern_id: str,
        limit: int = 5
    ) -> List[Pattern]:
        """
        查找与指定模式相似的模式

        Args:
            pattern_id: 模式 ID
            limit: 返回数量限制

        Returns:
            相似模式列表
        """
        # 获取目标模式
        target_pattern = await self._get_pattern_by_id(pattern_id)
        if not target_pattern:
            return []

        # 获取同类型的所有模式
        patterns = await self.global_memory.get_patterns(
            pattern_type=target_pattern.pattern_type,
            limit=100
        )

        # 计算相似度
        similarities = []
        for pattern in patterns:
            if pattern.id != pattern_id:
                similarity = self._calculate_pattern_similarity(target_pattern, pattern)
                if similarity > 0.1:
                    similarities.append((pattern, similarity))

        # 按相似度排序
        similarities.sort(key=lambda x: x[1], reverse=True)

        return [pattern for pattern, _ in similarities[:limit]]

    async def cluster_patterns(
        self,
        pattern_type: Optional[str] = None
    ) -> Dict[str, List[Pattern]]:
        """
        对模式进行聚类

        Args:
            pattern_type: 可选的模式类型过滤器

        Returns:
            聚类结果字典，键为聚类名称，值为模式列表
        """
        patterns = await self.global_memory.get_patterns(
            pattern_type=pattern_type,
            limit=500
        )

        # 基于标签的简单聚类
        clusters: Dict[str, List[Pattern]] = defaultdict(list)

        for pattern in patterns:
            tags = pattern.metadata.get("tags", []) if pattern.metadata else []
            if tags:
                for tag in tags:
                    clusters[tag].append(pattern)
            else:
                # 没有标签的模式根据 key 聚类
                cluster_name = self._extract_cluster_key(pattern.key)
                clusters[cluster_name].append(pattern)

        return dict(clusters)

    async def recommend_patterns(
        self,
        task_context: str,
        pattern_type: Optional[str] = None,
        exclude_ids: Optional[Set[str]] = None,
        limit: int = 3
    ) -> List[Pattern]:
        """
        根据任务上下文推荐模式

        Args:
            task_context: 任务上下文
            pattern_type: 可选的模式类型过滤器
            exclude_ids: 要排除的模式 ID 集合
            limit: 返回数量限制

        Returns:
            推荐的模式列表
        """
        patterns = await self.get_relevant_patterns(
            context=task_context,
            pattern_type=pattern_type,
            limit=limit * 2
        )

        # 排除指定 ID
        if exclude_ids:
            patterns = [p for p in patterns if p.id not in exclude_ids]

        # 排序考虑使用次数和置信度
        patterns.sort(
            key=lambda p: (p.usage_count * 0.6 + p.confidence * 0.4),
            reverse=True
        )

        return patterns[:limit]

    async def update_pattern_from_feedback(
        self,
        pattern_id: str,
        success: bool,
        feedback_weight: float = 0.2
    ) -> Optional[Pattern]:
        """
        根据反馈更新模式

        Args:
            pattern_id: 模式 ID
            success: 是否成功
            feedback_weight: 反馈权重（0-1）

        Returns:
            更新后的模式，如果不存在则返回 None
        """
        pattern = await self._get_pattern_by_id(pattern_id)
        if not pattern:
            return None

        # 更新使用次数
        await self.global_memory.update_pattern_usage(pattern_id)
        pattern.usage_count += 1

        # 更新置信度
        target_confidence = 1.0 if success else 0.0
        pattern.confidence = (
            (1 - feedback_weight) * pattern.confidence +
            feedback_weight * target_confidence
        )

        # 更新模式
        await self.global_memory.save_pattern(pattern)

        return pattern

    async def merge_patterns(
        self,
        pattern_ids: List[str],
        new_key: str,
        description: str = ""
    ) -> Optional[Pattern]:
        """
        合并多个相似的模式

        Args:
            pattern_ids: 要合并的模式 ID 列表
            new_key: 合并后的新键
            description: 新模式的描述

        Returns:
            合并后的模式
        """
        if not pattern_ids:
            return None

        # 获取所有要合并的模式
        patterns = []
        for pid in pattern_ids:
            pattern = await self._get_pattern_by_id(pid)
            if pattern:
                patterns.append(pattern)

        if not patterns:
            return None

        # 合并属性
        base_pattern = patterns[0]
        total_usage = sum(p.usage_count for p in patterns)
        weighted_confidence = sum(
            p.confidence * p.usage_count for p in patterns
        ) / total_usage

        # 合并标签
        all_tags: Set[str] = set()
        for pattern in patterns:
            tags = pattern.metadata.get("tags", []) if pattern.metadata else []
            all_tags.update(tags)

        # 合并值（如果可能）
        merged_value = self._merge_values([p.value for p in patterns])

        # 创建新模式
        new_pattern = Pattern(
            id=f"merged:{base_pattern.pattern_type}:{new_key}",
            pattern_type=base_pattern.pattern_type,
            key=new_key,
            value=merged_value,
            confidence=weighted_confidence,
            usage_count=total_usage,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            metadata={
                "description": description or f"Merged from {len(patterns)} patterns",
                "tags": list(all_tags),
                "merged_from": pattern_ids
            }
        )

        await self.global_memory.save_pattern(new_pattern)

        # 删除旧模式
        for pid in pattern_ids:
            await self.global_memory.delete_pattern(pid)

        return new_pattern

    async def get_pattern_statistics(
        self,
        pattern_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取模式统计信息

        Args:
            pattern_type: 可选的模式类型过滤器

        Returns:
            统计信息字典
        """
        patterns = await self.global_memory.get_patterns(
            pattern_type=pattern_type,
            limit=1000
        )

        if not patterns:
            return {
                "total": 0,
                "by_type": {},
                "average_confidence": 0.0,
                "average_usage": 0.0,
                "recent": []
            }

        # 按类型统计
        by_type: Dict[str, int] = defaultdict(int)
        for pattern in patterns:
            by_type[pattern.pattern_type] += 1

        # 计算平均值
        total = len(patterns)
        avg_confidence = sum(p.confidence for p in patterns) / total
        avg_usage = sum(p.usage_count for p in patterns) / total

        # 最近创建的模式
        recent_patterns = sorted(
            patterns,
            key=lambda p: p.created_at or datetime.min,
            reverse=True
        )[:10]

        return {
            "total": total,
            "by_type": dict(by_type),
            "average_confidence": avg_confidence,
            "average_usage": avg_usage,
            "recent": [
                {
                    "id": p.id,
                    "key": p.key,
                    "pattern_type": p.pattern_type,
                    "confidence": p.confidence,
                    "usage_count": p.usage_count
                }
                for p in recent_patterns
            ]
        }

    async def _get_pattern_by_id(self, pattern_id: str) -> Optional[Pattern]:
        """根据 ID 获取模式"""
        # 从数据库获取
        patterns = await self.global_memory.search_patterns(pattern_id, limit=1)
        for pattern in patterns:
            if pattern.id == pattern_id:
                return pattern
        return None

    def _calculate_relevance(self, pattern: Pattern, context: str) -> float:
        """
        计算模式与上下文的相关性分数

        Args:
            pattern: 模式对象
            context: 上下文文本

        Returns:
            相关性分数（0-1）
        """
        score = 0.0
        context_lower = context.lower()

        # 模式键匹配
        if pattern.key.lower() in context_lower:
            score += 0.5

        # 模式值匹配
        pattern_value_str = str(pattern.value).lower()
        if pattern_value_str and pattern_value_str in context_lower:
            score += 0.3

        # 标签匹配
        tags = pattern.metadata.get("tags", []) if pattern.metadata else []
        for tag in tags:
            if tag.lower() in context_lower:
                score += 0.1

        # 描述匹配
        description = pattern.metadata.get("description", "") if pattern.metadata else ""
        if description and any(word in context_lower for word in description.lower().split()):
            score += 0.1

        return min(score, 1.0)

    def _calculate_pattern_similarity(self, pattern1: Pattern, pattern2: Pattern) -> float:
        """
        计算两个模式的相似度

        Args:
            pattern1: 第一个模式
            pattern2: 第二个模式

        Returns:
            相似度分数（0-1）
        """
        similarity = 0.0

        # 键相似度
        if pattern1.key == pattern2.key:
            similarity += 0.5
        elif self._key_similarity(pattern1.key, pattern2.key) > 0.5:
            similarity += 0.3

        # 值相似度
        value_sim = self._value_similarity(pattern1.value, pattern2.value)
        similarity += value_sim * 0.3

        # 标签重叠
        tags1 = set(pattern1.metadata.get("tags", [])) if pattern1.metadata else set()
        tags2 = set(pattern2.metadata.get("tags", [])) if pattern2.metadata else set()
        if tags1 and tags2:
            overlap = len(tags1 & tags2) / len(tags1 | tags2)
            similarity += overlap * 0.2

        return min(similarity, 1.0)

    def _key_similarity(self, key1: str, key2: str) -> float:
        """计算键的相似度"""
        key1_words = set(re.findall(r'\w+', key1.lower()))
        key2_words = set(re.findall(r'\w+', key2.lower()))

        if not key1_words or not key2_words:
            return 0.0

        overlap = len(key1_words & key2_words)
        return overlap / max(len(key1_words), len(key2_words))

    def _value_similarity(self, value1: Any, value2: Any) -> float:
        """计算值的相似度"""
        # 转换为字符串比较
        str1 = str(value1).lower()
        str2 = str(value2).lower()

        if str1 == str2:
            return 1.0

        # 计算词重叠
        words1 = set(re.findall(r'\w+', str1))
        words2 = set(re.findall(r'\w+', str2))

        if not words1 or not words2:
            return 0.0

        overlap = len(words1 & words2)
        return overlap / max(len(words1), len(words2))

    def _extract_cluster_key(self, pattern_key: str) -> str:
        """从模式键提取聚类键"""
        # 简单实现：提取第一个单词或前缀
        words = re.findall(r'\w+', pattern_key.lower())
        if words:
            return words[0]
        return "other"

    def _merge_values(self, values: List[Any]) -> Any:
        """
        合并多个模式值

        Args:
            values: 值列表

        Returns:
            合并后的值
        """
        if not values:
            return None

        if len(values) == 1:
            return values[0]

        # 如果所有值相同，返回该值
        first_value = values[0]
        if all(v == first_value for v in values):
            return first_value

        # 如果是列表类型，尝试合并
        if isinstance(first_value, list):
            merged = []
            seen = set()
            for value in values:
                if isinstance(value, list):
                    for item in value:
                        item_str = str(item)
                        if item_str not in seen:
                            seen.add(item_str)
                            merged.append(item)
            return merged

        # 如果是字典类型，尝试合并
        if isinstance(first_value, dict):
            merged = {}
            for value in values:
                if isinstance(value, dict):
                    merged.update(value)
            return merged

        # 默认：返回第一个值作为代表
        return first_value

    def clear_cache(self) -> None:
        """清空缓存"""
        self._cache.clear()
        self._cache_time.clear()
