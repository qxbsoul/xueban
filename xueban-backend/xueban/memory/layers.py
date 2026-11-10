"""
三层 Memory 架构

根据 SHADOW_CLAW_DESIGN.md 中的三层架构设计：

1. Knowledge Layer (知识层)
   - 用户笔记（MD 文件）
   - 双向链接（[[WikiLink]]）
   - 标签系统（#Tag）
   - 文件元数据

2. Context Layer (上下文层)
   - 会话记忆（当前对话上下文）
   - 项目上下文（代码库结构、依赖）
   - 工作流状态（进行中的任务）
   - 环境状态（已打开文件、光标位置）

3. Semantic Layer (语义层)
   - Agent 记忆（工作方式、偏好）
   - 用户画像（交互模式、历史）
   - 任务模式（成功/失败案例）
   - 模式库（可复用的解决方案）
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from enum import Enum
import re
from dataclasses import dataclass
from datetime import datetime


class LayerType(Enum):
    """Memory 层级类型"""
    KNOWLEDGE = "knowledge"    # 知识层 - 用户笔记
    CONTEXT = "context"      # 上下文层 - 会话记忆
    SEMANTIC = "semantic"    # 语义层 - Agent记忆


@dataclass
class WikiLink:
    """双向链接信息"""
    target: str              # 链接目标文件名
    source: str              # 来源文件名
    line_number: int         # 行号
    context: str             # 链接上下文


@dataclass
class Tag:
    """标签信息"""
    name: str                # 标签名
    source: str              # 来源文件
    count: int               # 使用次数


@dataclass
class NoteMetadata:
    """笔记元数据"""
    file_path: str           # 文件路径
    created_at: datetime     # 创建时间
    modified_at: datetime    # 修改时间
    word_count: int          # 字数
    tags: List[Tag]          # 标签列表
    wiki_links: List[WikiLink]  # 双向链接


@dataclass
class SessionContext:
    """会话上下文"""
    session_id: str          # 会话 ID
    user_id: str             # 用户 ID
    messages: List[Dict]     # 消息列表
    project_context: Dict    # 项目上下文
    workflow_state: Dict     # 工作流状态
    environment_state: Dict  # 环境状态


@dataclass
class AgentPattern:
    """Agent 模式（成功/失败案例）"""
    pattern_id: str          # 模式 ID
    name: str                # 模式名称
    description: str         # 描述
    success_rate: float      # 成功率
    usage_count: int         # 使用次数
    last_used: datetime      # 最后使用时间
    tags: List[str]          # 标签


@dataclass
class UserProfile:
    """用户画像"""
    user_id: str             # 用户 ID
    preferred_agents: List[str]   # 偏好的 Agent
    interaction_style: str       # 交互风格
    task_patterns: List[str]     # 常用任务模式
    session_count: int           # 会话次数


class MemoryLayer(ABC):
    """Memory 层级抽象基类"""

    @abstractmethod
    async def save(self, key: str, value: Any) -> None:
        """保存数据"""
        pass

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """获取数据"""
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索数据"""
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """删除数据"""
        pass


class KnowledgeLayer(MemoryLayer):
    """
    知识层 - 用户笔记管理

    负责：
    - MD 文件存储和检索
    - 双向链接（[[WikiLink]]）识别和建立
    - 标签系统（#Tag）管理
    - 文件元数据管理
    """

    WIKI_LINK_PATTERN = r'\[\[([^\]]+)\]\]'
    TAG_PATTERN = r'#([a-zA-Z0-9_\-]+)'

    def __init__(self, base_path: str = "./knowledge"):
        self.base_path = base_path
        self._notes: Dict[str, Dict] = {}
        self._links: Dict[str, List[WikiLink]] = {}
        self._tags: Dict[str, List[Tag]] = {}

    async def save(self, key: str, value: Any) -> None:
        """保存笔记"""
        if isinstance(value, str):
            # 提取元数据
            metadata = self._extract_metadata(value, key)
            self._notes[key] = {
                "content": value,
                "metadata": metadata,
                "last_updated": datetime.now().isoformat()
            }
            # 更新链接索引
            await self._update_links(key, value)
            # 更新标签索引
            await self._update_tags(key, value)

    async def get(self, key: str) -> Optional[Any]:
        """获取笔记"""
        return self._notes.get(key)

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索笔记（按内容、标签、链接）"""
        results = []
        query_lower = query.lower()

        # 按内容搜索
        for key, note in self._notes.items():
            content = note["content"].lower()
            if query_lower in content:
                results.append({
                    "key": key,
                    "type": "content",
                    "preview": self._get_preview(note["content"], query),
                    "score": content.count(query_lower)
                })

        # 按标签搜索
        if query in self._tags:
            for tag_info in self._tags[query]:
                if tag_info.source not in [r["key"] for r in results]:
                    results.append({
                        "key": tag_info.source,
                        "type": "tag",
                        "preview": f"包含标签 #{tag_info.name}",
                        "score": 5
                    })

        # 按链接搜索
        if query in self._links:
            for link in self._links[query]:
                if link.source not in [r["key"] for r in results]:
                    note = self._notes.get(link.source)
                    if note:
                        results.append({
                            "key": link.source,
                            "type": "link",
                            "preview": f"链接到 [[{link.target}]]",
                            "score": 3
                        })

        # 排序并限制结果
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def delete(self, key: str) -> bool:
        """删除笔记"""
        if key in self._notes:
            # 清理链接
            if key in self._links:
                del self._links[key]
            # 清理其他笔记指向该笔记的链接
            for link_key in self._links:
                self._links[link_key] = [l for l in self._links[link_key] if l.target != key]
            del self._notes[key]
            return True
        return False

    async def get_backlinks(self, target_key: str) -> List[WikiLink]:
        """获取反向链接（指向该笔记的所有链接）"""
        backlinks = []
        for source, links in self._links.items():
            for link in links:
                if link.target == target_key:
                    backlinks.append(link)
        return backlinks

    async def get_related_notes(self, key: str, limit: int = 5) -> List[Dict]:
        """获取相关笔记（通过标签和链接）"""
        note = self._notes.get(key)
        if not note:
            return []

        related = set()
        # 通过链接获取
        if key in self._links:
            for link in self._links[key]:
                related.add(link.target)
        # 通过反向链接获取
        backlinks = await self.get_backlinks(key)
        for link in backlinks:
            related.add(link.source)
        # 通过标签获取
        for tag_name in note["metadata"]["tags"]:
            for tag_info in self._tags.get(tag_name, []):
                if tag_info.source != key:
                    related.add(tag_info.source)

        results = []
        for related_key in related:
            if related_key in self._notes:
                results.append({
                    "key": related_key,
                    "content": self._notes[related_key]["content"][:200]
                })

        return results[:limit]

    async def _update_links(self, key: str, content: str) -> None:
        """更新链接索引"""
        links = []
        for match in re.finditer(self.WIKI_LINK_PATTERN, content):
            target = match.group(1)
            line_number = content[:match.start()].count('\n') + 1
            # 获取上下文
            lines = content.split('\n')
            context = lines[min(line_number - 1, len(lines) - 1)]
            links.append(WikiLink(
                target=target,
                source=key,
                line_number=line_number,
                context=context.strip()
            ))
        self._links[key] = links

    async def _update_tags(self, key: str, content: str) -> None:
        """更新标签索引"""
        # 先清理该笔记的旧标签
        for tag_name in self._tags:
            self._tags[tag_name] = [t for t in self._tags[tag_name] if t.source != key]

        # 添加新标签
        for match in re.finditer(self.TAG_PATTERN, content):
            tag_name = match.group(1)
            if tag_name not in self._tags:
                self._tags[tag_name] = []
            # 检查是否已存在
            existing = next((t for t in self._tags[tag_name] if t.source == key), None)
            if existing:
                existing.count += 1
            else:
                self._tags[tag_name].append(Tag(
                    name=tag_name,
                    source=key,
                    count=1
                ))

    def _extract_metadata(self, content: str, key: str) -> NoteMetadata:
        """提取笔记元数据"""
        # 提取标签
        tags = []
        tag_counts = {}
        for match in re.finditer(self.TAG_PATTERN, content):
            tag_name = match.group(1)
            tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1

        for tag_name, count in tag_counts.items():
            tags.append(Tag(name=tag_name, source=key, count=count))

        # 提取链接
        wiki_links = []
        for match in re.finditer(self.WIKI_LINK_PATTERN, content):
            target = match.group(1)
            line_number = content[:match.start()].count('\n') + 1
            lines = content.split('\n')
            context = lines[min(line_number - 1, len(lines) - 1)]
            wiki_links.append(WikiLink(
                target=target,
                source=key,
                line_number=line_number,
                context=context.strip()
            ))

        return NoteMetadata(
            file_path=key,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            word_count=len(content.split()),
            tags=tags,
            wiki_links=wiki_links
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


class ContextLayer(MemoryLayer):
    """
    上下文层 - 会话和项目上下文管理

    负责：
    - 会话记忆（当前对话上下文）
    - 项目上下文（代码库结构、依赖）
    - 工作流状态（进行中的任务）
    - 环境状态（已打开文件、光标位置）
    """

    def __init__(self):
        self._sessions: Dict[str, SessionContext] = {}
        self._project_context: Dict[str, Dict] = {}
        self._workflows: Dict[str, Dict] = {}
        self._environment: Dict[str, Dict] = {}

    async def save(self, key: str, value: Any) -> None:
        """保存上下文数据"""
        key_parts = key.split(":")
        if key_parts[0] == "session":
            session_id = key_parts[1] if len(key_parts) > 1 else "default"
            if isinstance(value, dict):
                self._sessions[session_id] = SessionContext(
                    session_id=session_id,
                    user_id=value.get("user_id", "unknown"),
                    messages=value.get("messages", []),
                    project_context=value.get("project_context", {}),
                    workflow_state=value.get("workflow_state", {}),
                    environment_state=value.get("environment_state", {})
                )
        elif key_parts[0] == "project":
            self._project_context[key_parts[1]] = value
        elif key_parts[0] == "workflow":
            self._workflows[key_parts[1]] = value
        elif key_parts[0] == "environment":
            self._environment[key_parts[1]] = value

    async def get(self, key: str) -> Optional[Any]:
        """获取上下文数据"""
        key_parts = key.split(":")
        if key_parts[0] == "session":
            return self._sessions.get(key_parts[1])
        elif key_parts[0] == "project":
            return self._project_context.get(key_parts[1])
        elif key_parts[0] == "workflow":
            return self._workflows.get(key_parts[1])
        elif key_parts[0] == "environment":
            return self._environment.get(key_parts[1])
        return None

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索上下文"""
        results = []
        query_lower = query.lower()

        # 搜索会话消息
        for session_id, session in self._sessions.items():
            for msg in session.messages:
                content = msg.get("content", "")
                if query_lower in content.lower():
                    results.append({
                        "type": "session_message",
                        "session_id": session_id,
                        "preview": content[:100],
                        "timestamp": msg.get("timestamp")
                    })
                    if len(results) >= limit:
                        return results

        # 搜索工作流
        for wf_id, workflow in self._workflows.items():
            wf_str = str(workflow).lower()
            if query_lower in wf_str:
                results.append({
                    "type": "workflow",
                    "workflow_id": wf_id,
                    "status": workflow.get("status"),
                    "description": workflow.get("description", "")[:100]
                })

        return results[:limit]

    async def delete(self, key: str) -> bool:
        """删除上下文数据"""
        key_parts = key.split(":")
        if key_parts[0] == "session" and key_parts[1] in self._sessions:
            del self._sessions[key_parts[1]]
            return True
        elif key_parts[0] == "project" and key_parts[1] in self._project_context:
            del self._project_context[key_parts[1]]
            return True
        elif key_parts[0] == "workflow" and key_parts[1] in self._workflows:
            del self._workflows[key_parts[1]]
            return True
        elif key_parts[0] == "environment" and key_parts[1] in self._environment:
            del self._environment[key_parts[1]]
            return True
        return False

    async def add_message(self, session_id: str, message: Dict) -> None:
        """添加消息到会话"""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionContext(
                session_id=session_id,
                user_id=message.get("user_id", "unknown"),
                messages=[],
                project_context={},
                workflow_state={},
                environment_state={}
            )
        self._sessions[session_id].messages.append(message)

    async def get_session_messages(self, session_id: str, limit: int = 10) -> List[Dict]:
        """获取会话消息"""
        session = self._sessions.get(session_id)
        if session:
            return session.messages[-limit:]
        return []

    async def get_messages(self, user_id: str, limit: int = 10) -> List[Dict]:
        """获取用户消息（别名的 get_session_messages 方法）"""
        return await self.get_session_messages(user_id, limit)

    async def update_workflow_state(self, workflow_id: str, state: Dict) -> None:
        """更新工作流状态"""
        if workflow_id in self._workflows:
            self._workflows[workflow_id].update(state)

    async def get_active_workflows(self) -> List[Dict]:
        """获取活跃的工作流"""
        return [
            {"id": wf_id, **workflow}
            for wf_id, workflow in self._workflows.items()
            if workflow.get("status") in ["running", "pending"]
        ]


class SemanticLayer(MemoryLayer):
    """
    语义层 - Agent 记忆和模式学习

    负责：
    - Agent 记忆（工作方式、偏好）
    - 用户画像（交互模式、历史）
    - 任务模式（成功/失败案例）
    - 模式库（可复用的解决方案）
    """

    def __init__(self):
        self._agent_memories: Dict[str, Dict] = {}
        self._user_profiles: Dict[str, UserProfile] = {}
        self._patterns: Dict[str, AgentPattern] = {}
        self._task_history: List[Dict] = []

    async def save(self, key: str, value: Any) -> None:
        """保存语义数据"""
        key_parts = key.split(":")
        if key_parts[0] == "agent":
            agent_id = key_parts[1]
            if isinstance(value, dict):
                if agent_id not in self._agent_memories:
                    self._agent_memories[agent_id] = {}
                self._agent_memories[agent_id].update(value)
        elif key_parts[0] == "user":
            if isinstance(value, dict):
                self._user_profiles[key_parts[1]] = UserProfile(
                    user_id=key_parts[1],
                    preferred_agents=value.get("preferred_agents", []),
                    interaction_style=value.get("interaction_style", "standard"),
                    task_patterns=value.get("task_patterns", []),
                    session_count=value.get("session_count", 0)
                )
        elif key_parts[0] == "pattern":
            if isinstance(value, dict):
                self._patterns[value.get("pattern_id", key_parts[1])] = AgentPattern(
                    pattern_id=value.get("pattern_id", key_parts[1]),
                    name=value.get("name", "unnamed"),
                    description=value.get("description", ""),
                    success_rate=value.get("success_rate", 0.0),
                    usage_count=value.get("usage_count", 0),
                    last_used=datetime.now(),
                    tags=value.get("tags", [])
                )

    async def get(self, key: str) -> Optional[Any]:
        """获取语义数据"""
        key_parts = key.split(":")
        if key_parts[0] == "agent":
            return self._agent_memories.get(key_parts[1])
        elif key_parts[0] == "user":
            return self._user_profiles.get(key_parts[1])
        elif key_parts[0] == "pattern":
            return self._patterns.get(key_parts[1])
        return None

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索语义数据"""
        results = []
        query_lower = query.lower()

        # 搜索模式
        for pattern_id, pattern in self._patterns.items():
            if (query_lower in pattern.name.lower() or
                query_lower in pattern.description.lower() or
                any(query_lower in tag.lower() for tag in pattern.tags)):
                results.append({
                    "type": "pattern",
                    "pattern_id": pattern_id,
                    "name": pattern.name,
                    "description": pattern.description,
                    "success_rate": pattern.success_rate,
                    "usage_count": pattern.usage_count
                })

        # 搜索用户画像
        for user_id, profile in self._user_profiles.items():
            if query_lower in user_id.lower():
                results.append({
                    "type": "user_profile",
                    "user_id": user_id,
                    "interaction_style": profile.interaction_style,
                    "preferred_agents": profile.preferred_agents
                })

        return results[:limit]

    async def delete(self, key: str) -> bool:
        """删除语义数据"""
        key_parts = key.split(":")
        if key_parts[0] == "agent" and key_parts[1] in self._agent_memories:
            del self._agent_memories[key_parts[1]]
            return True
        elif key_parts[0] == "user" and key_parts[1] in self._user_profiles:
            del self._user_profiles[key_parts[1]]
            return True
        elif key_parts[0] == "pattern" and key_parts[1] in self._patterns:
            del self._patterns[key_parts[1]]
            return True
        return False

    async def learn_pattern(self, pattern: Dict) -> str:
        """学习新模式"""
        pattern_id = pattern.get("pattern_id") or f"pattern-{len(self._patterns)}"

        # 如果模式已存在，更新成功率
        if pattern_id in self._patterns:
            existing = self._patterns[pattern_id]
            existing.usage_count += 1
            existing.last_used = datetime.now()
            # 更新成功率（简单平均）
            new_rate = pattern.get("success_rate")
            if new_rate is not None:
                existing.success_rate = (existing.success_rate * (existing.usage_count - 1) + new_rate) / existing.usage_count
            return pattern_id

        # 创建新模式
        self._patterns[pattern_id] = AgentPattern(
            pattern_id=pattern_id,
            name=pattern.get("name", "unnamed"),
            description=pattern.get("description", ""),
            success_rate=pattern.get("success_rate", 0.0),
            usage_count=1,
            last_used=datetime.now(),
            tags=pattern.get("tags", [])
        )
        return pattern_id

    async def get_best_patterns(self, task_type: str, limit: int = 3) -> List[AgentPattern]:
        """获取针对特定任务类型的最佳模式"""
        candidates = []
        for pattern in self._patterns.values():
            # 检查标签是否匹配
            if task_type in pattern.tags or task_type.lower() in pattern.description.lower():
                candidates.append(pattern)

        # 按成功率排序
        candidates.sort(key=lambda p: (p.success_rate, p.usage_count), reverse=True)
        return candidates[:limit]

    async def update_user_behavior(self, user_id: str, behavior_data: Dict) -> None:
        """更新用户行为数据"""
        if user_id not in self._user_profiles:
            self._user_profiles[user_id] = UserProfile(
                user_id=user_id,
                preferred_agents=[],
                interaction_style="standard",
                task_patterns=[],
                session_count=0
            )

        profile = self._user_profiles[user_id]

        # 更新偏好的 Agent
        agent_used = behavior_data.get("agent_used")
        if agent_used and agent_used not in profile.preferred_agents:
            profile.preferred_agents.append(agent_used)

        # 更新任务模式
        task_type = behavior_data.get("task_type")
        if task_type and task_type not in profile.task_patterns:
            profile.task_patterns.append(task_type)

        # 更新交互风格
        if behavior_data.get("style"):
            profile.interaction_style = behavior_data["style"]

    async def record_task_completion(self, task_id: str, success: bool, metadata: Dict) -> None:
        """记录任务完成情况"""
        self._task_history.append({
            "task_id": task_id,
            "success": success,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata
        })

    async def get_task_statistics(self) -> Dict:
        """获取任务统计"""
        if not self._task_history:
            return {"total": 0, "success": 0, "failure": 0, "success_rate": 0.0}

        total = len(self._task_history)
        success_count = sum(1 for task in self._task_history if task["success"])
        return {
            "total": total,
            "success": success_count,
            "failure": total - success_count,
            "success_rate": success_count / total if total > 0 else 0.0
        }


class LayeredMemory:
    """
    三层 Memory 架构统一接口

    整合 Knowledge Layer、Context Layer 和 Semantic Layer，
    提供统一的访问接口。
    """

    def __init__(
        self,
        knowledge_db: Optional[MemoryLayer] = None,
        context_db: Optional[MemoryLayer] = None,
        semantic_db: Optional[MemoryLayer] = None
    ):
        self.knowledge = knowledge_db or KnowledgeLayer()
        self.context = context_db or ContextLayer()
        self.semantic = semantic_db or SemanticLayer()

    async def save_interaction(
        self,
        user_id: str,
        agent_id: str,
        input: str,
        output: str,
        reasoning: Optional[str] = None,
        confidence: Optional[float] = None,
        layer: LayerType = LayerType.CONTEXT
    ) -> str:
        """
        保存交互到指定层级

        返回保存的数据的 key
        """
        timestamp = datetime.now().isoformat()

        if layer == LayerType.CONTEXT:
            # 保存到上下文层（会话消息）
            session_key = f"session:{user_id}"
            message = {
                "role": "user",
                "content": input,
                "agent_response": output,
                "agent_id": agent_id,
                "reasoning": reasoning,
                "confidence": confidence,
                "timestamp": timestamp
            }
            await self.context.add_message(user_id, message)
            return session_key

        elif layer == LayerType.SEMANTIC:
            # 保存到语义层（用户行为）
            await self.semantic.update_user_behavior(user_id, {
                "agent_used": agent_id,
                "task_type": reasoning or "general",
                "style": "standard"
            })
            # 记录任务完成
            task_id = f"task-{int(datetime.now().timestamp())}"
            success = confidence is not None and confidence > 0.5
            await self.semantic.record_task_completion(task_id, success, {
                "agent_id": agent_id,
                "input": input[:100]
            })
            return f"semantic:{user_id}"

        return ""

    async def get_knowledge(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索知识层"""
        return await self.knowledge.search(query, limit)

    async def get_context(self, user_id: str, session_id: Optional[str] = None) -> Dict:
        """获取上下文"""
        sid = session_id or user_id
        session = await self.context.get(f"session:{sid}")
        if session:
            return {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "messages": session.messages,
                "project_context": session.project_context,
                "workflow_state": session.workflow_state,
                "environment_state": session.environment_state
            }
        return {"session_id": sid, "messages": []}

    async def get_semantic(self, user_id: str, role: Optional[str] = None) -> Dict:
        """获取语义记忆"""
        user_profile = await self.semantic.get(f"user:{user_id}")

        result = {"user_id": user_id}
        if user_profile:
            result.update({
                "preferred_agents": user_profile.preferred_agents,
                "interaction_style": user_profile.interaction_style,
                "task_patterns": user_profile.task_patterns,
                "session_count": user_profile.session_count
            })

        # 获取任务统计
        stats = await self.semantic.get_task_statistics()
        result["task_statistics"] = stats

        # 如果指定了角色，获取相关模式
        if role:
            patterns = await self.semantic.get_best_patterns(role, limit=5)
            result["relevant_patterns"] = [
                {
                    "pattern_id": p.pattern_id,
                    "name": p.name,
                    "description": p.description,
                    "success_rate": p.success_rate
                }
                for p in patterns
            ]

        return result

    async def learn_pattern(self, pattern: Dict) -> str:
        """学习新模式到语义层"""
        return await self.semantic.learn_pattern(pattern)

    async def save_note(self, key: str, content: str) -> None:
        """保存笔记到知识层"""
        await self.knowledge.save(key, content)

    async def get_note(self, key: str) -> Optional[Dict]:
        """获取笔记"""
        return await self.knowledge.get(key)

    async def get_related_notes(self, key: str, limit: int = 5) -> List[Dict]:
        """获取相关笔记"""
        return await self.knowledge.get_related_notes(key, limit)

    async def search_all(self, query: str, limit_per_layer: int = 5) -> Dict[str, List[Dict]]:
        """跨所有层级搜索"""
        return {
            "knowledge": await self.get_knowledge(query, limit_per_layer),
            "context": await self.context.search(query, limit_per_layer),
            "semantic": await self.semantic.search(query, limit_per_layer)
        }

    async def get_active_workflows(self) -> List[Dict]:
        """获取活跃的工作流"""
        return await self.context.get_active_workflows()

    async def get_best_patterns(self, task_type: str, limit: int = 3) -> List[AgentPattern]:
        """获取针对特定任务类型的最佳模式"""
        return await self.semantic.get_best_patterns(task_type, limit)

    async def get_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取用户历史记录（从上下文层）"""
        messages = await self.context.get_messages(user_id, limit)
        # 转换为兼容旧格式的历史记录
        history = []
        for msg in messages:
            history.append({
                "agent_id": msg.get("agent_id", ""),
                "input": msg.get("content", ""),
                "output": msg.get("agent_response", ""),
                "reasoning": msg.get("reasoning", ""),
                "confidence": msg.get("confidence", None),
                "timestamp": msg.get("timestamp", "")
            })
        return history
