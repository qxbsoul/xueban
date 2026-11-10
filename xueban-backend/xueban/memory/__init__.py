"""
Memory 模块 - 三层记忆架构

提供会话级、用户级、全局级记忆管理。
"""

# 原有接口（向后兼容）
try:
    from agentgraph.memory.base import Memory, BaseMemory
    from agentgraph.memory.base import (
        MemoryScope,
        Interaction,
        Pattern,
    )
except ModuleNotFoundError:  # pragma: no cover - legacy package not installed
    Memory = None  # type: ignore[assignment]
    BaseMemory = None  # type: ignore[assignment]
    MemoryScope = None  # type: ignore[assignment]
    Interaction = None  # type: ignore[assignment]
    Pattern = None  # type: ignore[assignment]

# 原有的实现
try:
    from agentgraph.memory.sqlite import SQLiteMemory
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    SQLiteMemory = None

try:
    from agentgraph.memory.redis import RedisMemory
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    RedisMemory = None

# 三层架构
try:
    from agentgraph.memory.layers import (
        LayerType,
        MemoryLayer,
        KnowledgeLayer,
        ContextLayer,
        SemanticLayer,
        LayeredMemory,
        WikiLink,
        Tag,
        NoteMetadata,
        SessionContext,
        AgentPattern,
        UserProfile,
    )
except ModuleNotFoundError:  # pragma: no cover - legacy package not installed
    LayerType = None  # type: ignore[assignment]
    MemoryLayer = None  # type: ignore[assignment]
    KnowledgeLayer = None  # type: ignore[assignment]
    ContextLayer = None  # type: ignore[assignment]
    SemanticLayer = None  # type: ignore[assignment]
    LayeredMemory = None  # type: ignore[assignment]
    WikiLink = None  # type: ignore[assignment]
    Tag = None  # type: ignore[assignment]
    NoteMetadata = None  # type: ignore[assignment]
    SessionContext = None  # type: ignore[assignment]
    AgentPattern = None  # type: ignore[assignment]
    UserProfile = None  # type: ignore[assignment]

# 新的三层记忆实现
try:
    from agentgraph.memory.session import SessionMemory
except ModuleNotFoundError:  # pragma: no cover - legacy package not installed
    SessionMemory = None  # type: ignore[assignment]
try:
    from agentgraph.memory.user import UserMemory, ContextResult, UserMemoryFactory
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    UserMemory = None
    ContextResult = None
    UserMemoryFactory = None

try:
    from agentgraph.memory.global_memory import GlobalMemory, PatternType
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    GlobalMemory = None
    PatternType = None

try:
    from agentgraph.memory.patterns import PatternManager
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    PatternManager = None

# Story 9.1 — KnowledgePack + RetrievalProfile (independent, not part of legacy
# agentgraph.memory). Imported eagerly because xueban.api.knowledge depends on
# them.
from xueban.memory.retrieval_profiles import RetrievalProfile, RetrievalMode
from xueban.memory.knowledge_pack import (
    KnowledgePack,
    KnowledgeSource,
    SourceType,
    IngestStatus,
    PackStatus,
    FreshnessPolicy,
    update_pack,
)

__all__ = [
    # 基础接口
    "Memory",
    "BaseMemory",

    # 数据类和枚举
    "MemoryScope",
    "Interaction",
    "Pattern",

    # 原有实现
    "SQLiteMemory",
    "RedisMemory",

    # 三层架构
    "LayerType",
    "MemoryLayer",
    "KnowledgeLayer",
    "ContextLayer",
    "SemanticLayer",
    "LayeredMemory",
    "WikiLink",
    "Tag",
    "NoteMetadata",
    "SessionContext",
    "AgentPattern",
    "UserProfile",

    # 新的三层记忆实现
    "SessionMemory",
    "UserMemory",
    "GlobalMemory",
    "PatternType",
    "PatternManager",

    # 兼容旧的导出
    "ContextResult",
    "UserMemoryFactory",

    # Story 9.1 — KnowledgePack
    "RetrievalProfile",
    "RetrievalMode",
    "KnowledgePack",
    "KnowledgeSource",
    "SourceType",
    "IngestStatus",
    "PackStatus",
    "FreshnessPolicy",
    "update_pack",
]
