import redis.asyncio as redis
from typing import Dict, List, Optional, Any
import json
import re
from datetime import datetime

from agentgraph.memory.base import Memory


class RedisMemory(Memory):
    """
    Redis Memory 实现 - 支持三层架构

    Key 命名空间：
    - agentgraph:{user_id}:history - 交互历史
    - agentgraph:knowledge:{key} - 知识层笔记
    - agentgraph:links:{key} - 双向链接索引
    - agentgraph:tags:{name} - 标签索引
    - agentgraph:session:{session_id} - 上下文层会话
    - agentgraph:session:{session_id}:messages - 会话消息
    - agentgraph:workflow:{workflow_id} - 工作流
    - agentgraph:semantic:pattern:{pattern_id} - 语义层模式
    - agentgraph:semantic:user:{user_id} - 用户画像
    - agentgraph:semantic:tasks - 任务历史
    - agentgraph:search:* - 语义搜索索引（使用 Redis Search 或自定义索引）
    """

    WIKI_LINK_PATTERN = r'\[\[([^\]]+)\]\]'
    TAG_PATTERN = r'#([a-zA-Z0-9_\-]+)'

    def __init__(self, url: str = "redis://localhost:6379"):
        self.url = url
        self._client: Optional[redis.Redis] = None

    async def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self.url, decode_responses=True)
        return self._client

    # ========== 原有接口（保持兼容）==========

    async def save_interaction(
        self,
        user_id: str,
        agent_id: str,
        input: str,
        output: str,
        reasoning: Optional[str] = None,
        confidence: Optional[float] = None
    ) -> None:
        client = await self._get_client()

        interaction = {
            "user_id": user_id,
            "agent_id": agent_id,
            "input": input,
            "output": output,
            "reasoning": reasoning,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat()
        }

        key = f"agentgraph:{user_id}:history"
        await client.lpush(key, json.dumps(interaction))
        await client.ltrim(key, 0, 99)

        # 同时索引到语义搜索
        await self._index_for_search("interaction", interaction, user_id)

    async def get_history(
        self,
        user_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        client = await self._get_client()

        key = f"agentgraph:{user_id}:history"
        items = await client.lrange(key, 0, limit - 1)

        history = []
        for item in items:
            try:
                interaction = json.loads(item)
                history.append(interaction)
            except json.JSONDecodeError:
                continue

        return history

    async def get_summary(
        self,
        user_id: str,
        role: Optional[str] = None
    ) -> str:
        history = await self.get_history(user_id, limit=5)

        if not history:
            return "No previous interactions."

        summary = f"User {user_id} has {len(history)} recent interactions:\n"
        for item in history:
            summary += f"- {item['agent_id']}: {item['output'][:50]}...\n"

        return summary

    async def clear_history(self, user_id: str) -> None:
        client = await self._get_client()

        key = f"agentgraph:{user_id}:history"
        await client.delete(key)

    # ========== 知识层接口 ==========

    async def save_note(self, key: str, content: str) -> None:
        """保存笔记到知识层"""
        client = await self._get_client()

        note_key = f"agentgraph:knowledge:{key}"
        note_data = {
            "key": key,
            "content": content,
            "word_count": len(content.split()),
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        await client.hset(note_key, mapping=note_data)

        # 提取并索引链接
        await self._extract_and_index_links(key, content, client)

        # 提取并索引标签
        await self._extract_and_index_tags(key, content, client)

        # 索引用于搜索
        await self._index_for_search("knowledge", note_data, key)

    async def get_note(self, key: str) -> Optional[Dict]:
        """获取笔记"""
        client = await self._get_client()

        note_key = f"agentgraph:knowledge:{key}"
        note = await client.hgetall(note_key)

        if note:
            # 获取链接
            links_key = f"agentgraph:links:{key}"
            links_data = await client.lrange(links_key, 0, -1)
            note["links"] = [json.loads(l) for l in links_data if l]

            # 获取标签
            tags_key = f"agentgraph:tags:note:{key}"
            tags_data = await client.smembers(tags_key)
            note["tags"] = list(tags_data)

            return note
        return None

    async def search_knowledge(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索知识层"""
        client = await self._get_client()

        results = []

        # 1. 搜索笔记内容
        search_pattern = f"agentgraph:knowledge:*"
        keys = []
        async for key in client.scan_iter(match=search_pattern, count=100):
            if len(keys) >= limit:
                break
            note = await client.hgetall(key)
            if note:
                content = note.get("content", "")
                note_key = note.get("key", "")
                if query.lower() in content.lower() or query.lower() in note_key.lower():
                    results.append({
                        "type": "note",
                        "key": note_key,
                        "content": content[:200],
                        "preview": self._get_preview(content, query)
                    })

        # 2. 搜索标签
        tag_key = f"agentgraph:tags:name:{query.lower()}"
        tag_notes = await client.smembers(tag_key)
        for note_key in tag_notes:
            if len(results) >= limit:
                break
            if not any(r["key"] == note_key for r in results):
                results.append({
                    "type": "tag",
                    "key": note_key,
                    "preview": f"包含标签 #{query}"
                })

        # 3. 搜索链接
        link_key = f"agentgraph:links:target:{query.lower()}"
        link_sources = await client.smembers(link_key)
        for source_key in link_sources:
            if len(results) >= limit:
                break
            if not any(r["key"] == source_key for r in results):
                results.append({
                    "type": "link",
                    "key": source_key,
                    "preview": f"链接到 [[{query}]]"
                })

        return results[:limit]

    async def get_backlinks(self, target_key: str) -> List[Dict]:
        """获取反向链接"""
        client = await self._get_client()

        backlinks = []
        link_key = f"agentgraph:links:target:{target_key.lower()}"
        sources = await client.smembers(link_key)

        for source_key in sources:
            links_key = f"agentgraph:links:{source_key}"
            links_data = await client.lrange(links_key, 0, -1)
            for link_str in links_data:
                try:
                    link = json.loads(link_str)
                    if link["target"].lower() == target_key.lower():
                        backlinks.append(link)
                except json.JSONDecodeError:
                    continue

        return backlinks

    # ========== 上下文层接口 ==========

    async def save_session(
        self,
        session_id: str,
        user_id: str,
        data: Dict = None
    ) -> None:
        """保存或更新会话"""
        client = await self._get_client()

        session_key = f"agentgraph:session:{session_id}"
        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "project_context": json.dumps(data.get("project_context", {}) if data else {}),
            "workflow_state": json.dumps(data.get("workflow_state", {}) if data else {}),
            "environment_state": json.dumps(data.get("environment_state", {}) if data else {}),
            "updated_at": datetime.now().isoformat()
        }

        await client.hset(session_key, mapping=session_data)

        # 添加到用户会话列表
        user_sessions_key = f"agentgraph:user:{user_id}:sessions"
        await client.sadd(user_sessions_key, session_id)

    async def add_session_message(
        self,
        session_id: str,
        role: str,
        content: str,
        agent_id: Optional[str] = None,
        reasoning: Optional[str] = None,
        confidence: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """添加会话消息"""
        client = await self._get_client()

        messages_key = f"agentgraph:session:{session_id}:messages"
        message = {
            "role": role,
            "content": content,
            "agent_id": agent_id,
            "reasoning": reasoning,
            "confidence": confidence,
            "metadata": json.dumps(metadata) if metadata else None,
            "timestamp": datetime.now().isoformat()
        }

        await client.lpush(messages_key, json.dumps(message))
        await client.ltrim(messages_key, 0, 99)

        # 索引用于搜索
        await self._index_for_search("message", message, session_id)

    async def get_session_messages(self, session_id: str, limit: int = 10) -> List[Dict]:
        """获取会话消息"""
        client = await self._get_client()

        messages_key = f"agentgraph:session:{session_id}:messages"
        items = await client.lrange(messages_key, 0, limit - 1)

        messages = []
        for item in items:
            try:
                msg = json.loads(item)
                messages.append(msg)
            except json.JSONDecodeError:
                continue

        return messages

    async def save_workflow(
        self,
        workflow_id: str,
        name: str,
        description: Optional[str] = None,
        status: str = "pending",
        definition: Optional[str] = None
    ) -> None:
        """保存工作流"""
        client = await self._get_client()

        workflow_key = f"agentgraph:workflow:{workflow_id}"
        workflow_data = {
            "workflow_id": workflow_id,
            "name": name,
            "description": description or "",
            "status": status,
            "definition": definition or "",
            "created_at": datetime.now().isoformat()
        }

        await client.hset(workflow_key, mapping=workflow_data)

        # 添加到活跃工作流集合
        if status in ["pending", "running"]:
            await client.sadd("agentgraph:workflows:active", workflow_id)

    async def update_workflow_status(
        self,
        workflow_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """更新工作流状态"""
        client = await self._get_client()

        workflow_key = f"agentgraph:workflow:{workflow_id}"

        await client.hset(workflow_key, "status", status)

        if status == "running":
            await client.hset(workflow_key, "started_at", datetime.now().isoformat())
            await client.sadd("agentgraph:workflows:active", workflow_id)
        elif status in ["completed", "failed"]:
            await client.hset(workflow_key, "completed_at", datetime.now().isoformat())
            if result:
                await client.hset(workflow_key, "result", result)
            if error:
                await client.hset(workflow_key, "error", error)
            await client.srem("agentgraph:workflows:active", workflow_id)

    async def get_active_workflows(self) -> List[Dict]:
        """获取活跃的工作流"""
        client = await self._get_client()

        active_workflow_ids = await client.smembers("agentgraph:workflows:active")

        workflows = []
        for wf_id in active_workflow_ids:
            workflow_key = f"agentgraph:workflow:{wf_id}"
            workflow = await client.hgetall(workflow_key)
            if workflow:
                workflows.append(workflow)

        return workflows

    # ========== 语义层接口 ==========

    async def save_pattern(
        self,
        pattern_id: str,
        name: str,
        description: str,
        tags: List[str] = None
    ) -> None:
        """保存或更新模式"""
        client = await self._get_client()

        pattern_key = f"agentgraph:semantic:pattern:{pattern_id}"
        pattern_data = {
            "pattern_id": pattern_id,
            "name": name,
            "description": description,
            "tags": json.dumps(tags or []),
            "success_rate": "0.0",
            "usage_count": "0",
            "created_at": datetime.now().isoformat(),
            "last_used": datetime.now().isoformat()
        }

        await client.hset(pattern_key, mapping=pattern_data)

        # 添加到模式集合
        await client.sadd("agentgraph:semantic:patterns", pattern_id)

        # 索引标签
        if tags:
            for tag in tags:
                tag_key = f"agentgraph:semantic:patterns:tag:{tag.lower()}"
                await client.sadd(tag_key, pattern_id)

    async def update_pattern_success(self, pattern_id: str, success: bool) -> None:
        """更新模式成功率"""
        client = await self._get_client()

        pattern_key = f"agentgraph:semantic:pattern:{pattern_id}"

        # 获取当前值
        usage_count = await client.hincrby(pattern_key, "usage_count", 1)
        current_rate = float(await client.hget(pattern_key, "success_rate") or "0.0")

        # 更新成功率
        new_rate = (current_rate * (usage_count - 1) + (1.0 if success else 0.0)) / usage_count
        await client.hset(pattern_key, "success_rate", str(new_rate))
        await client.hset(pattern_key, "last_used", datetime.now().isoformat())

    async def get_best_patterns(self, task_type: str, limit: int = 3) -> List[Dict]:
        """获取针对特定任务类型的最佳模式"""
        client = await self._get_client()

        candidates = set()

        # 通过标签查找
        tag_key = f"agentgraph:semantic:patterns:tag:{task_type.lower()}"
        pattern_ids = await client.smembers(tag_key)
        candidates.update(pattern_ids)

        # 如果候选不足，搜索所有模式的名称和描述
        if len(candidates) < limit:
            all_pattern_ids = await client.smembers("agentgraph:semantic:patterns")
            for pid in all_pattern_ids:
                pattern_key = f"agentgraph:semantic:pattern:{pid}"
                pattern = await client.hgetall(pattern_key)
                if (task_type.lower() in pattern.get("name", "").lower() or
                    task_type.lower() in pattern.get("description", "").lower()):
                    candidates.add(pid)

        # 获取完整信息并排序
        patterns = []
        for pid in candidates:
            pattern_key = f"agentgraph:semantic:pattern:{pid}"
            pattern = await client.hgetall(pattern_key)
            if pattern:
                pattern["success_rate"] = float(pattern.get("success_rate", 0))
                pattern["usage_count"] = int(pattern.get("usage_count", 0))
                patterns.append(pattern)

        # 按成功率和使用次数排序
        patterns.sort(key=lambda p: (p.get("success_rate", 0), p.get("usage_count", 0)), reverse=True)

        return patterns[:limit]

    async def save_user_profile(
        self,
        user_id: str,
        preferred_agents: List[str] = None,
        interaction_style: str = "standard",
        task_patterns: List[str] = None
    ) -> None:
        """保存用户画像"""
        client = await self._get_client()

        profile_key = f"agentgraph:semantic:user:{user_id}"
        profile_data = {
            "user_id": user_id,
            "preferred_agents": json.dumps(preferred_agents or []),
            "interaction_style": interaction_style,
            "task_patterns": json.dumps(task_patterns or []),
            "session_count": "0",
            "updated_at": datetime.now().isoformat()
        }

        await client.hset(profile_key, mapping=profile_data)

    async def get_user_profile(self, user_id: str) -> Optional[Dict]:
        """获取用户画像"""
        client = await self._get_client()

        profile_key = f"agentgraph:semantic:user:{user_id}"
        profile = await client.hgetall(profile_key)

        if profile:
            try:
                profile["preferred_agents"] = json.loads(profile.get("preferred_agents", "[]"))
                profile["task_patterns"] = json.loads(profile.get("task_patterns", "[]"))
                profile["session_count"] = int(profile.get("session_count", 0))
            except json.JSONDecodeError:
                pass
            return profile
        return None

    async def record_task(
        self,
        task_id: str,
        user_id: str,
        agent_id: Optional[str],
        success: bool,
        metadata: Dict = None
    ) -> None:
        """记录任务完成情况"""
        client = await self._get_client()

        task_key = f"agentgraph:semantic:task:{task_id}"
        task_data = {
            "task_id": task_id,
            "user_id": user_id,
            "agent_id": agent_id or "",
            "success": "true" if success else "false",
            "metadata": json.dumps(metadata) if metadata else "",
            "timestamp": datetime.now().isoformat()
        }

        await client.hset(task_key, mapping=task_data)

        # 添加到用户任务列表
        user_tasks_key = f"agentgraph:semantic:user:{user_id}:tasks"
        await client.lpush(user_tasks_key, task_id)
        await client.ltrim(user_tasks_key, 0, 99)

    async def get_task_statistics(self, user_id: Optional[str] = None) -> Dict:
        """获取任务统计"""
        client = await self._get_client()

        total = 0
        success_count = 0

        if user_id:
            user_tasks_key = f"agentgraph:semantic:user:{user_id}:tasks"
            task_ids = await client.lrange(user_tasks_key, 0, 99)
            for task_id in task_ids:
                task_key = f"agentgraph:semantic:task:{task_id}"
                task = await client.hgetall(task_key)
                if task:
                    total += 1
                    if task.get("success") == "true":
                        success_count += 1
        else:
            # 扫描所有任务
            task_pattern = "agentgraph:semantic:task:*"
            async for key in client.scan_iter(match=task_pattern, count=100):
                task = await client.hgetall(key)
                if task:
                    total += 1
                    if task.get("success") == "true":
                        success_count += 1

        return {
            "total": total,
            "success": success_count,
            "failure": total - success_count,
            "success_rate": success_count / total if total > 0 else 0.0
        }

    # ========== 辅助方法 ==========

    async def _extract_and_index_links(self, key: str, content: str, client: redis.Redis) -> None:
        """提取并索引链接"""
        links_key = f"agentgraph:links:{key}"
        await client.delete(links_key)

        for match in re.finditer(self.WIKI_LINK_PATTERN, content):
            target = match.group(1)
            line_number = content[:match.start()].count('\n') + 1
            lines = content.split('\n')
            context = lines[min(line_number - 1, len(lines) - 1)].strip()

            link_data = {
                "source": key,
                "target": target,
                "line_number": line_number,
                "context": context
            }
            await client.rpush(links_key, json.dumps(link_data))

            # 反向索引
            backlink_key = f"agentgraph:links:target:{target.lower()}"
            await client.sadd(backlink_key, key)

    async def _extract_and_index_tags(self, key: str, content: str, client: redis.Redis) -> None:
        """提取并索引标签"""
        # 清理旧索引
        async for old_key in client.scan_iter(match=f"agentgraph:tags:note:{key}*"):
            await client.delete(old_key)

        note_tags_key = f"agentgraph:tags:note:{key}"
        tag_counts = {}

        for match in re.finditer(self.TAG_PATTERN, content):
            tag_name = match.group(1).lower()
            tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1

        # 保存笔记的标签
        for tag_name in tag_counts:
            await client.sadd(note_tags_key, tag_name)
            # 反向索引
            tag_notes_key = f"agentgraph:tags:name:{tag_name}"
            await client.sadd(tag_notes_key, key)

    async def _index_for_search(self, doc_type: str, doc_data: Dict, doc_id: str) -> None:
        """为语义搜索建立索引"""
        # 这里可以集成 Redis Search 模块或使用自定义倒排索引
        client = await self._get_client()

        # 提取关键词
        text = ""
        if doc_type == "knowledge":
            text = doc_data.get("content", "")
        elif doc_type == "interaction":
            text = f"{doc_data.get('input', '')} {doc_data.get('output', '')}"
        elif doc_type == "message":
            text = doc_data.get("content", "")

        # 简单分词和索引
        words = re.findall(r'\w+', text.lower())
        for word in set(words):
            if len(word) > 2:  # 忽略短词
                word_index_key = f"agentgraph:search:index:{word}"
                await client.zadd(
                    word_index_key,
                    {f"{doc_type}:{doc_id}": datetime.now().timestamp()}
                )

    def _get_preview(self, content: str, query: str, max_length: int = 150) -> str:
        """获取搜索结果预览"""
        query_lower = query.lower()
        content_lower = content.lower()
        index = content_lower.find(query_lower)

        if index == -1:
            return content[:max_length] + ("..." if len(content) > max_length else "")

        start = max(0, index - 50)
        end = min(len(content), index + len(query) + 100)

        preview = "..." if start > 0 else ""
        preview += content[start:end]
        preview += "..." if end < len(content) else ""

        return preview

    async def close(self):
        """关闭 Redis 连接"""
        if self._client:
            await self._client.close()
            self._client = None
