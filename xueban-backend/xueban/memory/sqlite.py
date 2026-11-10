import aiosqlite
from typing import Dict, List, Optional, Any
from datetime import datetime

from agentgraph.memory.base import Memory


class SQLiteMemory(Memory):
    """SQLite Memory - Three-layer architecture support"""

    def __init__(self, db_path: str = "agentgraph.db"):
        self.db_path = db_path
        self._initialized = False
        self._db = None

    async def _get_db(self):
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
        return self._db

    async def _initialize(self):
        if self._initialized:
            return
        db = await self._get_db()
        await db.execute("CREATE TABLE IF NOT EXISTS interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, agent_id TEXT NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL, reasoning TEXT, confidence REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_interactions_user_id ON interactions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_interactions_timestamp ON interactions(timestamp DESC)")
        await db.execute("CREATE TABLE IF NOT EXISTS knowledge_notes (key TEXT PRIMARY KEY, content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, word_count INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS wiki_links (id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, target_key TEXT NOT NULL, line_number INTEGER NOT NULL, context TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_key, target_key, line_number))")
        await db.execute("CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source_key TEXT NOT NULL, count INTEGER DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(name, source_key))")
        await db.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_context TEXT, workflow_state TEXT, environment_state TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS session_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, agent_id TEXT, reasoning TEXT, confidence REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, metadata TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS workflows (workflow_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, status TEXT DEFAULT 'pending', definition TEXT, result TEXT, error TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, started_at DATETIME, completed_at DATETIME)")
        await db.execute("CREATE TABLE IF NOT EXISTS agent_patterns (pattern_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0, last_used DATETIME, tags TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, preferred_agents TEXT, interaction_style TEXT DEFAULT 'standard', task_patterns TEXT, session_count INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS task_history (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, user_id TEXT NOT NULL, agent_id TEXT, success BOOLEAN NOT NULL, metadata TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
        await db.commit()
        self._initialized = True

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def save_interaction(self, user_id: str, agent_id: str, input: str, output: str, reasoning: Optional[str] = None, confidence: Optional[float] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT INTO interactions (user_id, agent_id, input, output, reasoning, confidence) VALUES (?, ?, ?, ?, ?, ?)", (user_id, agent_id, input, output, reasoning, confidence))
        await db.commit()

    async def get_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM interactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_summary(self, user_id: str, role: Optional[str] = None) -> str:
        history = await self.get_history(user_id, limit=5)
        if not history:
            return "No previous interactions."
        return f"User {user_id} has {len(history)} recent interactions."

    async def clear_history(self, user_id: str) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("DELETE FROM interactions WHERE user_id = ?", (user_id,))
        await db.commit()

    async def save_note(self, key: str, content: str) -> None:
        await self._initialize()
        db = await self._get_db()
        now = datetime.now().isoformat()
        cursor = await db.execute("SELECT key FROM knowledge_notes WHERE key = ?", (key,))
        if await cursor.fetchone():
            await db.execute("UPDATE knowledge_notes SET content = ?, updated_at = ?, word_count = ? WHERE key = ?", (content, now, len(content.split()), key))
        else:
            await db.execute("INSERT INTO knowledge_notes (key, content, created_at, updated_at, word_count) VALUES (?, ?, ?, ?, ?)", (key, content, now, now, len(content.split())))
        await db.commit()

    async def get_note(self, key: str) -> Optional[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM knowledge_notes WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def search_knowledge(self, query: str, limit: int = 10) -> List[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, content FROM knowledge_notes WHERE content LIKE ? OR key LIKE ? LIMIT ?", (f"%{query}%", f"%{query}%", limit))
        return {"notes": [dict(row) for row in await cursor.fetchall()]}

    async def get_backlinks(self, target_key: str) -> List[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM wiki_links WHERE target_key = ?", (target_key,))
        return [dict(row) for row in await cursor.fetchall()]

    async def save_session(self, session_id: str, user_id: str, data: Dict = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT OR REPLACE INTO sessions (session_id, user_id, project_context, workflow_state, environment_state, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, str(data.get("project_context", {}) if data else {}), str(data.get("workflow_state", {}) if data else {}), str(data.get("environment_state", {}) if data else {}), datetime.now().isoformat()))
        await db.commit()

    async def add_session_message(self, session_id: str, role: str, content: str, agent_id: Optional[str] = None, reasoning: Optional[str] = None, confidence: Optional[float] = None, metadata: Optional[Dict] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT INTO session_messages (session_id, role, content, agent_id, reasoning, confidence, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (session_id, role, content, agent_id, reasoning, confidence, str(metadata) if metadata else None))
        await db.commit()

    async def get_session_messages(self, session_id: str, limit: int = 10) -> List[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM session_messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?", (session_id, limit))
        return [dict(row) for row in await cursor.fetchall()]

    async def save_workflow(self, workflow_id: str, name: str, description: Optional[str] = None, status: str = "pending", definition: Optional[str] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT OR REPLACE INTO workflows (workflow_id, name, description, status, definition, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (workflow_id, name, description, status, definition, datetime.now().isoformat()))
        await db.commit()

    async def update_workflow_status(self, workflow_id: str, status: str, result: Optional[str] = None, error: Optional[str] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        now = datetime.now().isoformat()
        if status == "running":
            await db.execute("UPDATE workflows SET status = ?, started_at = ? WHERE workflow_id = ?", (status, now, workflow_id))
        elif status in ["completed", "failed"]:
            await db.execute("UPDATE workflows SET status = ?, completed_at = ?, result = ?, error = ? WHERE workflow_id = ?", (status, now, result, error, workflow_id))
        else:
            await db.execute("UPDATE workflows SET status = ? WHERE workflow_id = ?", (status, workflow_id))
        await db.commit()

    async def get_active_workflows(self) -> List[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM workflows WHERE status IN ('pending', 'running') ORDER BY created_at DESC")
        return [dict(row) for row in await cursor.fetchall()]

    async def save_pattern(self, pattern_id: str, name: str, description: str, tags: List[str] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        now = datetime.now().isoformat()
        await db.execute("INSERT OR REPLACE INTO agent_patterns (pattern_id, name, description, tags, created_at, last_used) VALUES (?, ?, ?, ?, ?, ?)",
                        (pattern_id, name, description, str(tags), now, now))
        await db.commit()

    async def update_pattern_success(self, pattern_id: str, success: bool) -> None:
        await self._initialize()
        db = await self._get_db()
        cursor = await db.execute("SELECT usage_count, success_rate FROM agent_patterns WHERE pattern_id = ?", (pattern_id,))
        row = await cursor.fetchone()
        if row:
            usage_count, current_rate = row
            new_count = usage_count + 1
            new_rate = (current_rate * usage_count + (1.0 if success else 0.0)) / new_count
            await db.execute("UPDATE agent_patterns SET usage_count = ?, success_rate = ?, last_used = ? WHERE pattern_id = ?",
                           (new_count, new_rate, datetime.now().isoformat(), pattern_id))
            await db.commit()

    async def get_best_patterns(self, task_type: str, limit: int = 3) -> List[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM agent_patterns WHERE name LIKE ? OR description LIKE ? OR tags LIKE ? ORDER BY success_rate DESC, usage_count DESC LIMIT ?",
                                 (f"%{task_type}%", f"%{task_type}%", f"%{task_type}%", limit))
        return [dict(row) for row in await cursor.fetchall()]

    async def save_user_profile(self, user_id: str, preferred_agents: List[str] = None, interaction_style: str = "standard", task_patterns: List[str] = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT OR REPLACE INTO user_profiles (user_id, preferred_agents, interaction_style, task_patterns, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (user_id, str(preferred_agents), interaction_style, str(task_patterns), datetime.now().isoformat()))
        await db.commit()

    async def get_user_profile(self, user_id: str) -> Optional[Dict]:
        await self._initialize()
        db = await self._get_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def record_task(self, task_id: str, user_id: str, agent_id: Optional[str], success: bool, metadata: Dict = None) -> None:
        await self._initialize()
        db = await self._get_db()
        await db.execute("INSERT INTO task_history (task_id, user_id, agent_id, success, metadata) VALUES (?, ?, ?, ?, ?)",
                        (task_id, user_id, agent_id, success, str(metadata) if metadata else None))
        await db.commit()

    async def get_task_statistics(self, user_id: Optional[str] = None) -> Dict:
        await self._initialize()
        db = await self._get_db()
        if user_id:
            cursor = await db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count FROM task_history WHERE user_id = ?", (user_id,))
        else:
            cursor = await db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count FROM task_history")
        row = await cursor.fetchone()
        if row:
            total, success_count = row
            return {"total": total, "success": success_count, "failure": total - success_count, "success_rate": success_count / total if total > 0 else 0.0}
        return {"total": 0, "success": 0, "failure": 0, "success_rate": 0.0}
