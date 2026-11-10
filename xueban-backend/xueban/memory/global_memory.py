"""
全局记忆 - 模式库和最佳实践

GlobalMemory 提供全局级别的记忆，用于存储跨用户共享的模式库和最佳实践。
"""

import aiosqlite
import json
import uuid
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from enum import Enum

from .base import Pattern


class PatternType(Enum):
    """模式类型枚举"""
    USER_PREFERENCE = "user_preference"    # 用户偏好模式
    TASK_PATTERN = "task_pattern"         # 任务模式
    BEST_PRACTICE = "best_practice"        # 最佳实践
    SUCCESS_CASE = "success_case"          # 成功案例
    FAILURE_CASE = "failure_case"          # 失败案例
    CODE_PATTERN = "code_pattern"          # 代码模式


class GlobalMemory:
    """
    全局记忆实现

    特点：
    - 数据持久化到 SQLite 数据库
    - 跨用户共享
    - 存储可复用的模式和最佳实践
    - 支持模式的学习和检索
    """

    # SQL 语句
    CREATE_PATTERNS_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS patterns (
            id TEXT PRIMARY KEY,
            pattern_type TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            tags TEXT,
            description TEXT,
            metadata TEXT
        )
    """

    CREATE_PATTERNS_INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS idx_pattern_type ON patterns(pattern_type)",
        "CREATE INDEX IF NOT EXISTS idx_pattern_key ON patterns(key)",
        "CREATE INDEX IF NOT EXISTS idx_pattern_confidence ON patterns(confidence)",
        "CREATE INDEX IF NOT EXISTS idx_pattern_usage ON patterns(usage_count)"
    ]

    CREATE_PATTERNS_FTS_TABLE_SQL = """
        CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
            id,
            key,
            value,
            description,
            tags,
            content='patterns',
            content_rowid='rowid'
        )
    """

    def __init__(self, db_path: str = "agentgraph_global.db"):
        """
        初始化全局记忆

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self._initialized = False

    async def _initialize(self) -> None:
        """初始化数据库表和索引"""
        if self._initialized:
            return

        # 确保数据库目录存在
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # 创建模式表
            await db.execute(self.CREATE_PATTERNS_TABLE_SQL)

            # 创建索引
            for index_sql in self.CREATE_PATTERNS_INDEX_SQL:
                await db.execute(index_sql)

            # 尝试创建全文搜索表
            try:
                await db.execute(self.CREATE_PATTERNS_FTS_TABLE_SQL)
            except aiosqlite.Error:
                pass

            await db.commit()

        self._initialized = True

    async def save_pattern(self, pattern: Pattern) -> None:
        """
        保存模式

        Args:
            pattern: 模式对象
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.now().isoformat()

            # 检查模式是否已存在
            cursor = await db.execute(
                "SELECT id FROM patterns WHERE id = ?",
                (pattern.id,)
            )
            existing = await cursor.fetchone()

            if existing:
                # 更新现有模式
                await db.execute(
                    """
                    UPDATE patterns
                    SET pattern_type = ?, key = ?, value = ?, confidence = ?,
                        usage_count = ?, updated_at = ?, tags = ?, description = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (
                        pattern.pattern_type,
                        pattern.key,
                        json.dumps(pattern.value) if not isinstance(pattern.value, str) else pattern.value,
                        pattern.confidence,
                        pattern.usage_count,
                        now,
                        json.dumps(pattern.metadata.get("tags", [])) if pattern.metadata else "[]",
                        pattern.metadata.get("description", "") if pattern.metadata else "",
                        json.dumps(pattern.metadata) if pattern.metadata else "{}",
                        pattern.id
                    )
                )
            else:
                # 插入新模式
                await db.execute(
                    """
                    INSERT INTO patterns
                    (id, pattern_type, key, value, confidence, usage_count, created_at, updated_at, tags, description, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pattern.id,
                        pattern.pattern_type,
                        pattern.key,
                        json.dumps(pattern.value) if not isinstance(pattern.value, str) else pattern.value,
                        pattern.confidence,
                        pattern.usage_count,
                        pattern.created_at.isoformat() if pattern.created_at else now,
                        now,
                        json.dumps(pattern.metadata.get("tags", [])) if pattern.metadata else "[]",
                        pattern.metadata.get("description", "") if pattern.metadata else "",
                        json.dumps(pattern.metadata) if pattern.metadata else "{}"
                    )
                )

            await db.commit()

    async def get_patterns(
        self,
        pattern_type: Optional[str] = None,
        limit: int = 10,
        min_confidence: float = 0.0,
        min_usage: int = 0
    ) -> List[Pattern]:
        """
        获取模式列表

        Args:
            pattern_type: 模式类型过滤器
            limit: 返回数量限制
            min_confidence: 最小置信度
            min_usage: 最小使用次数

        Returns:
            模式列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            query = "SELECT * FROM patterns WHERE 1=1"
            params = []

            if pattern_type:
                query += " AND pattern_type = ?"
                params.append(pattern_type)

            if min_confidence > 0:
                query += " AND confidence >= ?"
                params.append(min_confidence)

            if min_usage > 0:
                query += " AND usage_count >= ?"
                params.append(min_usage)

            query += " ORDER BY confidence DESC, usage_count DESC LIMIT ?"
            params.append(limit)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

        return [self._row_to_pattern(row) for row in rows]

    async def get_pattern_by_key(self, pattern_type: str, key: str) -> Optional[Pattern]:
        """
        根据类型和键获取模式

        Args:
            pattern_type: 模式类型
            key: 模式键

        Returns:
            模式对象，如果不存在则返回 None
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM patterns
                WHERE pattern_type = ? AND key = ?
                ORDER BY confidence DESC, usage_count DESC
                LIMIT 1
                """,
                (pattern_type, key)
            )
            row = await cursor.fetchone()

        return self._row_to_pattern(row) if row else None

    async def update_pattern_usage(self, pattern_id: str) -> bool:
        """
        更新模式使用次数

        Args:
            pattern_id: 模式 ID

        Returns:
            是否更新成功
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.now().isoformat()
            cursor = await db.execute(
                """
                UPDATE patterns
                SET usage_count = usage_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, pattern_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_pattern_confidence(self, pattern_id: str, confidence: float) -> bool:
        """
        更新模式置信度

        Args:
            pattern_id: 模式 ID
            confidence: 新的置信度值

        Returns:
            是否更新成功
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.now().isoformat()
            cursor = await db.execute(
                """
                UPDATE patterns
                SET confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (confidence, now, pattern_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def search_patterns(
        self,
        query: str,
        pattern_type: Optional[str] = None,
        limit: int = 5
    ) -> List[Pattern]:
        """
        搜索模式

        Args:
            query: 搜索查询
            pattern_type: 可选的模式类型过滤器
            limit: 返回数量限制

        Returns:
            匹配的模式列表
        """
        await self._initialize()

        try:
            # 尝试使用全文搜索
            return await self._search_patterns_fts(query, pattern_type, limit)
        except Exception:
            # FTS 不可用，使用 LIKE 搜索
            return await self._search_patterns_like(query, pattern_type, limit)

    async def _search_patterns_fts(
        self,
        query: str,
        pattern_type: Optional[str],
        limit: int
    ) -> List[Pattern]:
        """使用全文搜索模式"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if pattern_type:
                cursor = await db.execute(
                    """
                    SELECT patterns.*
                    FROM patterns
                    JOIN patterns_fts ON patterns.id = patterns_fts.id
                    WHERE patterns_fts MATCH ? AND patterns.pattern_type = ?
                    ORDER BY patterns.confidence DESC, patterns.usage_count DESC
                    LIMIT ?
                    """,
                    (query, pattern_type, limit)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT patterns.*
                    FROM patterns
                    JOIN patterns_fts ON patterns.id = patterns_fts.id
                    WHERE patterns_fts MATCH ?
                    ORDER BY patterns.confidence DESC, patterns.usage_count DESC
                    LIMIT ?
                    """,
                    (query, limit)
                )

            rows = await cursor.fetchall()

        return [self._row_to_pattern(row) for row in rows]

    async def _search_patterns_like(
        self,
        query: str,
        pattern_type: Optional[str],
        limit: int
    ) -> List[Pattern]:
        """使用 LIKE 搜索模式"""
        query_pattern = f"%{query}%"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if pattern_type:
                cursor = await db.execute(
                    """
                    SELECT * FROM patterns
                    WHERE pattern_type = ?
                    AND (key LIKE ? OR value LIKE ? OR description LIKE ? OR tags LIKE ?)
                    ORDER BY confidence DESC, usage_count DESC
                    LIMIT ?
                    """,
                    (pattern_type, query_pattern, query_pattern, query_pattern, query_pattern, limit)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM patterns
                    WHERE key LIKE ? OR value LIKE ? OR description LIKE ? OR tags LIKE ?
                    ORDER BY confidence DESC, usage_count DESC
                    LIMIT ?
                    """,
                    (query_pattern, query_pattern, query_pattern, query_pattern, limit)
                )

            rows = await cursor.fetchall()

        return [self._row_to_pattern(row) for row in rows]

    async def learn_pattern(
        self,
        pattern_type: str,
        key: str,
        value: Any,
        description: str = "",
        tags: Optional[List[str]] = None,
        confidence: float = 0.5
    ) -> str:
        """
        学习新模式（如果已存在则更新）

        Args:
            pattern_type: 模式类型
            key: 模式键
            value: 模式值
            description: 描述
            tags: 标签列表
            confidence: 初始置信度

        Returns:
            模式 ID
        """
        await self._initialize()

        # 检查是否已存在
        existing = await self.get_pattern_by_key(pattern_type, key)

        if existing:
            # 更新现有模式
            existing.usage_count += 1
            # 使用指数加权平均更新置信度
            alpha = 0.3
            existing.confidence = alpha * confidence + (1 - alpha) * existing.confidence
            if description:
                existing.metadata = existing.metadata or {}
                existing.metadata["description"] = description
            if tags:
                existing.metadata = existing.metadata or {}
                existing.metadata["tags"] = list(set(existing.metadata.get("tags", []) + tags))

            await self.save_pattern(existing)
            return existing.id

        # 创建新模式
        pattern_id = f"{pattern_type}:{key}:{uuid.uuid4().hex[:8]}"
        pattern = Pattern(
            id=pattern_id,
            pattern_type=pattern_type,
            key=key,
            value=value,
            confidence=confidence,
            usage_count=1,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            metadata={
                "description": description,
                "tags": tags or []
            }
        )

        await self.save_pattern(pattern)
        return pattern_id

    async def delete_pattern(self, pattern_id: str) -> bool:
        """
        删除模式

        Args:
            pattern_id: 模式 ID

        Returns:
            是否删除成功
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM patterns WHERE id = ?",
                (pattern_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_statistics(self) -> Dict[str, Any]:
        """
        获取全局记忆的统计信息

        Returns:
            统计信息字典
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 总模式数
            cursor = await db.execute("SELECT COUNT(*) as count FROM patterns")
            total = (await cursor.fetchone())["count"]

            # 按类型统计
            cursor = await db.execute(
                """
                SELECT pattern_type, COUNT(*) as count,
                       AVG(confidence) as avg_conf,
                       AVG(usage_count) as avg_usage
                FROM patterns
                GROUP BY pattern_type
                """
            )
            type_stats = {
                row["pattern_type"]: {
                    "count": row["count"],
                    "average_confidence": row["avg_conf"],
                    "average_usage": row["avg_usage"]
                }
                for row in await cursor.fetchall()
            }

            # 最常用的模式
            cursor = await db.execute(
                """
                SELECT id, key, pattern_type, usage_count, confidence
                FROM patterns
                ORDER BY usage_count DESC
                LIMIT 10
                """
            )
            top_used = [
                {
                    "id": row["id"],
                    "key": row["key"],
                    "pattern_type": row["pattern_type"],
                    "usage_count": row["usage_count"],
                    "confidence": row["confidence"]
                }
                for row in await cursor.fetchall()
            ]

            return {
                "total_patterns": total,
                "by_type": type_stats,
                "top_used": top_used
            }

    async def get_tags(self) -> List[str]:
        """
        获取所有标签

        Returns:
            标签列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT DISTINCT tags FROM patterns WHERE tags IS NOT NULL")
            rows = await cursor.fetchall()

        tags_set = set()
        for row in rows:
            try:
                tags = json.loads(row[0])
                tags_set.update(tags)
            except json.JSONDecodeError:
                pass

        return sorted(tags_set)

    def _row_to_pattern(self, row: aiosqlite.Row) -> Pattern:
        """将数据库行转换为 Pattern 对象"""
        metadata = {}
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            pass

        # 解析 value
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, ValueError):
            value = row["value"]

        # 确保 metadata 有必要的字段
        if "tags" not in metadata and row["tags"]:
            try:
                metadata["tags"] = json.loads(row["tags"])
            except json.JSONDecodeError:
                pass

        if "description" not in metadata and row["description"]:
            metadata["description"] = row["description"]

        return Pattern(
            id=row["id"],
            pattern_type=row["pattern_type"],
            key=row["key"],
            value=value,
            confidence=row["confidence"],
            usage_count=row["usage_count"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            metadata=metadata
        )
