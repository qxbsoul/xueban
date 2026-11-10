"""
用户级记忆 - SQLite 持久化，跨会话共享

UserMemory 提供用户级别的持久化记忆，数据存储在 SQLite 数据库中，
可以在多个会话之间共享。
"""

import aiosqlite
import json
import uuid
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .base import BaseMemory, Interaction


@dataclass
class ContextResult:
    """上下文检索结果"""
    formatted_context: str
    interactions: List[Interaction]
    tokens_used: int
    truncated: bool = False


class UserMemory(BaseMemory):
    """
    用户级记忆实现

    特点：
    - 数据持久化到 SQLite 数据库
    - 跨会话共享
    - 支持复杂的查询和搜索
    - 适合存储用户的历史交互和偏好
    """

    # SQL 语句
    CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT,
            input TEXT NOT NULL,
            output TEXT NOT NULL,
            reasoning TEXT,
            confidence REAL DEFAULT 0.0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
    """

    CREATE_INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS idx_user_id ON interactions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_id ON interactions(agent_id)",
        "CREATE INDEX IF NOT EXISTS idx_session_id ON interactions(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_timestamp ON interactions(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_input_output ON interactions(input, output)"
    ]

    CREATE_FTS_TABLE_SQL = """
        CREATE VIRTUAL TABLE IF NOT EXISTS interactions_fts USING fts5(
            id,
            user_id,
            agent_id,
            input,
            output,
            reasoning,
            metadata,
            content='interactions',
            content_rowid='rowid'
        )
    """

    CREATE_FTS_TRIGGER_SQL = """
        CREATE TRIGGER IF NOT EXISTS interactions_fts_insert AFTER INSERT ON interactions
        BEGIN
            INSERT INTO interactions_fts(rowid, id, user_id, agent_id, input, output, reasoning, metadata)
            VALUES (NEW.rowid, NEW.id, NEW.user_id, NEW.agent_id, NEW.input, NEW.output, NEW.reasoning, NEW.metadata);
        END;

        CREATE TRIGGER IF NOT EXISTS interactions_fts_delete AFTER DELETE ON interactions
        BEGIN
            DELETE FROM interactions_fts WHERE rowid = OLD.rowid;
        END;

        CREATE TRIGGER IF NOT EXISTS interactions_fts_update AFTER UPDATE ON interactions
        BEGIN
            DELETE FROM interactions_fts WHERE rowid = OLD.rowid;
            INSERT INTO interactions_fts(rowid, id, user_id, agent_id, input, output, reasoning, metadata)
            VALUES (NEW.rowid, NEW.id, NEW.user_id, NEW.agent_id, NEW.input, NEW.output, NEW.reasoning, NEW.metadata);
        END;
    """

    def __init__(self, db_path: str = "agentgraph_memory.db"):
        """
        初始化用户记忆

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self._initialized = False
        self._lock = None  # 可以使用 asyncio.Lock 进行并发控制

    async def _initialize(self) -> None:
        """初始化数据库表和索引"""
        if self._initialized:
            return

        # 确保数据库目录存在
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # 创建主表
            await db.execute(self.CREATE_TABLE_SQL)

            # 创建索引
            for index_sql in self.CREATE_INDEX_SQL:
                await db.execute(index_sql)

            # 尝试创建全文搜索表（SQLite 需要启用 FTS5）
            try:
                await db.execute(self.CREATE_FTS_TABLE_SQL)
                await db.executescript(self.CREATE_FTS_TRIGGER_SQL)
            except aiosqlite.Error:
                # FTS5 不可用，继续运行但不支持全文搜索
                pass

            await db.commit()

        self._initialized = True

    async def save(self, interaction: Interaction) -> None:
        """
        保存交互记录到数据库

        Args:
            interaction: 交互记录对象
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO interactions
                (id, user_id, agent_id, session_id, input, output, reasoning, confidence, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.id,
                    interaction.user_id,
                    interaction.agent_id,
                    interaction.session_id,
                    interaction.input,
                    interaction.output,
                    interaction.reasoning,
                    interaction.confidence,
                    interaction.timestamp.isoformat() if interaction.timestamp else None,
                    json.dumps(interaction.metadata) if interaction.metadata else None
                )
            )
            await db.commit()

    async def get_recent(self, user_id: str, limit: int = 10) -> List[Interaction]:
        """
        获取用户最近的交互记录

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            最近的交互记录列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM interactions
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, limit)
            )
            rows = await cursor.fetchall()

        return [self._row_to_interaction(row) for row in rows]

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
        await self._initialize()

        try:
            # 尝试使用全文搜索
            return await self._search_fts(query, user_id, limit)
        except Exception:
            # FTS 不可用，使用 LIKE 搜索
            return await self._search_like(query, user_id, limit)

    async def _search_fts(
        self,
        query: str,
        user_id: Optional[str],
        limit: int
    ) -> List[Interaction]:
        """使用全文搜索"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if user_id:
                cursor = await db.execute(
                    """
                    SELECT interactions.*
                    FROM interactions
                    JOIN interactions_fts ON interactions.id = interactions_fts.id
                    WHERE interactions_fts MATCH ? AND interactions.user_id = ?
                    ORDER BY interactions.timestamp DESC
                    LIMIT ?
                    """,
                    (query, user_id, limit)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT interactions.*
                    FROM interactions
                    JOIN interactions_fts ON interactions.id = interactions_fts.id
                    WHERE interactions_fts MATCH ?
                    ORDER BY interactions.timestamp DESC
                    LIMIT ?
                    """,
                    (query, limit)
                )

            rows = await cursor.fetchall()

        return [self._row_to_interaction(row) for row in rows]

    async def _search_like(
        self,
        query: str,
        user_id: Optional[str],
        limit: int
    ) -> List[Interaction]:
        """使用 LIKE 搜索"""
        query_pattern = f"%{query}%"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if user_id:
                cursor = await db.execute(
                    """
                    SELECT * FROM interactions
                    WHERE user_id = ?
                    AND (input LIKE ? OR output LIKE ? OR reasoning LIKE ?)
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (user_id, query_pattern, query_pattern, query_pattern, limit)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM interactions
                    WHERE input LIKE ? OR output LIKE ? OR reasoning LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (query_pattern, query_pattern, query_pattern, limit)
                )

            rows = await cursor.fetchall()

        return [self._row_to_interaction(row) for row in rows]

    async def get_user_history(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Interaction]:
        """
        获取用户的历史交互记录（支持分页）

        Args:
            user_id: 用户 ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            交互记录列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM interactions
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset)
            )
            rows = await cursor.fetchall()

        return [self._row_to_interaction(row) for row in rows]

    async def get_by_id(self, interaction_id: str) -> Optional[Interaction]:
        """
        根据 ID 获取交互记录

        Args:
            interaction_id: 交互记录 ID

        Returns:
            交互记录对象，如果不存在则返回 None
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM interactions WHERE id = ?",
                (interaction_id,)
            )
            row = await cursor.fetchone()

        return self._row_to_interaction(row) if row else None

    async def get_by_session(self, session_id: str) -> List[Interaction]:
        """
        获取会话的所有交互记录

        Args:
            session_id: 会话 ID

        Returns:
            交互记录列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM interactions
                WHERE session_id = ?
                ORDER BY timestamp ASC
                """,
                (session_id,)
            )
            rows = await cursor.fetchall()

        return [self._row_to_interaction(row) for row in rows]

    async def get_statistics(self, user_id: str) -> Dict[str, Any]:
        """
        获取用户的统计信息

        Args:
            user_id: 用户 ID

        Returns:
            统计信息字典
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 总交互数
            cursor = await db.execute(
                "SELECT COUNT(*) as count FROM interactions WHERE user_id = ?",
                (user_id,)
            )
            total = (await cursor.fetchone())["count"]

            if total == 0:
                return {
                    "user_id": user_id,
                    "total": 0,
                    "sessions": 0,
                    "agents": {},
                    "average_confidence": 0.0
                }

            # 平均置信度
            cursor = await db.execute(
                "SELECT AVG(confidence) as avg_conf FROM interactions WHERE user_id = ?",
                (user_id,)
            )
            avg_confidence = (await cursor.fetchone())["avg_conf"] or 0.0

            # 会话数
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT session_id) as count FROM interactions WHERE user_id = ?",
                (user_id,)
            )
            sessions = (await cursor.fetchone())["count"]

            # 各 Agent 的交互数
            cursor = await db.execute(
                """
                SELECT agent_id, COUNT(*) as count
                FROM interactions
                WHERE user_id = ?
                GROUP BY agent_id
                ORDER BY count DESC
                """,
                (user_id,)
            )
            agents = {row["agent_id"]: row["count"] for row in await cursor.fetchall()}

            return {
                "user_id": user_id,
                "total": total,
                "sessions": sessions,
                "agents": agents,
                "average_confidence": avg_confidence
            }

    async def clear_history(self, user_id: str) -> int:
        """
        清除用户的所有历史记录

        Args:
            user_id: 用户 ID

        Returns:
            删除的记录数
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM interactions WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()
            return cursor.rowcount

    async def clear_session(self, session_id: str) -> int:
        """
        清除特定会话的所有记录

        Args:
            session_id: 会话 ID

        Returns:
            删除的记录数
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM interactions WHERE session_id = ?",
                (session_id,)
            )
            await db.commit()
            return cursor.rowcount

    async def get_all_users(self) -> List[str]:
        """
        获取所有用户 ID

        Returns:
            用户 ID 列表
        """
        await self._initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT DISTINCT user_id FROM interactions"
            )
            rows = await cursor.fetchall()

        return [row[0] for row in rows]


class UserMemoryFactory:
    """用户记忆工厂，用于管理多个用户的记忆实例"""

    _instances: Dict[str, "UserMemory"] = {}

    @classmethod
    def get(cls, user_id: str, db_path: str = "agentgraph_memory.db") -> "UserMemory":
        """
        获取或创建用户记忆实例

        Args:
            user_id: 用户 ID
            db_path: 数据库路径

        Returns:
            UserMemory 实例
        """
        cache_key = f"{db_path}:{user_id}"
        if cache_key not in cls._instances:
            cls._instances[cache_key] = UserMemory(db_path)
        return cls._instances[cache_key]

    @classmethod
    def clear(cls) -> None:
        """清除所有缓存实例"""
        cls._instances.clear()

    def _row_to_interaction(self, row: aiosqlite.Row) -> Interaction:
        """将数据库行转换为 Interaction 对象"""
        return Interaction(
            id=row["id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            input=row["input"],
            output=row["output"],
            reasoning=row["reasoning"],
            confidence=row["confidence"],
            timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
            metadata=json.loads(row["metadata"]) if row["metadata"] else None
        )
